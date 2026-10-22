"""Wikidata 政治面摄取器 + 查询（任务 1.4，接线 docs/wikidata-probe.md 的定案）。

探针定案（端点直连实测，36 份样本 docs/wikidata-probe-samples/）落成代码：

- 四类实体查询姿势：人物 occupation=P106:Q82955（occupation 为主判据，P39 只作
  任期补充、绝不进判据——pol2 样本里「先知」误挂 P39 的噪声实证）；政党 P31∈
  Q7278 子类闭包；事件 P31∈Q40231 子类闭包（**闭包是必要姿势**，直查漏子类：
  event2 直查 20 行 vs event3 闭包 7 行不同集合）；政治运动 P31∈Q2738074。
- 极性判据表（每边落库记属性 PID 作极性依据，可回溯）：
  继承 P155/P1365；同盟 P361/P463/P102；对立分层取信 P5004(1.0) > P461(0.8,
  查询内做政治域过滤) > P7047(0.4, 个体恩怨降权)；P991 获胜者是事件→实体关联
  （非极性，polarity=NULL）。
- 「批判」档在 Wikidata 结构性缺失（P3323 全库 4 条，search_props_critique 命中
  全是外部 ID 属性）——本模块不产批判边，批判关系由参照系其他源（SEP/中国线）承载。
- 坐标锚双通道：P1142 意识形态（政党面 15,257 使用）/ P1387 政治倾向（3,804）。

快照版本化：WD 无版本号 → version = 摄取时间(UTC) + 查询指纹（种子集+限额+四类
实体查询文本的 sha256 前 12 位）；原始响应整体存不可变快照（snapshot.store），
CC0 归属，快照 id 回填到每一行。wd_* 三表是「当前快照」视图：新快照落库后清掉
旧快照行（全量刷新语义，库本就是可重建缓存）。

网络层照 fd-zhesi-reference 习惯：描述性 UA、POST + sparql-results+json、45s
超时、短退避 ×2；代理走 FDZX_REF_PROXY（空=显式直连）。断网/5xx/解析失败按
errors.py 口径映射 unreachable / upstream，绝不静默为空。

查询面（读已落库数据，离线）：query_entity / query_relations(极性过滤) /
query_ideology_anchor，逐结果带 provenance；空结果必带 taxonomy reason。
"""

from __future__ import annotations

import hashlib
import json
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import schema, snapshot
from .config import Settings
from .errors import NOT_INGESTED, UNREACHABLE, UPSTREAM, empty_result
from .license import assert_allowed_url, require_license
from .provenance import ATTRIBUTIONS, provenance

SOURCE = "wikidata"
DATASET = "wikidata-politics"
UA = "fd-zhengxin-reference/0.1 (zhengxin line; contact: local)"  # Wikidata UA 政策：描述性
DEFAULT_TIMEOUT = 45
QID_RE = re.compile(r"Q\d+")

# ---- 类锚（探针 find_class_*/verify_classes 定案）----
POLITICIAN_OCCUPATION = "Q82955"  # politician（occupation，直挂）
PARTY_CLASS = "Q7278"             # political party（P31 子类闭包）
EVENT_CLASS = "Q40231"            # election（P31 子类闭包——事件类必要姿势）
MOVEMENT_CLASS = "Q2738074"       # political movement（find_class_movement 唯一正主）

# 域过滤用的政治类全集（P461 opposite of 需过滤虚构角色对立，domain_P461 样本）
POLITICAL_DOMAIN_CLASSES = (PARTY_CLASS, MOVEMENT_CLASS, EVENT_CLASS)

# ---- 极性四档 ----
POLARITY_INHERITANCE = "inheritance"  # 继承
POLARITY_ALLIANCE = "alliance"        # 同盟
POLARITY_OPPOSITION = "opposition"    # 对立
POLARITY_CRITIQUE = "critique"        # 批判（WD 缺失，见 CRITIQUE_GAP；仅作查询 token）
POLARITY_TIERS = (POLARITY_INHERITANCE, POLARITY_ALLIANCE, POLARITY_OPPOSITION, POLARITY_CRITIQUE)

# 极性判据表：pid -> (polarity, weight)。对立分层取信 P5004 > P461(域过滤) > P7047(降权)。
POLARITY_TABLE: dict[str, tuple[str | None, float]] = {
    "P155": (POLARITY_INHERITANCE, 1.0),   # follows 后继于（谱系顺序）
    "P1365": (POLARITY_INHERITANCE, 1.0),  # replaces 取代（结构上接续记继承；政权更替的对立面
    #                                          向在谱系层由对立三属性承载）
    "P361": (POLARITY_ALLIANCE, 1.0),      # part of（组织隶属）
    "P463": (POLARITY_ALLIANCE, 1.0),      # member of（党际组织成员）
    "P102": (POLARITY_ALLIANCE, 1.0),      # party membership（人→党）
    "P5004": (POLARITY_OPPOSITION, 1.0),   # opposition to（最贴政治域，优先取信）
    "P461": (POLARITY_OPPOSITION, 0.8),    # opposite of（查询内政治域过滤后取信）
    "P7047": (POLARITY_OPPOSITION, 0.4),   # enemy of（个体恩怨噪声高，降权）
    "P991": (None, 1.0),                   # winner（事件→实体关联，非极性）
}
TRACKED_PIDS = tuple(POLARITY_TABLE)

