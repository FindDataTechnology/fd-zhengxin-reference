"""SEP 政治哲学概念层客户端（只链不抄）。

通道纪律照 fd-zhesi-reference/fd_zhesi_reference/sep.py：唯一网络调用是搜索端点
`GET /search/searcher.py?query=<term>`（UA/超时 8s/限速 1.5s 同实测口径），
解析只取 标题 + 摘要片段（结果页自带的引用性短片段）+ `/entries/<slug>/` 链接，
绝不请求词条正文页。本模块内不存在任何抓取 `/entries/` 正文的代码路径。

内置 docs/sep-coverage.md（2026-10-23，23/23 命中实测）的产物：
- `CANONICAL`：26 个政治思想核心词（20 主表词 + 中国线 3 词 + 补充验证 3 词）
  的「查询词 → 规范条目」表；
- `HARD_MAPPINGS`：直查会排名漂移的词的纠偏映射（property 顶部误召
  Intellectual Property、social contract 主题拆两条目、liberty/legitimacy/
  utilitarianism 无同名条目、Confucian political thought 无专条靠伞形条目承载）；
- `seed_core_cache`：把核心词白名单物化为 concepts 缓存（离线可用，幂等）。

失败口径照 errors.py taxonomy：unreachable（网络层）/ upstream（非 200 或
解析不出可用结果）；空结果必带 reason，绝不编造。
"""

from __future__ import annotations

import asyncio
import html as html_lib
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, unquote, urlparse

import httpx

from .china_lineage import SEED_CONCEPT_MARKER  # concepts 表共享面：种子行不由 SEP 层读写
from .config import Settings
from .errors import UPSTREAM, UNREACHABLE, empty_result
from .provenance import provenance
from . import schema

USER_AGENT = "fd-zhengxin-reference/0.1"  # 照 zhesi 线 <product>/0.1 命名习惯
DEFAULT_TIMEOUT = 8.0  # 超时 8s → unreachable（spec 失败口径）
SEARCH_PATH = "/search/searcher.py"
DEFAULT_BASE = "https://plato.stanford.edu"
MIN_REQUEST_INTERVAL_S = 1.5  # docs/sep-coverage.md 实测请求节奏
DEFAULT_LIMIT = 5
CACHE_TTL_DAYS = 30
MAX_SNIPPET_CHARS = 200  # 短引用上限（只链不抄的存储面守卫）
MAX_TITLE_CHARS = 200
VERIFIED_AT = "2026-10-23"  # docs/sep-coverage.md 实测日期（curated 锚点的 as_of）


class SepUnreachable(Exception):
    """SEP 不可达（网络层：DNS/连接/超时/TLS）。映射 reason=unreachable。"""


class SepUpstream(Exception):
    """SEP 可达但响应异常（非 200）。映射 reason=upstream。"""


@dataclass(frozen=True)
class SepHit:
    """一条引用性结果：标题 + 摘要片段 + entries 链接。不存正文。"""

    title: str
    url: str
    snippet: str

    def as_dict(self) -> dict:
        return {"title": self.title, "url": self.url, "snippet": self.snippet}


# ---------------------------------------------------------------- 硬映射与白名单

def _entry(title: str, slug: str, note: str = "") -> dict:
    return {"title": title, "slug": slug, "note": note}


