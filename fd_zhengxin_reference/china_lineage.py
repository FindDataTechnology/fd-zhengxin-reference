"""中国线种子加载器 + 近现代思潮文献索引层。

两个入口（都走快照版本化、都幂等、都给种子行加可区分标记）：

- `load_china_seed`：读 `seed/china_seed.json`（先秦诸子 curated 种子谱系，人工
  提炼的结构化数据，非百科语料）入参照系库。种子条目与语料条目在 schema 上
  可区分：currents 走 `seed:china/` 前缀命名空间（`is_seed_current`），
  concepts 行的维基引用 URL 带 `#china-seed` 片段标记（`is_seed_concept_url`），
  relations 边的 source/target 即种子命名空间 id。**种子不造数值坐标**：
  currents.coordinates 一律 NULL（curated 谱系只有定性关系，无数值轴）。

- `load_modern_index`：把 docs/china-modern-sources.md 固化的五思潮（维新变法/
  辛亥革命/自由主义/文化保守主义/社会主义论战）文献索引（思潮 → 文献条目：
  标题/作者/年代/维基文库或 Commons URL/形态）以不可变快照入库，五思潮本体
  落 currents（kind="current"，同样无数值坐标），文献条目本体在快照 JSON 里。

许可口径：种子是编辑产出的坐标数据（底稿中文维基百科条目群，CC BY-SA 4.0
以条目名署名；QID 对齐 Wikidata CC0；ctext 只链不抄），**不是外部语料摄取**，
因此不触发 china-line 语料许可门（该门管的是语料全文层）。
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from . import schema, snapshot

SEED_ENV = "FDZX_REF_CHINA_SEED"
SEED_FILE = "china_seed.json"
SEED_DATASET = "china-seed"
MODERN_DATASET = "china-modern-index"

# is_seed 标记的三处落点（种子条目 vs 语料条目的 schema 级区分）
SEED_ID_PREFIX = "seed:china/"
MODERN_ID_PREFIX = "modern:china/"
SEED_CONCEPT_MARKER = "#china-seed"
WIKIPEDIA_URL_PREFIX = "https://zh.wikipedia.org/wiki/"

POLARITY_MAP = {"继承": "positive", "批判": "critical"}  # 种子极性 → relations.polarity
KIND_MAP = {  # 种子边 kind → relations.kind（开放词表：influence/critique/debate）
    "师承": "influence", "传承": "influence", "家学": "influence",
    "继承": "influence", "并称": "influence",
    "评价": "critique", "论战": "debate", "批判": "critique",
}

SEED_ATTRIBUTION = (
    "先秦诸子种子谱系（curated 结构化数据）：中文维基百科条目群底稿"
    "（编辑版 CC BY-SA 4.0，条目名署名）+ Wikidata QID 对齐（CC0）"
    "+ ctext.org 篇目锚（只链不抄）"
)
MODERN_ATTRIBUTION = (
    "中国近现代思潮文献索引：维基文库（底本公版，编辑版 CC BY-SA 4.0）"
    "+ 维基共享国图 NLC 原刊扫描（Public domain, Credit: National Library of China）；"
    "索引只存条目元数据与链接，不存正文"
)


class SeedInvalid(RuntimeError):
    """种子 JSON 违反结构契约（粒度区间/枚举/双锚/引用完整性）。"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_seed_path() -> Path:
    override = os.environ.get(SEED_ENV)
    if override:
        return Path(override)
    return Path(__file__).resolve().parent.parent / "seed" / SEED_FILE


def load_seed_data(seed_path: Path | str | None = None) -> dict:
    path = Path(seed_path) if seed_path else default_seed_path()
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- 校验（纯函数）

_QID_RE = "Q[1-9][0-9]*"


def _is_qid(value) -> bool:
    v = value or ""
    return v == "" or (v.startswith("Q") and v[1:].isdigit() and v != "Q0")