# PID 语义名（relations.kind 列；开放词表，回溯时人读）
PID_KIND = {
    "P155": "follows",
    "P1365": "replaces",
    "P361": "part_of",
    "P463": "member_of",
    "P102": "party_membership",
    "P5004": "opposition_to",
    "P461": "opposite_of",
    "P7047": "enemy_of",
    "P991": "winner",
}

# 边查询姿势（变量名照探针样本，fixture 可 1:1 作响应桩）
EDGE_SHAPES: dict[str, tuple[str, str]] = {
    "P155": ("s", "o"),
    "P1365": ("s", "o"),
    "P461": ("s", "o"),
    "P5004": ("s", "o"),
    "P7047": ("s", "o"),
    "P361": ("m", "parent"),
    "P463": ("party", "org"),
    "P102": ("person", "party"),
    "P991": ("event", "winner"),
}

# 实体邻域扩展用的 tie 属性（seed 一跳内找同类实体；P39 刻意不在内——只作任期）
HOP_PIDS = ("P102", "P463", "P361", "P155", "P1365", "P461", "P5004", "P7047", "P991")

CRITIQUE_GAP = (
    "批判档在 Wikidata 结构性缺失（P3323 disputation with 全库仅 4 条，"
    "search_props_critique 命中全是外部 ID 属性，见 docs/wikidata-probe.md §3）；"
    "批判关系由参照系其他源（SEP 概念层、中国线 curated 谱系）承载，Wikidata 只出不进。"
)

ENTITY_KINDS = ("politician", "party", "event", "movement")


# ---------------------------------------------------------------- 网络层


class SparqlError(RuntimeError):
    """查询失败（短退避后仍失败）。reason 按 errors.py 口径：unreachable / upstream。"""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"SPARQL 查询失败（{reason}）：{detail}")
        self.reason = reason
        self.detail = detail


