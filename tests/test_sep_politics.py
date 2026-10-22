"""SEP 政治概念层客户端测试（全离线：响应桩 + 临时 SQLite + monkeypatch）。

许可红线：只断言 标题/摘要片段/entries 链接，任何用例都不得抓词条正文页；
断网口径照 errors.py（unreachable / upstream），空结果必带 reason。
"""

from __future__ import annotations

import pytest

from fd_zhengxin_reference import schema, sep_politics
from fd_zhengxin_reference.config import Settings
from fd_zhengxin_reference.sep_politics import (
    SepUnreachable,
    SepUpstream,
    apply_hard_map,
    canonical_hits,
    entry_url,
    parse_search_results,
    query_concept,
    search_url,
    seed_core_cache,
    _throttle_delay,
)


def _fake_fetch(html: str):
    async def fake(term: str, base_url: str = "", timeout: float = 0) -> str:
        return html

    return fake


def _block(title: str, slug: str, snippet: str, rank: int = 0) -> str:
    """拼一个 SEP 搜索结果块（照真实结果页 result_listing 结构）。"""
    return f"""
    <div class="result_listing">
      <div class="result_title"><a href="https://plato.stanford.edu/search/r?rank={rank}">{title}</a></div>
      <div class="result_snippet">{snippet}</div>
      <div class="result_url"><a href="entries/{slug}/">https://plato.stanford.edu/entries/{slug}/</a></div>
    </div>
    """


def _settings(tmp_path) -> Settings:
    return Settings(db_path=str(tmp_path / "ref.db"))


# ---------------------------------------------------------------- 纯函数


def test_search_url() -> None:
    assert search_url("justice") == (
        "https://plato.stanford.edu/search/searcher.py?query=justice"
    )


def test_entry_url_normalizes_slug() -> None:
    assert entry_url("property") == "https://plato.stanford.edu/entries/property/"
    assert entry_url("/property/") == "https://plato.stanford.edu/entries/property/"


def test_canonical_whitelist_coverage() -> None:
    """白名单 26 词（docs/sep-coverage.md 20 主表 + 3 中国线 + 3 补充验证）。"""
    assert len(sep_politics.CANONICAL) == 26
    assert sep_politics.CORE_TERMS == frozenset(sep_politics.CANONICAL)
    # 硬映射子集 = 纠偏清单（缺口 3/4/5）
    assert set(sep_politics.HARD_MAPPINGS) == {
        "liberty",
        "legitimacy",
        "utilitarianism",
        "social contract",
        "property",
        "confucian political thought",
    }


def test_throttle_delay_math() -> None:
    assert _throttle_delay(last=0.0, now=0.0) == sep_politics.MIN_REQUEST_INTERVAL_S
    assert _throttle_delay(last=0.0, now=1.5) == 0.0
    assert _throttle_delay(last=10.0, now=11.0) == pytest.approx(0.5)


def test_parse_takes_only_entries_links() -> None:
    page = (
        _block("Intellectual Property", "intellectual-property", "patents and copyright…")
        + _block("Property and Ownership", "property", "private property and its justification…")
        + """
    <div class="result_listing">
      <div class="result_title"><a href="https://plato.stanford.edu/blog/x">Blog</a></div>
      <div class="result_snippet">not an entry</div>
      <div class="result_url"><a href="/blog/x">https://plato.stanford.edu/blog/x</a></div>
    </div>
    """
    )
    hits = parse_search_results(page)
    assert [h.url for h in hits] == [
        "https://plato.stanford.edu/entries/intellectual-property/",
        "https://plato.stanford.edu/entries/property/",
    ]
    for h in hits:
        assert h.title and "<" not in h.snippet


def test_parse_no_entries_returns_empty() -> None:
    assert parse_search_results("<html><body>0 documents found</body></html>") == []


# ---------------------------------------------------------------- 硬映射纠偏（核心用例）


def test_hard_map_property_pins_canonical_first() -> None:
    """直查 property 首位误召 Intellectual Property：纠偏后规范条目钉首。"""
    page = (
        _block("Intellectual Property", "intellectual-property", "patents…")
        + _block("Property and Ownership", "property", "the justification of private property…")
    )
    hits = parse_search_results(page)
    pinned = apply_hard_map("property", hits)
    assert pinned[0].url == "https://plato.stanford.edu/entries/property/"
    assert pinned[0].title == "Property and Ownership"
    assert pinned[0].snippet  # snippet 取实时结果，不空造
    assert pinned[1].url.endswith("/entries/intellectual-property/")  # 误召仍保留在后


def test_hard_map_social_contract_splits_two_entries() -> None:
    """social contract 主题拆两条目：古典 + 当代，按映射序钉前。"""
    page = (
        _block("Contemporary Approaches to the Social Contract", "contractarianism-contemporary", "rawls…")
        + _block("Contractarianism", "contractarianism", "hobbes…")
        + _block("Liberalism", "liberalism", "other hit")
    )
    pinned = apply_hard_map("social contract", parse_search_results(page))
    assert [h.url for h in pinned[:2]] == [
        "https://plato.stanford.edu/entries/contractarianism/",
        "https://plato.stanford.edu/entries/contractarianism-contemporary/",
    ]


def test_hard_map_missing_canonical_still_linked_without_snippet() -> None:
    """规范条目漂移出结果页：仍以 curated 链接补位，snippet 空（不编造摘要）。"""
    page = _block("Intellectual Property", "intellectual-property", "patents…")
    pinned = apply_hard_map("liberty", parse_search_results(page))
    assert pinned[0].url == "https://plato.stanford.edu/entries/liberty-positive-negative/"
    assert pinned[0].title == "Positive and Negative Liberty"
    assert pinned[0].snippet == ""