def validate_seed(data: dict) -> list[str]:
    """结构契约校验，返回违例清单（空 = 通过）。粒度区间取自 meta.granularity。"""
    bad: list[str] = []
    meta = data.get("meta") or {}
    gran = meta.get("granularity") or {}
    enums = meta.get("enums") or {}
    polarity_ok, kind_ok = set(enums.get("polarity", [])), set(enums.get("kind", []))
    if not polarity_ok or not kind_ok:
        bad.append("meta.enums 缺 polarity/kind 枚举")

    schools = data.get("schools") or []
    figures = data.get("figures") or []
    concepts = data.get("concepts") or []
    edges = data.get("edges") or []

    # 粒度区间（docs/china-modern-sources.md 先秦定案）
    for key, items in (("schools", schools), ("figures", figures), ("concepts", concepts), ("edges", edges)):
        lo, hi = gran.get(key, (0, 10**9))
        if not lo <= len(items) <= hi:
            bad.append(f"粒度越界：{key}={len(items)}（区间 {lo}–{hi}）")

    school_ids = [s.get("id") for s in schools]
    if len(school_ids) != len(set(school_ids)):
        bad.append("schools.id 重复")
    figure_ids = [f.get("id") for f in figures]
    if len(figure_ids) != len(set(figure_ids)):
        bad.append("figures.id 重复")
    concept_ids = [c.get("id") for c in concepts]
    if len(concept_ids) != len(set(concept_ids)):
        bad.append("concepts.id 重复")

    for s in schools:
        if not s.get("source"):
            bad.append(f"school {s.get('id')} 缺 source（zh.wikipedia 条目名）")
        if not _is_qid(s.get("qid", None)):
            bad.append(f"school {s.get('id')} qid 非法（留空或 Q 数字）")
    for f in figures:
        if f.get("school") not in school_ids:
            bad.append(f"figure {f.get('id')} 引用未知学派 {f.get('school')!r}")
        if not f.get("source"):
            bad.append(f"figure {f.get('id')} 缺 source")
        if not _is_qid(f.get("qid", None)):
            bad.append(f"figure {f.get('id')} qid 非法")
        anchor = (f.get("anchors") or {}).get("ctext") or {}
        if not (anchor.get("work") and anchor.get("chapter")):
            bad.append(f"figure {f.get('id')} 缺 ctext 篇目锚（work+chapter）")
    # 每派人物数：通说下限 3；阴阳家可考人物少（meta.notes 已记），允许 2
    per_school: dict[str, int] = {}
    for f in figures:
        per_school[f.get("school")] = per_school.get(f.get("school"), 0) + 1
    for sid, n in per_school.items():
        if not 2 <= n <= 8:
            bad.append(f"school {sid} 人物数 {n} 越界（2–8；阴阳家特例下限 2）")

    for c in concepts:
        anchors = c.get("anchors") or {}
        if not anchors.get("wikipedia"):
            bad.append(f"concept {c.get('id')} 缺维基锚（wikipedia 条目名）")
        ctext = anchors.get("ctext") or {}
        if not (ctext.get("work") and ctext.get("chapter")):
            bad.append(f"concept {c.get('id')} 缺 ctext 篇目锚（双锚不完备）")
        if not c.get("definition"):
            bad.append(f"concept {c.get('id')} 缺一句话通说界定")
        if not c.get("source"):
            bad.append(f"concept {c.get('id')} 缺 source")
        if not _is_qid(c.get("qid", None)):
            bad.append(f"concept {c.get('id')} qid 非法")
        for fid in c.get("figures") or []:
            if fid not in figure_ids:
                bad.append(f"concept {c.get('id')} 引用未知人物 {fid!r}")

    for i, e in enumerate(edges):
        if e.get("source") not in figure_ids or e.get("target") not in figure_ids:
            bad.append(f"edge[{i}] 引用未知人物 {e.get('source')!r}->{e.get('target')!r}")
        if e.get("polarity") not in polarity_ok:
            bad.append(f"edge[{i}] 极性 {e.get('polarity')!r} 不在枚举内")
        if e.get("kind") not in kind_ok:
            bad.append(f"edge[{i}] kind {e.get('kind')!r} 不在枚举内")
        if not e.get("evidence"):
            bad.append(f"edge[{i}] 缺出典证据（evidence）")
        anchor = (e.get("anchors") or {}).get("ctext") or {}
        if not (anchor.get("work") and anchor.get("chapter")):
            bad.append(f"edge[{i}] 缺 ctext 篇目锚（边必须带出典篇目）")

    # 种子纪律：不造数值坐标——种子数据里不允许出现任何坐标字段
    for section in ("schools", "figures", "concepts", "edges"):
        for item in data.get(section) or []:
            if "coordinates" in item or "axes" in item:
                bad.append(f"{section} {item.get('id')} 含坐标字段（种子不造数值坐标）")
    return bad