class SparqlClient:
    """stdlib urllib 的 SPARQL 薄客户端：UA/POST/超时/短退避 ×2，代理走 FDZX_REF_PROXY。

    家族规矩（fd-zhesi-reference scripts/ingest_wikidata.py）：描述性 UA、
    POST 走表单（长查询不撑请求行）、45s 超时、4s/9s 退避两回。本仓无
    SPARQLWrapper 依赖，传输层用 urllib 手搓最小实现（格式是标准
    sparql-results+json，解析在 parse_* 纯函数里离线可测）。
    """

    def __init__(
        self,
        endpoint: str | None = None,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        proxy: str | None = None,
        retries: tuple[float, ...] = (4.0, 9.0),
        ua: str = UA,
        opener: urllib.request.OpenerDirector | None = None,
    ):
        settings = Settings.from_env()
        self.endpoint = endpoint or settings.wikidata_sparql
        self.timeout = timeout
        self.proxy = settings.http_proxy if proxy is None else proxy
        self.retries = tuple(retries)
        self.ua = ua
        if opener is not None:
            self.opener = opener
        elif self.proxy:
            self.opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": self.proxy, "https": self.proxy})
            )
        else:
            # 显式空 ProxyHandler：不吃环境代理变量，空=直连（config 注释口径）
            self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def query(self, sparql: str) -> dict:
        last: SparqlError | None = None
        for attempt in range(1 + len(self.retries)):
            try:
                return self._query_once(sparql)
            except SparqlError as exc:
                last = exc
                if attempt < len(self.retries):
                    time.sleep(self.retries[attempt])
        raise last  # type: ignore[misc]

    def _query_once(self, sparql: str) -> dict:
        data = urllib.parse.urlencode({"query": sparql}).encode("utf-8")
        req = urllib.request.Request(
            self.endpoint,
            data=data,
            headers={
                "User-Agent": self.ua,
                "Accept": "application/sparql-results+json",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            method="POST",
        )
        try:
            with self.opener.open(req, timeout=self.timeout) as resp:
                payload = resp.read()
        except urllib.error.HTTPError as exc:  # 先于 URLError：源可达但响应异常
            raise SparqlError(UPSTREAM, f"http {exc.code} {str(exc.reason)[:120]}") from exc
        except urllib.error.URLError as exc:
            raise SparqlError(UNREACHABLE, str(exc.reason or exc)[:160]) from exc
        except (socket.timeout, TimeoutError) as exc:
            raise SparqlError(UNREACHABLE, f"timeout {self.timeout}s") from exc
        try:
            return json.loads(payload)
        except (ValueError, UnicodeDecodeError) as exc:
            raise SparqlError(UPSTREAM, f"响应不是合法 JSON：{str(exc)[:120]}") from exc


# ---------------------------------------------------------------- 查询构造
# 每个查询首行带 `# fdzx:<标记>` 注释：离线 stub 按标记回放 fixture，也方便日志排查。


def normalize_qids(qids) -> list[str]:
    r"""清洗种子：只认 Q\d+，去重保序；空/全非法即 ValueError（fail loud）。"""
    out: list[str] = []
    for raw in qids:
        qid = str(raw).strip()
        if not QID_RE.fullmatch(qid):
            raise ValueError(f"非法 QID：{raw!r}（形如 Q17427）")
        if qid not in out:
            out.append(qid)
    if not out:
        raise ValueError("seed_qids 为空")
    return out


def _values(qids: list[str], var: str) -> str:
    return f"VALUES ?{var} {{ {' '.join('wd:' + q for q in qids)} }}"


def _seed_hop_clause(item: str, seeds: list[str], signature: str) -> str:
    """实体邻域：item 满足类签名，且与任一 seed 隔着一条受跟踪属性（双向），
    或 item 本身是满足签名的 seed。签名用具体闭包路径（wdt:P31/wdt:P279* wd:Qx），
    不绑 ?klass 再 FILTER——那会先物化整棵 P279 祖先树。"""
    ties = " ".join("wdt:" + p for p in HOP_PIDS)
    return (
        f"{{ {signature} . {_values(seeds, 'seed')} . VALUES ?tie {{ {ties} }} .\n"
        f"    {{ ?seed ?tie ?{item} }} UNION {{ ?{item} ?tie ?seed }} }}\n"
        f"  UNION {{ {_values(seeds, item)} {signature} }}"
    )


def build_person_query(seed_qids: list[str], limit: int = 200) -> str:
    """政治人物：occupation=P106:Q82955 直挂（探针 pol1 姿势；P39 只作任期不入判据）。"""
    seeds = normalize_qids(seed_qids)
    signature = f"?person wdt:P106 wd:{POLITICIAN_OCCUPATION}"
    return f"""# fdzx:person
SELECT DISTINCT ?person ?personLabel ?birth ?death ?countryLabel WHERE {{
  {_seed_hop_clause("person", seeds, signature)}
  OPTIONAL {{ ?person wdt:P569 ?birth. }}
  OPTIONAL {{ ?person wdt:P570 ?death. }}
  OPTIONAL {{ ?person wdt:P27 ?country. }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en".
    ?person rdfs:label ?personLabel. ?country rdfs:label ?countryLabel. }}
}}
LIMIT {limit}"""


def build_party_query(seed_qids: list[str], limit: int = 200) -> str:
    """政党：P31∈Q7278 子类闭包 + P1387 政治倾向（探针 party1 姿势）+P571 成立。"""
    seeds = normalize_qids(seed_qids)
    signature = f"?party wdt:P31/wdt:P279* wd:{PARTY_CLASS}"
    return f"""# fdzx:party
SELECT DISTINCT ?party ?partyLabel ?alignLabel ?inception ?countryLabel WHERE {{
  {_seed_hop_clause("party", seeds, signature)}
  OPTIONAL {{ ?party wdt:P1387 ?align. }}
  OPTIONAL {{ ?party wdt:P571 ?inception. }}
  OPTIONAL {{ ?party wdt:P17 ?country. }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en".
    ?party rdfs:label ?partyLabel. ?align rdfs:label ?alignLabel. ?country rdfs:label ?countryLabel. }}
}}
LIMIT {limit}"""


def build_event_query(seed_qids: list[str], limit: int = 200) -> str:
    """政治事件：P31∈Q40231 子类闭包（探针 event3 姿势——直查漏子类，闭包是必要姿势）。"""
    seeds = normalize_qids(seed_qids)
    signature = f"?event wdt:P31 ?class . ?class wdt:P279* wd:{EVENT_CLASS}"
    return f"""# fdzx:event
SELECT DISTINCT ?event ?eventLabel ?classLabel ?when ?countryLabel WHERE {{
  {_seed_hop_clause("event", seeds, signature)}
  OPTIONAL {{ ?event wdt:P585 ?when. }}
  OPTIONAL {{ ?event wdt:P17 ?country. }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en".
    ?event rdfs:label ?eventLabel. ?class rdfs:label ?classLabel. ?country rdfs:label ?countryLabel. }}
}}
LIMIT {limit}"""


def build_movement_query(seed_qids: list[str], limit: int = 200) -> str:
    """政治运动/组织：P31∈Q2738074 子类闭包（探针 move1 姿势）。"""
    seeds = normalize_qids(seed_qids)
    signature = f"?m wdt:P31/wdt:P279* wd:{MOVEMENT_CLASS}"
    return f"""# fdzx:movement
SELECT DISTINCT ?m ?mLabel ?inception ?countryLabel WHERE {{
  {_seed_hop_clause("m", seeds, signature)}
  OPTIONAL {{ ?m wdt:P571 ?inception. }}
  OPTIONAL {{ ?m wdt:P17 ?country. }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en".
    ?m rdfs:label ?mLabel. ?country rdfs:label ?countryLabel. }}
}}
LIMIT {limit}"""


def _political_domain_filter(var: str) -> str:
    """P461 域过滤：端点是政治人物（P106）或政党/运动/选举类闭包实例。

    探针 domain_P461_opposite_political 定案：不过滤会混入虚构角色对立。
    """
    pols = " ".join("wd:" + q for q in POLITICAL_DOMAIN_CLASSES)
    return (
        f"{{ {{ ?{var} wdt:P106 wd:{POLITICIAN_OCCUPATION} }}\n"
        f"   UNION {{ ?{var} wdt:P31/wdt:P279* ?pol . VALUES ?pol {{ {pols} }} }} }}"
    )


def build_edge_query(pid: str, anchor_qids: list[str], limit: int = 500) -> str:
    """单属性关系边：任一端点 ∈ anchor（seed 一跳 / 扩展域二跳共用本构造）。

    变量名照探针边样本（EDGE_SHAPES），fixture 可直接作响应桩。P461 附政治域过滤；
    P7047 不过滤但权重 0.4 降权（P5004>P461>P7047 分层取信在 POLARITY_TABLE）。
    """
    if pid not in EDGE_SHAPES or pid == "P991":
        raise ValueError(f"属性 {pid} 无边查询姿势（P991 用 build_winner_query）")
    svar, ovar = EDGE_SHAPES[pid]
    anchors = normalize_qids(anchor_qids)
    domain = ""
    if pid == "P461":
        domain = f"  {_political_domain_filter('s')}\n  {_political_domain_filter('o')}\n"
    return f"""# fdzx:edge:{pid}
SELECT ?{svar} ?{svar}Label ?{ovar} ?{ovar}Label WHERE {{
  {_values(anchors, 'a')}
  {{ ?a wdt:{pid} ?{ovar} . BIND(?a AS ?{svar}) }} UNION {{ ?{svar} wdt:{pid} ?a . BIND(?a AS ?{ovar}) }}
{domain}  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en".
    ?{svar} rdfs:label ?{svar}Label. ?{ovar} rdfs:label ?{ovar}Label. }}
}}
LIMIT {limit}"""


def build_winner_query(anchor_qids: list[str], limit: int = 200) -> str:
    """P991 获胜者（事件→实体关联，非极性）：探针 edges_P991 姿势（含 P585 时间）。"""
    anchors = normalize_qids(anchor_qids)
    return f"""# fdzx:edge:P991
SELECT ?event ?eventLabel ?winner ?winnerLabel ?when WHERE {{
  {_values(anchors, 'a')}
  {{ ?a wdt:P991 ?winner . BIND(?a AS ?event) }} UNION {{ ?event wdt:P991 ?a . BIND(?a AS ?winner) }}
  OPTIONAL {{ ?event wdt:P585 ?when. }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en".
    ?event rdfs:label ?eventLabel. ?winner rdfs:label ?winnerLabel. }}
}}
LIMIT {limit}"""


def build_anchor_query(qids: list[str], limit: int = 500) -> str:
    """坐标锚双通道：P1142 意识形态（实体值）+ P1387 政治倾向（探针 edges_P1142/party1 姿势）。"""
    items = normalize_qids(qids)
    return f"""# fdzx:anchor
SELECT ?party ?partyLabel ?ideology ?ideologyLabel ?align ?alignLabel WHERE {{
  {_values(items, 'party')}
  OPTIONAL {{ ?party wdt:P1142 ?ideology. }}
  OPTIONAL {{ ?party wdt:P1387 ?align. }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en".
    ?party rdfs:label ?partyLabel. ?ideology rdfs:label ?ideologyLabel. ?align rdfs:label ?alignLabel. }}
}}
LIMIT {limit}"""


def query_fingerprint(seed_qids: list[str], limit: int, edge_limit: int) -> str:
    """查询指纹：种子集（排序稳定）+ 限额 + 四类实体查询文本的 sha256。

    WD 无版本号，快照 version = 摄取时间 + 本指纹（动态的二跳边查询不进指纹，
    它由种子与一跳实体决定，指纹覆盖姿势即覆盖语义）。
    """
    seeds = sorted(normalize_qids(seed_qids))
    queries = [
        build_person_query(seeds, limit),
        build_party_query(seeds, limit),
        build_event_query(seeds, limit),
        build_movement_query(seeds, limit),
    ]
    blob = json.dumps(
        {"seeds": seeds, "limit": limit, "edge_limit": edge_limit, "queries": queries},
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- 解析（纯函数）


@dataclass
class PoliticalEntity:
    """一个政治面实体（多行绑定按 QID 合并，首见非空值占位）。"""

    qid: str
    kind: str  # politician / party / event / movement
    label_en: str | None = None
    label_zh: str | None = None
    birth: str | None = None
    death: str | None = None
    inception: str | None = None
    point_in_time: str | None = None  # 事件时间（P585）
    country: str | None = None
    class_label: str | None = None    # 事件命中的子类标签（闭包证据）


@dataclass(frozen=True)
class RelationEdge:
    """一条关系边：QID→QID + 属性 PID + 推导极性（探针 §4 的可回溯形态）。"""

    source_id: str
    target_id: str
    pid: str
    polarity: str | None
    weight: float
    label_source: str | None
    label_target: str | None


@dataclass(frozen=True)
class IdeologyAnchor:
    """坐标锚：P1142（意识形态实体）/ P1387（政治倾向，连续值姿势可无 QID）。"""

    qid: str
    anchor_pid: str
    anchor_qid: str | None
    anchor_label: str


def _bindings(sparql_json: dict) -> list[dict]:
    rows = sparql_json.get("results", {}).get("bindings")
    return rows if isinstance(rows, list) else []


def _val(row: dict, key: str) -> str | None:
    binding = row.get(key)
    if not isinstance(binding, dict):
        return None
    value = binding.get("value")
    return str(value) if value else None


def _qid_of(row: dict, key: str) -> str:
    uri = _val(row, key) or ""
    qid = uri.rsplit("/", 1)[-1]
    return qid if QID_RE.fullmatch(qid) else ""


def _clean_label(value: str | None) -> str | None:
    """label service 的坑：无标签时回填 QID 字符串——视为无标签（不硬造）。"""
    if value is None:
        return None
    return None if QID_RE.fullmatch(value) else value


def _merge_entity(existing: PoliticalEntity, **fields) -> None:
    for name, value in fields.items():
        if value is not None and getattr(existing, name) is None:
            setattr(existing, name, value)


def parse_person_rows(sparql_json: dict) -> list[PoliticalEntity]:
    out: dict[str, PoliticalEntity] = {}
    for row in _bindings(sparql_json):
        qid = _qid_of(row, "person")
        if not qid:
            continue
        ent = out.setdefault(qid, PoliticalEntity(qid=qid, kind="politician"))
        _merge_entity(
            ent,
            label_en=_clean_label(_val(row, "personLabel")),
            birth=_val(row, "birth"),
            death=_val(row, "death"),
            country=_clean_label(_val(row, "countryLabel")),
        )
    return list(out.values())


def parse_party_rows(sparql_json: dict) -> tuple[list[PoliticalEntity], list[IdeologyAnchor]]:
    """政党行 → 实体 + P1387 坐标锚（party1 姿势的 alignLabel）。"""
    out: dict[str, PoliticalEntity] = {}
    anchors: dict[tuple[str, str, str], IdeologyAnchor] = {}
    for row in _bindings(sparql_json):
        qid = _qid_of(row, "party")
        if not qid:
            continue
        ent = out.setdefault(qid, PoliticalEntity(qid=qid, kind="party"))
        _merge_entity(
            ent,
            label_en=_clean_label(_val(row, "partyLabel")),
            label_zh=_clean_label(_val(row, "partyLabelZh")),
            inception=_val(row, "inception"),
            country=_clean_label(_val(row, "countryLabel")),
        )
        if align := _clean_label(_val(row, "alignLabel")):
            key = (qid, "P1387", align)
            anchors.setdefault(key, IdeologyAnchor(qid, "P1387", None, align))
    return list(out.values()), list(anchors.values())


def parse_event_rows(sparql_json: dict) -> list[PoliticalEntity]:
    out: dict[str, PoliticalEntity] = {}
    for row in _bindings(sparql_json):
        qid = _qid_of(row, "event")
        if not qid:
            continue
        ent = out.setdefault(qid, PoliticalEntity(qid=qid, kind="event"))
        _merge_entity(
            ent,
            label_en=_clean_label(_val(row, "eventLabel")),
            point_in_time=_val(row, "when"),
            country=_clean_label(_val(row, "countryLabel")),
            class_label=_clean_label(_val(row, "classLabel")),
        )
    return list(out.values())


def parse_movement_rows(sparql_json: dict) -> list[PoliticalEntity]:
    out: dict[str, PoliticalEntity] = {}
    for row in _bindings(sparql_json):
        qid = _qid_of(row, "m")
        if not qid:
            continue
        ent = out.setdefault(qid, PoliticalEntity(qid=qid, kind="movement"))
        _merge_entity(
            ent,
            label_en=_clean_label(_val(row, "mLabel")),
            inception=_val(row, "inception"),
            country=_clean_label(_val(row, "countryLabel")),
        )
    return list(out.values())


def parse_anchor_rows(sparql_json: dict) -> list[IdeologyAnchor]:
    """坐标锚查询（edges_P1142/party1 姿势）→ P1142 + P1387 锚。"""
    anchors: dict[tuple[str, str, str], IdeologyAnchor] = {}
    for row in _bindings(sparql_json):
        qid = _qid_of(row, "party")
        if not qid:
            continue
        if label := _clean_label(_val(row, "ideologyLabel")):
            key = (qid, "P1142", label)
            anchors.setdefault(
                key, IdeologyAnchor(qid, "P1142", _qid_of(row, "ideology") or None, label)
            )
        if label := _clean_label(_val(row, "alignLabel")):
            key = (qid, "P1387", label)
            anchors.setdefault(
                key, IdeologyAnchor(qid, "P1387", _qid_of(row, "align") or None, label)
            )
    return list(anchors.values())


def parse_edge_rows(sparql_json: dict, pid: str) -> list[RelationEdge]:
    r"""单属性边样本（EDGE_SHAPES 变量名）→ 带 PID/极性/权重的可回溯边。

    缺端点或端点非 QID 的行丢弃（域侦察类 classLabel-only 行自然归零）。
    """
    if pid not in EDGE_SHAPES:
        raise ValueError(f"属性 {pid} 不在极性判据表")
    svar, ovar = EDGE_SHAPES[pid]
    polarity, weight = POLARITY_TABLE[pid]
    out: dict[tuple[str, str, str], RelationEdge] = {}
    for row in _bindings(sparql_json):
        src = _qid_of(row, svar)
        dst = _qid_of(row, ovar)
        if not src or not dst:
            continue
        out.setdefault(
            (src, dst, pid),
            RelationEdge(
                source_id=src,
                target_id=dst,
                pid=pid,
                polarity=polarity,
                weight=weight,
                label_source=_clean_label(_val(row, f"{svar}Label")),
                label_target=_clean_label(_val(row, f"{ovar}Label")),
            ),
        )
    return list(out.values())


# ---------------------------------------------------------------- 落库


WD_DDL = [
    """CREATE TABLE IF NOT EXISTS wd_political_entities (
        qid TEXT NOT NULL,
        kind TEXT NOT NULL,             -- politician / party / event / movement
        label_en TEXT,
        label_zh TEXT,
        birth TEXT,
        death TEXT,
        inception TEXT,
        point_in_time TEXT,             -- 事件时间（P585）
        country TEXT,
        class_label TEXT,               -- 事件子类标签（subclass 闭包命中的类）
        snapshot_version TEXT NOT NULL, -- 快照 id（wd 无版本号 → 摄取时间+查询指纹）
        ingested_at TEXT NOT NULL,
        PRIMARY KEY (qid, snapshot_version)
    )""",
    """CREATE TABLE IF NOT EXISTS wd_political_relations (
        relation_id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_id TEXT NOT NULL,
        target_id TEXT NOT NULL,
        property_pid TEXT NOT NULL,     -- 极性依据（P155/P1365/P361/P463/P102/P5004/P461/P7047/P991）
        kind TEXT NOT NULL,             -- PID 语义名（follows/replaces/part_of/...）
        polarity TEXT,                  -- inheritance / alliance / opposition；P991 为 NULL
        weight REAL NOT NULL,           -- 对立分层取信：P5004 1.0 > P461 0.8 > P7047 0.4
        label_source TEXT,
        label_target TEXT,
        snapshot_version TEXT NOT NULL,
        ingested_at TEXT NOT NULL,
        UNIQUE (source_id, target_id, property_pid, snapshot_version)
    )""",
    """CREATE TABLE IF NOT EXISTS wd_ideology_anchors (
        qid TEXT NOT NULL,
        anchor_pid TEXT NOT NULL,       -- P1142 意识形态 / P1387 政治倾向
        anchor_qid TEXT,                -- 坐标值实体（P1387 连续倾向姿势可缺）
        anchor_label TEXT NOT NULL,
        snapshot_version TEXT NOT NULL,
        ingested_at TEXT NOT NULL,
        PRIMARY KEY (qid, anchor_pid, anchor_label, snapshot_version)
    )""",
]

WD_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_wd_entities_kind ON wd_political_entities(kind)",
    "CREATE INDEX IF NOT EXISTS idx_wd_relations_src ON wd_political_relations(source_id)",
    "CREATE INDEX IF NOT EXISTS idx_wd_relations_dst ON wd_political_relations(target_id)",
    "CREATE INDEX IF NOT EXISTS idx_wd_relations_pid ON wd_political_relations(property_pid)",
    "CREATE INDEX IF NOT EXISTS idx_wd_anchors_qid ON wd_ideology_anchors(qid)",
]


def ensure_tables(conn) -> None:
    """建本模块三张表（快照/台账等共用表由 schema.init 一并确保）。"""
    schema.init(conn)
    for ddl in WD_DDL:
        conn.execute(ddl)
    for idx in WD_INDEXES:
        conn.execute(idx)
    conn.commit()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _chunked(qids: list[str], size: int = 100) -> list[list[str]]:
    return [qids[i : i + size] for i in range(0, len(qids), size)] or [[]]


def ingest_wikidata_politics(
    seed_qids,
    *,
    conn=None,
    client: SparqlClient | None = None,
    limit: int = 200,
    edge_limit: int = 500,
    expand: bool = True,
    snapshot_root: Path | None = None,
) -> dict:
    """政治面摄取：四类实体 + 坐标锚 + 极性边 → 快照 + wd_* 三表 + 台账。

    流程：许可门（wikidata=CC0 confirmed）→ 红线守卫 → 四类实体查询（seed
    邻域一跳）→ 政党/运动坐标锚（P1142/P1387）→ 关系边两遍（seed 一跳；
    expand=True 时对已取实体再拉一遍，把邻域内部边收进来）→ 原始响应整体
    存不可变快照（version=摄取时间+查询指纹，CC0 归属）→ 落库（全量刷新：
    旧快照行清除）→ ingest_log 台账。

    失败口径照 errors.py：单查询失败记 failures 继续跑（status=partial）；
    全部失败 status=failed、reason=unreachable/upstream，绝不编造数据。
    """
    seeds = normalize_qids(seed_qids)
    require_license(SOURCE)  # CC0 confirmed，未过门直接抛 LicenseNotConfirmed
    settings = Settings.from_env()
    client = client or SparqlClient()
    endpoint = getattr(client, "endpoint", settings.wikidata_sparql)
    assert_allowed_url(endpoint)

    owns_conn = conn is None
    conn = conn or settings.connect()
    ensure_tables(conn)

    responses: dict[str, dict] = {}
    failures: list[dict] = []

    def run(name: str, sparql: str) -> dict | None:
        try:
            j = client.query(sparql)
        except SparqlError as exc:
            failures.append({"query": name, "reason": exc.reason, "detail": str(exc)[:200]})
            return None
        responses[name] = j
        return j

    # ---- 四类实体（探针定案姿势）----
    entities: dict[str, PoliticalEntity] = {}
    anchors: dict[tuple[str, str, str], IdeologyAnchor] = {}

    if j := run("person", build_person_query(seeds, limit)):
        for ent in parse_person_rows(j):
            entities[ent.qid] = ent
    if j := run("party", build_party_query(seeds, limit)):
        party_rows, party_anchors = parse_party_rows(j)
        for ent in party_rows:
            entities[ent.qid] = ent
        for a in party_anchors:
            anchors.setdefault((a.qid, a.anchor_pid, a.anchor_label), a)
    if j := run("event", build_event_query(seeds, limit)):
        for ent in parse_event_rows(j):
            entities[ent.qid] = ent
    if j := run("movement", build_movement_query(seeds, limit)):
        for ent in parse_movement_rows(j):
            entities[ent.qid] = ent

    # ---- 坐标锚：P1142/P1387 双通道（政党+运动面）----
    anchor_targets = [q for q, e in entities.items() if e.kind in ("party", "movement")]
    for i, chunk in enumerate(_chunked(anchor_targets)):
        if not chunk:
            break
        if j := run(f"anchor:{i}", build_anchor_query(chunk, edge_limit)):
            for a in parse_anchor_rows(j):
                anchors.setdefault((a.qid, a.anchor_pid, a.anchor_label), a)

    # ---- 关系边（极性判据表；每边记 PID 可回溯）----
    edges: dict[tuple[str, str, str], RelationEdge] = {}

    def pull_edges(anchors_for_pass: list[str], pass_no: int) -> None:
        if not anchors_for_pass:
            return
        for pid in TRACKED_PIDS:
            if pid == "P991":
                sparql = build_winner_query(anchors_for_pass, edge_limit)
            else:
                sparql = build_edge_query(pid, anchors_for_pass, edge_limit)
            if j := run(f"edge:{pid}:{pass_no}", sparql):
                for e in parse_edge_rows(j, pid):
                    edges.setdefault((e.source_id, e.target_id, e.pid), e)

    pull_edges(seeds, 1)
    if expand:
        second_hop = [q for q in entities if q not in seeds]
        pull_edges(second_hop, 2)

    # ---- 全失败：不落库不编造，台账记 failed ----
    if not responses:
        reason = failures[0]["reason"] if failures else UPSTREAM
        as_of = _now_iso()
        conn.execute(
            "INSERT OR REPLACE INTO ingest_log (source, status, detail, as_of) VALUES (?,?,?,?)",
            (SOURCE, "failed", json.dumps({"failures": failures}, ensure_ascii=False), as_of),
        )
        conn.commit()
        if owns_conn:
            conn.close()
        return {
            "status": "failed",
            "reason": reason,
            "seeds": seeds,
            "snapshot_id": None,
            "snapshot_version": None,
            "counts": {},
            "failures": failures,
        }

    # ---- 快照版本化：摄取时间 + 查询指纹；原始响应整体冻结，CC0 归属 ----
    now_dt = datetime.now(timezone.utc)
    fetched_at = now_dt.isoformat(timespec="microseconds")
    version = f"{now_dt.strftime('%Y%m%dT%H%M%SZ')}+fp{query_fingerprint(seeds, limit, edge_limit)[:12]}"
    content = json.dumps(responses, ensure_ascii=False, sort_keys=True).encode("utf-8")
    manifest = snapshot.store(
        DATASET,
        version,
        content,
        source_url=endpoint,
        attribution=ATTRIBUTIONS[SOURCE],  # Wikidata (CC0)
        filename="responses.json",
        fetched_at=fetched_at,
        root=snapshot_root,
    )
    snapshot.record(conn, manifest)
    sid = manifest.snapshot_id

    # ---- 落库（当前快照全量刷新：先写新，再清旧）----
    for ent in entities.values():
        conn.execute(
            "INSERT OR REPLACE INTO wd_political_entities (qid, kind, label_en, label_zh,"
            " birth, death, inception, point_in_time, country, class_label, snapshot_version,"
            " ingested_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                ent.qid, ent.kind, ent.label_en, ent.label_zh, ent.birth, ent.death,
                ent.inception, ent.point_in_time, ent.country, ent.class_label, sid, fetched_at,
            ),
        )
    for e in edges.values():
        conn.execute(
            "INSERT OR IGNORE INTO wd_political_relations (source_id, target_id, property_pid,"
            " kind, polarity, weight, label_source, label_target, snapshot_version, ingested_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                e.source_id, e.target_id, e.pid, PID_KIND[e.pid], e.polarity, e.weight,
                e.label_source, e.label_target, sid, fetched_at,
            ),
        )
    for a in anchors.values():
        conn.execute(
            "INSERT OR REPLACE INTO wd_ideology_anchors (qid, anchor_pid, anchor_qid,"
            " anchor_label, snapshot_version, ingested_at) VALUES (?,?,?,?,?,?)",
            (a.qid, a.anchor_pid, a.anchor_qid, a.anchor_label, sid, fetched_at),
        )
    for table in ("wd_political_entities", "wd_political_relations", "wd_ideology_anchors"):
        conn.execute(f"DELETE FROM {table} WHERE snapshot_version != ?", (sid,))

    status = "ok" if not failures else "partial"
    by_kind = {k: sum(1 for e in entities.values() if e.kind == k) for k in ENTITY_KINDS}
    by_polarity: dict[str, int] = {}
    for e in edges.values():
        key = e.polarity or "none"
        by_polarity[key] = by_polarity.get(key, 0) + 1
    counts = {
        "entities": len(entities),
        "by_kind": by_kind,
        "relations": len(edges),
        "by_polarity": by_polarity,
        "anchors": len(anchors),
        "queries_ok": len(responses),
        "queries_failed": len(failures),
    }
    conn.execute(
        "INSERT OR REPLACE INTO ingest_log (source, status, detail, as_of) VALUES (?,?,?,?)",
        (
            SOURCE,
            status,
            json.dumps({"snapshot_id": sid, "counts": counts, "failures": failures}, ensure_ascii=False),
            fetched_at,
        ),
    )
    conn.commit()
    if owns_conn:
        conn.close()
    return {
        "status": status,
        "reason": failures[0]["reason"] if failures else None,
        "seeds": seeds,
        "snapshot_id": sid,
        "snapshot_version": version,
        "counts": counts,
        "failures": failures,
    }