# docs/sep-coverage.md §一/§二/补充验证：查询词 → 规范条目（26 词）。
# hard_map=True 的词直查会排名漂移或主题拆分，query_concept 按映射钉序纠偏。
CANONICAL: dict[str, dict] = {
    # 20 个政治思想核心词
    "justice": {"entries": [_entry("Justice", "justice")], "hard_map": False},
    "sovereignty": {"entries": [_entry("Sovereignty", "sovereignty")], "hard_map": False},
    "liberty": {
        "entries": [_entry(
            "Positive and Negative Liberty", "liberty-positive-negative",
            "SEP 无独立 Liberty 条目，此即规范条目（Berlin 两分框架）",
        )],
        "hard_map": True,
    },
    "democracy": {"entries": [_entry("Democracy", "democracy")], "hard_map": False},
    "legitimacy": {
        "entries": [_entry("Political Legitimacy", "legitimacy", "规范条目名带 Political 前缀")],
        "hard_map": True,
    },
    "authority": {"entries": [_entry("Authority", "authority")], "hard_map": False},
    "rights": {"entries": [_entry("Rights", "rights")], "hard_map": False},
    "equality": {"entries": [_entry("Equality", "equality")], "hard_map": False},
    "political representation": {
        "entries": [_entry("Political Representation", "political-representation")],
        "hard_map": False,
    },
    "constitutionalism": {"entries": [_entry("Constitutionalism", "constitutionalism")], "hard_map": False},
    "revolution": {"entries": [_entry("Revolution", "revolution")], "hard_map": False},
    "conservatism": {
        "entries": [_entry("Conservatism", "conservatism", "结果混入认识论 conservatism 同形词噪声，核名必要")],
        "hard_map": False,
    },
    "liberalism": {"entries": [_entry("Liberalism", "liberalism")], "hard_map": False},
    "socialism": {"entries": [_entry("Socialism", "socialism")], "hard_map": False},
    "nationalism": {"entries": [_entry("Nationalism", "nationalism")], "hard_map": False},
    "republicanism": {"entries": [_entry("Republicanism", "republicanism")], "hard_map": False},
    "utilitarianism": {
        "entries": [_entry(
            "The History of Utilitarianism", "utilitarianism-history",
            "SEP 无独立 Utilitarianism 条目；当代理论形态另见 Consequentialism",
        )],
        "hard_map": True,
    },
    "social contract": {
        "entries": [
            _entry("Contractarianism", "contractarianism", "古典契约传统"),
            _entry(
                "Contemporary Approaches to the Social Contract",
                "contractarianism-contemporary",
                "当代契约进路",
            ),
        ],
        "hard_map": True,  # 主题拆两条目，需同时挂
    },
    "property": {
        "entries": [_entry(
            "Property and Ownership", "property",
            "直查首位误召 Intellectual Property，政治哲学规范条目在第 2 位",
        )],
        "hard_map": True,
    },
    "civil disobedience": {"entries": [_entry("Civil Disobedience", "civil-disobedience")], "hard_map": False},
    # 中国政治思想线 3 词
    "confucian political thought": {
        "entries": [_entry(
            "Social and Political Thought in Chinese Philosophy", "chinese-social-political",
            "无专条：伞形条目 + Confucius/Mencius 等思想家条目拼合",
        )],
        "hard_map": True,
    },
    "legalism (chinese philosophy)": {
        "entries": [_entry("Legalism in Chinese Philosophy", "chinese-legalism")],
        "hard_map": False,
    },
    "chinese political philosophy": {
        "entries": [_entry(
            "Social and Political Thought in Chinese Philosophy", "chinese-social-political",
            "伞形专条（覆盖先秦至宋明及近现代）",
        )],
        "hard_map": False,
    },
    # 补充验证 3 词
    "contractarianism": {"entries": [_entry("Contractarianism", "contractarianism")], "hard_map": False},
    "confucius": {"entries": [_entry("Confucius", "confucius")], "hard_map": False},
    "mencius": {"entries": [_entry("Mencius", "mencius")], "hard_map": False},
}

# 硬映射子集（docs/sep-coverage.md §四 缺口 3/4/5 的纠偏清单）
HARD_MAPPINGS: dict[str, dict] = {t: e for t, e in CANONICAL.items() if e["hard_map"]}

CORE_TERMS: frozenset[str] = frozenset(CANONICAL)


def entry_url(slug: str, base_url: str = DEFAULT_BASE) -> str:
    return f"{base_url.rstrip('/')}/entries/{slug.strip('/')}/"


def canonical_hits(term: str, base_url: str = DEFAULT_BASE) -> list[SepHit]:
    """curated 锚点（title + URL，snippet 空）：断网时以只链不抄的离线锚点回报。"""
    spec = CANONICAL.get(term)
    if not spec:
        return []
    return [SepHit(title=e["title"], url=entry_url(e["slug"], base_url), snippet="") for e in spec["entries"]]


# ---------------------------------------------------------------- 抓取（限速）

_last_request: float = 0.0  # monotonic 时间戳（进程内限速状态）


def _throttle_delay(last: float, now: float) -> float:
    """距上次请求的必要等待（纯函数，供测试）。"""
    return max(0.0, last + MIN_REQUEST_INTERVAL_S - now)


def search_url(term: str, base_url: str = DEFAULT_BASE) -> str:
    """SEP 搜索端点 URL（GET，返回 HTML 结果页）。"""
    return f"{base_url.rstrip('/')}{SEARCH_PATH}?query={term}"