# ---------------------------------------------------------------- is_seed 标记

def is_seed_current(current_id: str) -> bool:
    """currents 行是否种子条目（命名空间前缀即标记）。"""
    return current_id.startswith(SEED_ID_PREFIX)


def is_modern_current(current_id: str) -> bool:
    return current_id.startswith(MODERN_ID_PREFIX)


def is_seed_concept_url(url: str) -> bool:
    """concepts 行是否种子条目（维基引用 URL 的片段标记）。"""
    return url.endswith(SEED_CONCEPT_MARKER)


def concept_citation_url(wiki_title: str) -> str:
    """种子概念的维基引用 URL（URL-encode 条目名 + 种子片段标记；仅作署名/引用，
    绝不作为请求目标）。"""
    return f"{WIKIPEDIA_URL_PREFIX}{quote(wiki_title, safe='')}{SEED_CONCEPT_MARKER}"


# ---------------------------------------------------------------- 先秦种子入库

def _seed_current_id(kind: str, item_id: str) -> str:
    return f"{SEED_ID_PREFIX}{kind}/{item_id}"


def load_china_seed(
    conn: sqlite3.Connection,
    *,
    seed_path: Path | str | None = None,
    snapshot_root: Path | None = None,
) -> dict:
    """读 seed/china_seed.json 入库（幂等：同命名空间整批覆写，快照内容寻址）。

    落点：学派/人物 → currents（kind=school/thinker，coordinates 恒 NULL）；
    概念 → concepts（term=标签，url=维基引用 URL 带 #china-seed 标记，
    snippet=一句话通说界定+原典锚）；师承批判边 → relations（kind/polarity 按
    开放词表映射）；种子 JSON 本体 → 不可变快照（行带 snapshot_version）。
    """
    path = Path(seed_path) if seed_path else default_seed_path()
    data = load_seed_data(path)
    violations = validate_seed(data)
    if violations:
        raise SeedInvalid("；".join(violations[:8]) + (f"（等 {len(violations)} 项）" if len(violations) > 8 else ""))

    manifest = snapshot.store(
        SEED_DATASET,
        version=str((data.get("meta") or {}).get("version", "0")),
        content=path.read_bytes(),
        source_url=None,
        attribution=SEED_ATTRIBUTION,
        probe_status="available",
        filename=SEED_FILE,
        fetched_at=str((data.get("meta") or {}).get("curated_at", "")) or None,
        root=snapshot_root,
    )
    snapshot.record(conn, manifest)
    sid, now = manifest.snapshot_id, _now()
    schools, figures = data["schools"], data["figures"]
    concepts, edges = data["concepts"], data["edges"]

    try:
        conn.execute("BEGIN")
        # 覆写式幂等：先清本命名空间旧行（currents/concepts/relations）
        conn.execute("DELETE FROM currents WHERE current_id LIKE ?", (SEED_ID_PREFIX + "%",))
        conn.execute("DELETE FROM concepts WHERE url LIKE ?", ("%" + SEED_CONCEPT_MARKER,))
        conn.execute("DELETE FROM relations WHERE source_id LIKE ?", (SEED_ID_PREFIX + "%",))

        conn.executemany(
            "INSERT INTO currents (current_id, label, label_en, label_zh, kind, coordinates,"
            " ingested_at, snapshot_version) VALUES (?,?,?,?,?,?,?,?)",
            [
                (_seed_current_id("school", s["id"]), s["label"], None, s["label"], "school", None, now, sid)
                for s in schools
            ]
            + [
                (_seed_current_id("thinker", f["id"]), f["label"], None, f["label"], "thinker", None, now, sid)
                for f in figures
            ],
        )
        conn.executemany(
            "INSERT INTO concepts (term, title, url, snippet, retrieved_at) VALUES (?,?,?,?,?)",
            [
                (
                    c["label"],
                    c["label"],
                    concept_citation_url(c["anchors"]["wikipedia"]),
                    f"{c['definition']}｜原典：《{c['anchors']['ctext']['work']}·{c['anchors']['ctext']['chapter']}》（ctext 只链不抄）",
                    now,
                )
                for c in concepts
            ],
        )
        conn.executemany(
            "INSERT INTO relations (source_id, target_id, kind, polarity, weight, ingested_at,"
            " snapshot_version) VALUES (?,?,?,?,?,?,?)",
            [
                (
                    _seed_current_id("thinker", e["source"]),
                    _seed_current_id("thinker", e["target"]),
                    KIND_MAP.get(e["kind"], e["kind"]),
                    POLARITY_MAP.get(e["polarity"]),
                    None,
                    now,
                    sid,
                )
                for e in edges
            ],
        )
        conn.execute(
            "INSERT OR REPLACE INTO ingest_log (source, status, detail, as_of) VALUES (?,?,?,?)",
            (
                SEED_DATASET,
                "ok",
                json.dumps(
                    {
                        "schools": len(schools), "figures": len(figures),
                        "concepts": len(concepts), "edges": len(edges),
                        "snapshot_id": sid,
                    },
                    ensure_ascii=False,
                ),
                now,
            ),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {
        "schools": len(schools),
        "figures": len(figures),
        "concepts": len(concepts),
        "edges": len(edges),
        "snapshot_id": sid,
        "is_seed": True,
    }


# ---------------------------------------------------------------- 近现代文献索引层

# docs/china-modern-sources.md（2026-10-23 实测）五思潮 URL 清单的固化索引。
# 形态枚举：原文全文 / 原文全文合集 / 期刊汇集页 / 原刊扫描索引 / 原书扫描。
MODERN_CURRENTS: list[dict] = [
    {
        "id": "weixin-reform",
        "label": "维新变法思潮",
        "period": "1895–1898 前后",
        "summary": "甲午后以变法图强为主轴的改良思潮（康梁变法、公车上书、新民说）。",
        "documents": [
            {"title": "公车上书", "author": "康有为", "year": "1895", "url": "https://zh.wikisource.org/wiki/公车上书", "form": "原文全文", "note": "重定向至《上清帝第二书》"},
            {"title": "變法通議", "author": "梁启超", "year": "1896", "url": "https://zh.wikisource.org/wiki/變法通議", "form": "原文全文"},
            {"title": "新民說", "author": "梁启超", "year": "1902", "url": "https://zh.wikisource.org/wiki/新民說", "form": "原文全文"},
            {"title": "孔子改制考", "author": "康有为", "year": "1897", "url": "https://zh.wikisource.org/wiki/孔子改制考", "form": "原文全文"},
            {"title": "天演論", "author": "严复（译述）", "year": "1898", "url": "https://zh.wikisource.org/wiki/天演論", "form": "原文全文"}
        ],
    },
    {
        "id": "xinhai-revolution",
        "label": "辛亥革命思潮",
        "period": "1900s–1911",
        "summary": "以排满革命、建立民国为目标的革命思潮（与维新派论战的核心文献群）。",
        "documents": [
            {"title": "革命軍", "author": "邹容", "year": "1903", "url": "https://zh.wikisource.org/wiki/革命軍", "form": "原文全文"},
            {"title": "駁康有為論革命書", "author": "章太炎", "year": "1903", "url": "https://zh.wikisource.org/wiki/駁康有為論革命書", "form": "原文全文", "note": "革命/维新两线互驳的核心文献"},
            {"title": "猛回頭", "author": "陈天华", "year": "1903", "url": "https://zh.wikisource.org/wiki/猛回頭", "form": "原文全文"},
            {"title": "民報發刊詞", "author": "孙文", "year": "1905", "url": "https://zh.wikisource.org/wiki/民報發刊詞", "form": "原文全文"},
            {"title": "三民主義", "author": "孙文", "year": "1924", "url": "https://zh.wikisource.org/wiki/三民主義", "form": "原文全文"},
            {"title": "民報", "author": None, "year": "1905–1910", "url": "https://zh.wikisource.org/wiki/民報", "form": "期刊汇集页", "note": "散篇入口；原刊合订扫描未定位（诚实缺口）"}
        ],
    },
    {
        "id": "liberalism",
        "label": "自由主义思潮",
        "period": "1917–1930s",
        "summary": "新文化—新月系的自由主义论战文献（问题与主义、文学革命、人权论战）。",
        "documents": [
            {"title": "文學改良芻議", "author": "胡适", "year": "1917", "url": "https://zh.wikisource.org/wiki/文學改良芻議", "form": "原文全文"},
            {"title": "多研究些问题，少谈些“主义”", "author": "胡适", "year": "1919", "url": "https://zh.wikisource.org/wiki/多研究些问题，少谈些“主义”", "form": "原文全文", "note": "问题与主义之争首回合"},
            {"title": "人權與約法", "author": "胡适", "year": "1929", "url": "https://zh.wikisource.org/wiki/人權與約法", "form": "原文全文", "note": "新月人权论战核心"},
            {"title": "新青年", "author": None, "year": "1915–1926", "url": "https://zh.wikisource.org/wiki/新青年", "form": "期刊汇集页", "note": "散篇入口；作者索引见 Author:胡適（724 条）"}
        ],
    },
    {
        "id": "cultural-conservatism",
        "label": "文化保守主义（学衡派/新儒家早期）",
        "period": "1921–1933",
        "summary": "以昌明国粹、融化新知为纲的文化保守主义（学衡派）；扫描层为主源。",
        "documents": [
            {"title": "學衡（期刊 1–79 期）", "author": "學衡杂志社（吴宓、柳诒徵等）", "year": "1922–1933", "url": "https://commons.wikimedia.org/wiki/Category:學衡", "form": "原刊扫描索引", "note": "Commons 国图 NLC404 系列 79 期完整覆盖（86 成员）；v1 作目录级索引+选篇 OCR 候选"},
            {"title": "東西文化及其哲學", "author": "梁漱溟", "year": "1921", "url": "https://commons.wikimedia.org/wiki/File:NLC511-023031404038080-40119_東西文化及其哲學.pdf", "form": "原书扫描", "note": "国图 NLC511 系列扫描（Public domain）；维基文库仅条目壳（文本层非公版）"}
        ],
    },
    {
        "id": "socialism-debate",
        "label": "社会主义论战",
        "period": "1919–1921",
        "summary": "问题与主义之争与关于社会主义的讨论（论战回合完整在库）。",
        "documents": [
            {"title": "多研究些问题，少谈些“主义”", "author": "胡适", "year": "1919", "url": "https://zh.wikisource.org/wiki/多研究些问题，少谈些“主义”", "form": "原文全文", "cross_ref": "思潮「自由主义」同目（论战首回合）"},
            {"title": "問題與主義", "author": "蓝公武", "year": "1919", "url": "https://zh.wikisource.org/wiki/問題與主義_(藍公武)", "form": "原文全文"},
            {"title": "再論問題與主義", "author": "李大钊", "year": "1919", "url": "https://zh.wikisource.org/wiki/再論問題與主義", "form": "原文全文"},
            {"title": "三論問題與主義", "author": "李大钊", "year": "1919", "url": "https://zh.wikisource.org/wiki/三論問題與主義", "form": "原文全文", "note": "另胡适《四论问题与主义》亦在库（四个回合+蓝公武一篇齐）"},
            {"title": "我的馬克思主義觀", "author": "李大钊", "year": "1919", "url": "https://zh.wikisource.org/wiki/我的馬克思主義觀", "form": "原文全文"},
            {"title": "關於社會主義的討論", "author": "《新青年》合集（含张东荪通信）", "year": "1920–1921", "url": "https://zh.wikisource.org/wiki/關於社會主義的討論", "form": "原文全文合集"},
            {"title": "由經濟上解釋中國近代思想變動的原因", "author": "李大钊", "year": "1920", "url": "https://zh.wikisource.org/wiki/由經濟上解釋中國近代思想變動的原因", "form": "原文全文"}
        ],
    },
]


def modern_index() -> list[dict]:
    """五思潮文献索引数据（纯数据访问器，供快照与查询面共用）。"""
    return MODERN_CURRENTS


def load_modern_index(
    conn: sqlite3.Connection,
    *,
    snapshot_root: Path | None = None,
) -> dict:
    """近现代思潮文献索引入库（快照版本化 + 五思潮落 currents；幂等）。

    文献条目本体进不可变快照 JSON（内容寻址），五思潮本体进 currents
    （kind="current"、无数值坐标），台账记 ingest_log。
    """
    payload = {
        "meta": {
            "dataset": MODERN_DATASET,
            "curated_at": "2026-10-23",
            "source_doc": "docs/china-modern-sources.md（五思潮源清单实测）",
            "attribution": MODERN_ATTRIBUTION,
            "known_gaps": [
                "期刊原刊级完整性：《新青年》《民報》合订本公开扫描未定位",
                "新儒家早期熊十力无公开可摄取版；吴宓/梅光迪文集无公开电子版",
                "张东荪专文未见独立条目（论战参与已由合集部分覆盖）"
            ],
        },
        "currents": MODERN_CURRENTS,
    }
    content = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    manifest = snapshot.store(
        MODERN_DATASET,
        version="2026-10-23",
        content=content,
        source_url="docs/china-modern-sources.md",
        attribution=MODERN_ATTRIBUTION,
        probe_status="available",
        filename="china_modern_index.json",
        root=snapshot_root,
    )
    snapshot.record(conn, manifest)
    sid, now = manifest.snapshot_id, _now()

    try:
        conn.execute("BEGIN")
        conn.execute("DELETE FROM currents WHERE current_id LIKE ?", (MODERN_ID_PREFIX + "%",))
        conn.executemany(
            "INSERT INTO currents (current_id, label, label_en, label_zh, kind, coordinates,"
            " ingested_at, snapshot_version) VALUES (?,?,?,?,?,?,?,?)",
            [
                (
                    f"{MODERN_ID_PREFIX}current/{c['id']}",
                    c["label"], None, c["label"], "current", None, now, sid,
                )
                for c in MODERN_CURRENTS
            ],
        )
        conn.execute(
            "INSERT OR REPLACE INTO ingest_log (source, status, detail, as_of) VALUES (?,?,?,?)",
            (
                MODERN_DATASET,
                "ok",
                json.dumps(
                    {"currents": len(MODERN_CURRENTS), "documents": sum(len(c["documents"]) for c in MODERN_CURRENTS), "snapshot_id": sid},
                    ensure_ascii=False,
                ),
                now,
            ),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {
        "currents": len(MODERN_CURRENTS),
        "documents": sum(len(c["documents"]) for c in MODERN_CURRENTS),
        "snapshot_id": sid,
    }


# ---------------------------------------------------------------- 查询辅助

def list_seed_currents(conn: sqlite3.Connection) -> list[dict]:
    """列种子谱系条目（学派/人物），is_seed=True、coordinates 恒 NULL。"""
    rows = conn.execute(
        "SELECT current_id, label, kind, coordinates, snapshot_version FROM currents"
        " WHERE current_id LIKE ? ORDER BY current_id",
        (SEED_ID_PREFIX + "%",),
    ).fetchall()
    return [
        {
            "current_id": r["current_id"],
            "label": r["label"],
            "kind": r["kind"],
            "is_seed": True,
            "coordinates": r["coordinates"],
            "snapshot_version": r["snapshot_version"],
        }
        for r in rows
    ]


def list_seed_relations(conn: sqlite3.Connection) -> list[dict]:
    """列种子师承批判边（映射后的 relations 行）。"""
    rows = conn.execute(
        "SELECT source_id, target_id, kind, polarity, snapshot_version FROM relations"
        " WHERE source_id LIKE ? ORDER BY rowid",
        (SEED_ID_PREFIX + "%",),
    ).fetchall()
    return [{k: r[k] for k in r.keys()} for r in rows]