# ---------------------------------------------------------------- 查询面（离线读库）


def _validate_qid(qid: str) -> str:
    qid = str(qid).strip()
    if not QID_RE.fullmatch(qid):
        raise ValueError(f"非法 QID：{qid!r}（形如 Q17427）")
    return qid


def _latest_snapshot_id(conn) -> str | None:
    manifest = snapshot.latest(conn, DATASET)
    return manifest["snapshot_id"] if manifest else None


def _provenance_for(qid: str, as_of: str, sid: str) -> dict:
    return provenance(SOURCE, stable_id=qid, as_of=as_of[:10], snapshot_version=sid)


def query_entity(conn, qid: str) -> dict:
    """实体档案：四类政治面实体 + provenance；未摄取返回空结果 + not_ingested。"""
    qid = _validate_qid(qid)
    sid = _latest_snapshot_id(conn)
    row = None
    if sid:
        row = conn.execute(
            "SELECT * FROM wd_political_entities WHERE qid=? AND snapshot_version=?", (qid, sid)
        ).fetchone()
    if row is None:
        return empty_result(
            NOT_INGESTED,
            hint=(
                f"{qid} 不在 wikidata 政治面当前快照（{sid or '尚无快照'}）。"
                "用 ingest_wikidata_politics 以该 QID 为种子（或其邻域种子）摄取。"
            ),
        )
    d = dict(row)
    d["provenance"] = _provenance_for(qid, d["ingested_at"], sid)
    return {"results": [d]}