async def fetch_search_html(
    term: str,
    base_url: str = DEFAULT_BASE,
    timeout: float = DEFAULT_TIMEOUT,
) -> str:
    """实时抓搜索结果页 HTML（限速 1.5s/请求）。

    网络层失败抛 SepUnreachable，非 200 抛 SepUpstream。只打搜索端点，绝不触正文页。
    """
    global _last_request
    wait = _throttle_delay(_last_request, time.monotonic())
    if wait > 0:
        await asyncio.sleep(wait)
    _last_request = time.monotonic()
    url = f"{base_url.rstrip('/')}{SEARCH_PATH}"
    try:
        async with httpx.AsyncClient(
            timeout=timeout,
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
        ) as client:
            resp = await client.get(url, params={"query": term})
    except httpx.HTTPError as exc:  # 含 TimeoutException / TransportError
        raise SepUnreachable(f"{type(exc).__name__}: {exc}") from exc
    if resp.status_code != 200:
        raise SepUpstream(f"HTTP {resp.status_code} for query={term!r}")
    return resp.text


# ---------------------------------------------------------------- 解析（纯函数）

_BLOCK_SPLIT_RE = re.compile(r'<div class="result_listing">')
_TITLE_RE = re.compile(r'<div class="result_title">\s*<a\b[^>]*>(.*?)</a>', re.S)
_SNIPPET_RE = re.compile(r'<div class="result_snippet">(.*?)</div>', re.S)
_RESULT_URL_RE = re.compile(r'<div class="result_url">\s*<a\b[^>]*>(.*?)</a>', re.S)
_TAG_RE = re.compile(r"<[^>]+>")
_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
_ENTRY_SLUG_RE = re.compile(r"entries/([^/?#&]+)")
_WS_RE = re.compile(r"\s+")


def _clean_text(raw: str, strip_comments: bool = False) -> str:
    """去注释/标签 → 反转义实体 → 折叠空白。"""
    if strip_comments:
        raw = _COMMENT_RE.sub(" ", raw)
    text = html_lib.unescape(_TAG_RE.sub(" ", raw))
    return _WS_RE.sub(" ", text).strip()


def _entry_url_from_display(raw: str, base_url: str) -> str | None:
    """优先取 result_url 区展示的规范链接文本（…/entries/<slug>/）。"""
    candidate = html_lib.unescape(raw).strip()
    m = _ENTRY_SLUG_RE.search(candidate)
    if not m:
        return None
    return f"{base_url.rstrip('/')}/entries/{m.group(1)}/"


def _entry_url_from_redirect(href: str, base_url: str) -> str | None:
    """兜底：从 result_title 锚点的 search/r?entry=/entries/<slug>/ 跳转参数还原。"""
    try:
        qs = parse_qs(urlparse(html_lib.unescape(href)).query)
        entry = qs.get("entry", [""])[0]
    except ValueError:
        return None
    m = _ENTRY_SLUG_RE.search(unquote(entry))
    if not m:
        return None
    return f"{base_url.rstrip('/')}/entries/{m.group(1)}/"


def parse_search_results(
    html: str, base_url: str = DEFAULT_BASE, limit: int = DEFAULT_LIMIT
) -> list[SepHit]:
    """解析搜索结果页 → 前 limit 条含 entries 链接的引用性结果（按排名序，按 URL 去重）。

    无 entries 链接时返回空列表（调用方按失败口径回报）。
    """
    hits: list[SepHit] = []
    seen: set[str] = set()
    chunks = _BLOCK_SPLIT_RE.split(html)[1:]  # 首段是结果列表之前的页面骨架
    for chunk in chunks:
        if len(hits) >= limit:
            break
        title_m = _TITLE_RE.search(chunk)
        if not title_m:
            continue
        url: str | None = None
        url_m = _RESULT_URL_RE.search(chunk)
        if url_m:
            url = _entry_url_from_display(url_m.group(1), base_url)
        if url is None:
            href_m = re.search(r'href="([^"]+)"', title_m.group(0))
            if href_m:
                url = _entry_url_from_redirect(href_m.group(1), base_url)
        if url is None or url in seen:
            continue  # 非 entries 结果（如站内其他页面）不收
        snippet_m = _SNIPPET_RE.search(chunk)
        snippet = _clean_text(snippet_m.group(1), strip_comments=True) if snippet_m else ""
        hits.append(SepHit(title=_clean_text(title_m.group(1)), url=url, snippet=snippet))
        seen.add(url)
    return hits