def test_non_hard_map_term_untouched() -> None:
    hits = parse_search_results(_block("Justice", "justice", "each person…"))
    assert apply_hard_map("justice", hits) == hits


# ---------------------------------------------------------------- query_concept（离线桩）


async def test_query_concept_fetches_parses_caches(tmp_path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    page = (
        _block("Intellectual Property", "intellectual-property", "patents and copyright…")
        + _block("Property and Ownership", "property", "the justification of private property …")
    )
    monkeypatch.setattr(sep_politics, "fetch_search_html", _fake_fetch(page))
    out = await query_concept("Property", settings=settings)  # 归一化为小写

    assert out["hard_map"] is True
    assert out["results"][0]["url"] == "https://plato.stanford.edu/entries/property/"
    prov = out["provenance"]
    assert prov["source"] == "sep"
    assert prov["attribution"].startswith("Stanford Encyclopedia of Philosophy")
    assert prov["stable_id"] == out["results"][0]["url"]

    conn = settings.connect()
    try:
        rows = conn.execute(
            "SELECT url FROM concepts WHERE term=? ORDER BY rowid", ("property",)
        ).fetchall()
        assert rows[0]["url"] == "https://plato.stanford.edu/entries/property/"
        assert sep_politics.storage_policy_violations(conn) == []
    finally:
        conn.close()


async def test_query_concept_cache_hit_skips_network(tmp_path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    conn = settings.connect()
    schema.init(conn)
    assert seed_core_cache(conn) == sum(len(e["entries"]) for e in sep_politics.CANONICAL.values())
    conn.close()

    async def must_not_fetch(*a, **kw):
        raise AssertionError("缓存命中时不得再打网络")

    monkeypatch.setattr(sep_politics, "fetch_search_html", must_not_fetch)
    out = await query_concept("justice", settings=settings)
    assert out["results"][0]["url"] == "https://plato.stanford.edu/entries/justice/"
    assert out["provenance"]["source"] == "sep"


async def test_query_concept_seed_cache_idempotent(tmp_path) -> None:
    settings = _settings(tmp_path)
    conn = settings.connect()
    schema.init(conn)
    seed_core_cache(conn)
    seed_core_cache(conn)  # 幂等
    n = conn.execute("SELECT COUNT(*) AS c FROM concepts").fetchone()["c"]
    conn.close()
    assert n == sum(len(e["entries"]) for e in sep_politics.CANONICAL.values()) == 27


async def test_query_concept_unreachable_returns_reason(tmp_path, monkeypatch) -> None:
    settings = _settings(tmp_path)

    async def boom(*a, **kw):
        raise SepUnreachable("dns failure")

    monkeypatch.setattr(sep_politics, "fetch_search_html", boom)
    out = await query_concept("deliberative democracy", settings=settings)  # 非白名单词
    assert out["results"] == []
    assert out["reason"] == "unreachable"  # errors.py 口径


async def test_query_concept_upstream_returns_reason(tmp_path, monkeypatch) -> None:
    settings = _settings(tmp_path)

    async def boom(*a, **kw):
        raise SepUpstream("HTTP 503")

    monkeypatch.setattr(sep_politics, "fetch_search_html", boom)
    out = await query_concept("deliberative democracy", settings=settings)
    assert out["results"] == []
    assert out["reason"] == "upstream"
    assert out["hint"]  # 空结果必带可解释的原因


async def test_query_concept_curated_fallback_offline(tmp_path, monkeypatch) -> None:
    """白名单词断网：回 curated 离线锚点（只链不抄、标注 note、不写缓存）。"""
    settings = _settings(tmp_path)

    async def boom(*a, **kw):
        raise SepUnreachable("offline")

    monkeypatch.setattr(sep_politics, "fetch_search_html", boom)
    out = await query_concept("social contract", settings=settings)
    assert {r["url"] for r in out["results"]} == {
        "https://plato.stanford.edu/entries/contractarianism/",
        "https://plato.stanford.edu/entries/contractarianism-contemporary/",
    }
    assert out["provenance"]["as_of"] == sep_politics.VERIFIED_AT
    assert "curated" in out["note"]
    conn = settings.connect()
    try:
        n = conn.execute("SELECT COUNT(*) AS c FROM concepts WHERE term='social contract'").fetchone()["c"]
        assert n == 0  # 降级结果不冒充实时缓存
    finally:
        conn.close()


async def test_query_concept_no_entries_non_whitelist(tmp_path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    monkeypatch.setattr(
        sep_politics, "fetch_search_html", _fake_fetch("<html><body>nothing</body></html>")
    )
    out = await query_concept("zzznonsense", settings=settings)
    assert out["results"] == []
    assert out["reason"] == "upstream"  # 端点正常但解析不出可用结果（空必带原因）
    assert out["hint"]


async def test_query_concept_long_snippet_truncated(tmp_path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    page = _block("Justice", "justice", "word " * 500)
    monkeypatch.setattr(sep_politics, "fetch_search_html", _fake_fetch(page))
    out = await query_concept("justice", settings=settings)
    assert len(out["results"][0]["snippet"]) <= sep_politics.MAX_SNIPPET_CHARS


async def test_query_concept_blank_term_raises(tmp_path) -> None:
    with pytest.raises(ValueError):
        await query_concept("   ", settings=_settings(tmp_path))


async def test_query_concept_schema_created_idempotently(tmp_path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    page = _block("Liberalism", "liberalism", "the liberal tradition…")
    monkeypatch.setattr(sep_politics, "fetch_search_html", _fake_fetch(page))
    for _ in range(2):
        out = await query_concept("liberalism", settings=settings)
        assert len(out["results"]) >= 1