def query_relations(conn, qid: str, polarity: str | None = None) -> dict:
    """关系边（双向：qid 作 source 或 target），polarity 过滤（四档 token）。

    - polarity="critique"：恒空——批判档 WD 缺失（CRITIQUE_GAP），其他源承载。
    - 实体未摄取 / 该极性无边：空结果 + not_ingested + 说明性 hint。
    """
    qid = _validate_qid(qid)
    if polarity is not None:
        if polarity not in POLARITY_TIERS:
            raise ValueError(f"未知极性 {polarity!r}（合法值：{POLARITY_TIERS}）")
        if polarity == POLARITY_CRITIQUE:
            return empty_result(NOT_INGESTED, hint=CRITIQUE_GAP)
    sid = _latest_snapshot_id(conn)
    ingested = False
    if sid:
        ingested = (
            conn.execute(
                "SELECT 1 FROM wd_political_entities WHERE qid=? AND snapshot_version=?",
                (qid, sid),
            ).fetchone()
            is not None
        )
    if not ingested:
        return empty_result(
            NOT_INGESTED,
            hint=(
                f"{qid} 不在 wikidata 政治面当前快照（{sid or '尚无快照'}），其关系边无从谈起。"
                "先用 ingest_wikidata_politics 摄取该 QID 的种子邻域。"
            ),
        )
    sql = (
        "SELECT * FROM wd_political_relations"
        " WHERE snapshot_version=? AND (source_id=? OR target_id=?)"
    )
    params: list = [sid, qid, qid]
    if polarity is not None:
        sql += " AND polarity=?"
        params.append(polarity)
    sql += " ORDER BY weight DESC, property_pid, source_id"
    rows = [dict(r) for r in conn.execute(sql, params)]
    if not rows:
        scope = f"极性 {polarity} 的" if polarity else ""
        return empty_result(
            NOT_INGESTED,
            hint=f"{qid} 已摄取，但当前快照没有其{scope}关系边（种子邻域一跳/二跳内无边）。",
        )
    for r in rows:
        r["direction"] = "out" if r["source_id"] == qid else "in"
        r["provenance"] = _provenance_for(qid, r["ingested_at"], sid)
    return {"results": rows}