def apply_hard_map(term: str, hits: list[SepHit], base_url: str = DEFAULT_BASE) -> list[SepHit]:
    """硬映射纠偏：规范条目按映射序钉在前面（snippet 取实时结果），其余按排名随后。

    非硬映射词原样返回。规范条目若不在实时结果里（排名漂移出页），仍以只链不抄的
    curated 链接补位（snippet 空，不编造摘要）。
    """
    spec = HARD_MAPPINGS.get(term)
    if not spec:
        return hits
    by_url = {h.url: h for h in hits}
    pinned: list[SepHit] = []
    for e in spec["entries"]:
        url = entry_url(e["slug"], base_url)
        live = by_url.pop(url, None)
        pinned.append(live if live is not None else SepHit(title=e["title"], url=url, snippet=""))
    pinned.extend(by_url.values())
    return pinned


# ---------------------------------------------------------------- 存储与缓存

def _short_quote(text: str, limit: int = MAX_SNIPPET_CHARS) -> str:
    """压空白后按词/字边界截断为短引用（不保留正文），总长不超过 limit。"""
    collapsed = " ".join((text or "").split())
    if len(collapsed) <= limit:
        return collapsed
    cut = collapsed[: limit - 1]
    for sep in (" ", "。", "；", ";", ",", "，"):
        idx = cut.rfind(sep)
        if idx >= (limit - 1) // 2:
            return cut[:idx].rstrip() + "…"
    return cut.rstrip() + "…"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_fresh(retrieved_at: str, now: datetime) -> bool:
    try:
        ts = datetime.fromisoformat(retrieved_at)
    except ValueError:
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts >= now - timedelta(days=CACHE_TTL_DAYS)


def _read_cache(conn, term: str) -> tuple[list[SepHit], str]:
    """读 TTL 内的缓存行（rowid 序 = 写入时的排名序）。返回 (hits, retrieved_at)。

    concepts 表是共享面（china_lineage 种子概念也落此表，URL 带 #china-seed
    标记）：SEP 缓存层只读自己的行，种子行不算缓存命中（不冒充 SEP 结果）。
    """
    now = datetime.now(timezone.utc)
    rows = conn.execute(
        "SELECT title, url, snippet, retrieved_at FROM concepts"
        " WHERE term = ? AND url NOT LIKE ? ORDER BY rowid",
        (term, "%" + SEED_CONCEPT_MARKER),
    ).fetchall()
    hits: list[SepHit] = []
    retrieved_at = ""
    for r in rows:
        if not _is_fresh(r["retrieved_at"], now):
            continue
        if not retrieved_at:
            retrieved_at = r["retrieved_at"]
        hits.append(SepHit(title=r["title"], url=r["url"], snippet=r["snippet"] or ""))
    return hits, retrieved_at


def _write_cache(conn, term: str, hits: list[SepHit]) -> str:
    """整词覆写缓存（删旧插新，单事务）；写时守卫：标题/片段一律截断后落库。

    只覆写 SEP 自己的行（种子行带 #china-seed 标记，不得被实时刷新清掉）。
    """
    retrieved_at = _now_iso()
    try:
        conn.execute("BEGIN")
        conn.execute(
            "DELETE FROM concepts WHERE term = ? AND url NOT LIKE ?",
            (term, "%" + SEED_CONCEPT_MARKER),
        )
        conn.executemany(
            "INSERT INTO concepts (term, title, url, snippet, retrieved_at) VALUES (?, ?, ?, ?, ?)",
            [
                (term, _short_quote(h.title, MAX_TITLE_CHARS), h.url, _short_quote(h.snippet), retrieved_at)
                for h in hits
            ],
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return retrieved_at


def seed_core_cache(conn, base_url: str = DEFAULT_BASE) -> int:
    """核心词白名单缓存物化：26 词的规范条目（title+URL，snippet 空）写入 concepts。

    幂等（INSERT OR REPLACE，键 (term, url)）。离线/生产首启可用；写入行 30 天内
    会被 query_concept 命中（缓存语义），过期后实时刷新并附真实摘要片段。
    """
    rows = []
    now = _now_iso()
    for term, spec in CANONICAL.items():
        for e in spec["entries"]:
            rows.append((term, e["title"], entry_url(e["slug"], base_url), "", now))
    conn.executemany(
        "INSERT OR REPLACE INTO concepts (term, title, url, snippet, retrieved_at) VALUES (?,?,?,?,?)",
        rows,
    )
    conn.commit()
    return len(rows)


def storage_policy_violations(conn) -> list[dict]:
    """存储面自检：列出违反「只存标题/URL/短引用」的行（应为空）。

    只查 SEP 自己的行（concepts 表的 #china-seed 种子行归 china_lineage 口径）。
    """
    rows = conn.execute(
        "SELECT term, title, url, snippet FROM concepts WHERE url NOT LIKE ?",
        ("%" + SEED_CONCEPT_MARKER,),
    ).fetchall()
    out: list[dict] = []
    for r in rows:
        title_len, snippet_len = len(r["title"] or ""), len(r["snippet"] or "")
        if title_len > MAX_TITLE_CHARS or snippet_len > MAX_SNIPPET_CHARS:
            out.append({"term": r["term"], "url": r["url"], "title_len": title_len, "snippet_len": snippet_len})
    return out


def _curated_fallback(term: str, base_url: str) -> dict | None:
    """白名单词断网时的 curated 离线锚点（不写缓存，不冒充实时结果）。"""
    hits = canonical_hits(term, base_url)
    if not hits:
        return None
    notes = [e["note"] for e in CANONICAL[term]["entries"] if e.get("note")]
    out = {
        "results": [h.as_dict() for h in hits],
        "provenance": provenance("sep", stable_id=hits[0].url, as_of=VERIFIED_AT),
        "note": (
            "curated 离线锚点（docs/sep-coverage.md 2026-10-23 实测；本轮未能实时核验）"
            + ("——" + "；".join(notes) if notes else "")
        ),
    }
    if term in HARD_MAPPINGS:
        out["hard_map"] = True
    return out


# ---------------------------------------------------------------- 查询入口

async def query_concept(term: str, *, settings: Settings | None = None) -> dict:
    """政治概念查询：定义摘要（搜索结果页引用性片段）+ 条目链接（只链不抄）。

    流程：归一化 → concepts 缓存（TTL 30 天）→ 实时搜索（限速 1.5s）→
    硬映射纠偏（property→/entries/property/ 钉首、social contract 拆两条目等）→
    写缓存。白名单词断网时回 curated 离线锚点（标注 note，不写缓存）。

    空结果必带 reason（errors.py taxonomy）：unreachable / upstream。空白查询词
    属调用方 bug，直接抛 ValueError（不静默）。
    """
    query = " ".join(term.split()).lower()
    if not query:
        raise ValueError("空查询词")

    settings = settings or Settings.from_env()
    base_url = settings.sep_base
    conn = settings.connect()
    try:
        schema.init(conn)
        cached, cached_at = _read_cache(conn, query)
        if cached:
            return {
                "results": [h.as_dict() for h in cached],
                "provenance": provenance("sep", stable_id=cached[0].url, as_of=cached_at),
            }
        try:
            html = await fetch_search_html(query, base_url=base_url)
        except SepUnreachable as exc:
            fallback = _curated_fallback(query, base_url)
            if fallback:
                return fallback
            return empty_result(UNREACHABLE, hint=f"SEP 搜索端点不可达（{exc}）")
        except SepUpstream as exc:
            fallback = _curated_fallback(query, base_url)
            if fallback:
                return fallback
            return empty_result(UPSTREAM, hint=f"SEP 搜索端点响应异常（{exc}）")

        hits = parse_search_results(html, base_url=base_url, limit=DEFAULT_LIMIT)
        hits = apply_hard_map(query, hits, base_url=base_url)
        if not hits:
            fallback = _curated_fallback(query, base_url)
            if fallback:
                return fallback
            return empty_result(
                UPSTREAM,
                hint="端点正常返回但解析不出可用的 /entries/ 结果（响应结构异常或该词无对应条目）",
            )
        # 归一化先于写缓存：返回值与落库内容逐字一致
        hits = [
            SepHit(
                title=_short_quote(h.title, MAX_TITLE_CHARS),
                url=h.url,
                snippet=_short_quote(h.snippet),
            )
            for h in hits[:DEFAULT_LIMIT]
        ]
        retrieved_at = _write_cache(conn, query, hits)
        out = {
            "results": [h.as_dict() for h in hits],
            "provenance": provenance("sep", stable_id=hits[0].url, as_of=retrieved_at),
        }
        if query in HARD_MAPPINGS:
            out["hard_map"] = True
        return out
    finally:
        conn.close()