def query_ideology_anchor(conn, qid: str) -> dict:
    """坐标锚：P1142 意识形态 / P1387 政治倾向（政党与运动面）。"""
    qid = _validate_qid(qid)
    sid = _latest_snapshot_id(conn)
    rows = []
    if sid:
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM wd_ideology_anchors WHERE qid=? AND snapshot_version=?"
                " ORDER BY anchor_pid, anchor_label",
                (qid, sid),
            )
        ]
    if not rows:
        return empty_result(
            NOT_INGESTED,
            hint=(
                f"{qid} 无坐标锚（P1142 意识形态 / P1387 政治倾向）。"
                "可能是未摄取（先 ingest_wikidata_politics），或该实体在 WD 本就无坐标值；"
                "锚主要落在政党/运动面（探针使用量 15,257 / 3,804）。"
            ),
        )
    for r in rows:
        r["provenance"] = _provenance_for(qid, r["ingested_at"], sid)
    return {"results": rows}


__all__ = [
    "CRITIQUE_GAP",
    "DATASET",
    "ENTITY_KINDS",
    "EDGE_SHAPES",
    "POLARITY_TABLE",
    "POLARITY_TIERS",
    "TRACKED_PIDS",
    "IdeologyAnchor",
    "PoliticalEntity",
    "RelationEdge",
    "SparqlClient",
    "SparqlError",
    "build_anchor_query",
    "build_edge_query",
    "build_event_query",
    "build_movement_query",
    "build_party_query",
    "build_person_query",
    "build_winner_query",
    "ensure_tables",
    "ingest_wikidata_politics",
    "parse_anchor_rows",
    "parse_edge_rows",
    "parse_event_rows",
    "parse_movement_rows",
    "parse_party_rows",
    "parse_person_rows",
    "query_entity",
    "query_fingerprint",
    "query_ideology_anchor",
    "query_relations",
]
