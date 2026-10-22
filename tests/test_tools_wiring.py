"""断源联测（任务 2.6 接线验收）：四工具接线形态下，任一源断——SEP 不可达、
wikidata 未摄取、marpor 门关、vdem 无数据——各查询空结果必带原因
（blocked/upstream/unreachable/not_ingested；marpor 另带台账口径
license_unconfirmed），绝不静默为空；好源（china-seed / modern-index /
已摄取的 v-dem、wikidata）不受坏源影响。

全离线：SEP 走响应桩/断网桩（照 test_sep_politics 口径）；wikidata 摄取走最小
接线桩（QID/标签/边取探针样本真值：Q76 Obama、Q29468 Republican Party、P102
党籍边、P1387 right-wing——本文件测接线管线，WD 语义归 test_wikidata_politics）；
vdem 用合成 CSV（官方 schema 列名，照 test_vdem 口径）。
"""

from __future__ import annotations

import re

import pytest

from fd_zhengxin_reference import sep_politics, vdem, wikidata_politics as wpx
from fd_zhengxin_reference.config import Settings
from fd_zhengxin_reference.errors import NOT_INGESTED, REASONS
from fd_zhengxin_reference.server import bootstrap_curated, create_server
from fd_zhengxin_reference.sep_politics import SepUnreachable
from fd_zhengxin_reference.tools_registry import source_status


# ---------------------------------------------------------------- 桩与夹具


async def call_tool(mcp, name: str, /, **args) -> dict:
    tools = await mcp.list_tools()
    fn = next(t.fn for t in tools if t.name == name)
    return await fn(**args)


async def _sep_offline(*a, **kw):
    raise SepUnreachable("offline (wiring test)")


def _sep_fetch(html: str):
    async def fake(term: str, base_url: str = "", timeout: float = 0) -> str:
        return html

    return fake


def _sep_block(title: str, slug: str, snippet: str = "") -> str:
    """拼一个 SEP 搜索结果块（照真实结果页 result_listing 结构）。"""
    return f"""
    <div class="result_listing">
      <div class="result_title"><a href="https://plato.stanford.edu/search/r">{title}</a></div>
      <div class="result_snippet">{snippet}</div>
      <div class="result_url"><a href="entries/{slug}/">https://plato.stanford.edu/entries/{slug}/</a></div>
    </div>
    """


def _uri(qid: str) -> dict:
    return {"type": "uri", "value": f"http://www.wikidata.org/entity/{qid}"}


def _lit(text: str) -> dict:
    return {"type": "literal", "xml:lang": "en", "value": text}


class WiringWdClient:
    """最小接线桩：按查询里的 `# fdzx:<标记>` 回放预置 bindings（缺标记回空）。"""

    def __init__(self, table: dict[str, list[dict]]):
        self.table = table
        self.calls: list[str] = []

    def query(self, sparql: str) -> dict:
        m = re.search(r"# fdzx:(\S+)", sparql)
        assert m, f"查询缺 fdzx 标记：{sparql[:80]}"
        base = re.sub(r":\d+$", "", m.group(1))  # edge:P102:1 / anchor:0 → 基础标记
        self.calls.append(base)
        return {"results": {"bindings": self.table.get(base, [])}}


# 值取自探针样本真值（pol1 的 Q76、party1 的 Q29468/right-wing、pol3 的 P102）
WIRING_WD_TABLE = {
    "person": [
        {"person": _uri("Q76"), "personLabel": _lit("Barack Obama")},
    ],
    "party": [
        {"party": _uri("Q29468"), "partyLabel": _lit("Republican Party"),
         "alignLabel": _lit("right-wing")},
    ],
    "edge:P102": [
        {"person": _uri("Q76"), "personLabel": _lit("Barack Obama"),
         "party": _uri("Q29468"), "partyLabel": _lit("Republican Party")},
    ],
}

VDEM_CSV = (
    "country_id,country_text_id,country_name,year,v2x_polyarchy,v2x_libdem,version\n"
    "1,USA,United States of America,2000,0.80,0.70,v16\n"
    "1,USA,United States of America,2001,0.81,0.71,v16\n"
    "2,CHN,China,2000,0.20,0.30,v16\n"
)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """隔离环境：独立 DB + 独立快照根（启动钩子会物化 curated 层快照）。"""
    monkeypatch.setenv("FDZX_REF_DB", str(tmp_path / "ref.db"))
    monkeypatch.setenv("FDZX_SNAPSHOT_DIR", str(tmp_path / "snapshots"))
    return Settings.from_env(), tmp_path


@pytest.fixture()
def mcp(env, monkeypatch):
    """已启动的服务：curated 层经启动钩子就位；SEP 置为断网（默认坏源）。"""
    monkeypatch.setattr(sep_politics, "fetch_search_html", _sep_offline)
    return create_server()


def _ingest_wikidata(env) -> str:
    settings, tmp_path = env
    conn = settings.connect()
    try:
        report = wpx.ingest_wikidata_politics(
            ["Q76", "Q29468"], conn=conn,
            client=WiringWdClient(WIRING_WD_TABLE),
            snapshot_root=tmp_path / "snapshots",
        )
        assert report["status"] == "ok"
        return report["snapshot_id"]
    finally:
        conn.close()


def _ingest_vdem(env) -> str:
    settings, tmp_path = env
    csv = tmp_path / "V-Dem-CY-Core-v16.csv"
    csv.write_text(VDEM_CSV, encoding="utf-8")
    out = vdem.ingest_vdem(csv, settings=settings)
    assert out["status"] == "ok"
    return out["snapshot_id"]


# ---------------------------------------------------------------- 启动初始化（启动钩子）


def test_bootstrap_loads_curated_layers(env) -> None:
    """启动钩子：china-seed（48 谱系条目 + 50 概念 + 83 边）与 modern-index
    （5 思潮）随 create_server 落库，台账各记一行 ok。"""
    settings, _ = env
    create_server()
    conn = settings.connect()
    try:
        n = lambda sql: conn.execute(sql).fetchone()["c"]  # noqa: E731
        assert n("SELECT COUNT(*) AS c FROM currents WHERE current_id LIKE 'seed:china/%'") == 48
        assert n("SELECT COUNT(*) AS c FROM currents WHERE current_id LIKE 'modern:china/%'") == 5
        assert n("SELECT COUNT(*) AS c FROM concepts WHERE url LIKE '%#china-seed'") == 50
        assert n("SELECT COUNT(*) AS c FROM relations WHERE source_id LIKE 'seed:china/%'") == 83
        ok = {
            r["source"]
            for r in conn.execute("SELECT source FROM ingest_log WHERE status='ok'")
        }
        assert {"china-seed", "china-modern-index"} <= ok
    finally:
        conn.close()


def test_bootstrap_idempotent(env) -> None:
    """幂等：重复 create_server → 行数不变（覆写式重建 + 快照内容寻址不堆副本）。"""
    settings, _ = env
    create_server()
    create_server()
    conn = settings.connect()
    try:
        n = lambda table: conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]  # noqa: E731
        assert n("currents") == 53  # 48 种子 + 5 近现代
        assert n("concepts") == 50
        assert n("relations") == 83
        for dataset in ("china-seed", "china-modern-index"):
            rows = conn.execute(
                "SELECT COUNT(*) AS c FROM snapshots WHERE dataset=?", (dataset,)
            ).fetchone()["c"]
            assert rows == 1, dataset  # 同内容不堆快照
    finally:
        conn.close()


def test_bootstrap_skips_memory_db(tmp_path, monkeypatch) -> None:
    """:memory: 库跳过（每条连接各自新建，加载即弃）：不写快照根、不炸。"""
    monkeypatch.setenv("FDZX_SNAPSHOT_DIR", str(tmp_path / "snapshots"))
    report = bootstrap_curated(Settings(db_path=":memory:"))
    assert all(v["status"] == "skipped" for v in report.values())
    assert not (tmp_path / "snapshots").exists()


# ---------------------------------------------------------------- concept_lookup


async def test_concept_lookup_seed_row_structure(mcp) -> None:
    """D2 行结构补齐：种子概念行自带 school/figures/weight（消费面归派与召回
    依据），id 锚在 provenance.stable_id；快照缺失字段不编造。"""
    out = await call_tool(mcp, "concept_lookup", term="仁政")
    assert len(out["results"]) == 1
    row = out["results"][0]
    assert row["provenance"]["stable_id"] == "china-seed:concept:仁政"
    assert row["school"] == "儒家"
    assert row["weight"] == 1.0
    assert [f["name"] for f in row["figures"]] == ["孟轲（孟子）"]
    assert row["figures"][0]["years"] == "约前372–前289"


async def test_concept_lookup_modern_row_school(mcp) -> None:
    """D2 行结构补齐：近现代思潮行 school=自身标签 + weight 1.0（精确标签
    命中即定位到该思潮）。"""
    out = await call_tool(mcp, "concept_lookup", term="自由主义思潮")
    row = out["results"][0]
    assert row["school"] == "自由主义思潮"
    assert row["weight"] == 1.0
    assert row["summary"]


async def test_concept_lookup_seed_hit_with_sep_down(mcp) -> None:
    """SEP 断网不影响库内好源：种子概念照常返回（来源标注 + provenance），
    sep 分项如实记 unreachable——好源不受坏源影响。"""
    out = await call_tool(mcp, "concept_lookup", term="仁")
    assert out["results"] and all(r["source"] == "china-seed" for r in out["results"])
    row = out["results"][0]
    assert row["url"].startswith("https://zh.wikipedia.org/wiki/") and row["url"].endswith("#china-seed")
    assert "原典" in row["snippet"]
    assert row["provenance"]["source"] == "china-seed"
    assert row["provenance"]["snapshot_version"].startswith("china-seed@")
    assert out["sources"]["sep"]["status"] == "miss"
    assert out["sources"]["sep"]["reason"] == "unreachable"
    assert out["sources"]["china-seed"]["status"] == "ok"


async def test_concept_lookup_modern_index_hit(mcp) -> None:
    """近现代思潮索引命中：摘要 + 文献条目（只存元数据与链接）+ 快照 provenance。"""
    out = await call_tool(mcp, "concept_lookup", term="维新变法思潮")
    assert len(out["results"]) == 1
    row = out["results"][0]
    assert row["source"] == "modern-index"
    assert row["summary"]
    assert len(row["documents"]) >= 5
    assert all(d["url"].startswith("https://zh.wikisource.org/") for d in row["documents"])
    assert row["provenance"]["snapshot_version"].startswith("china-modern-index@")


async def test_concept_lookup_sep_live_then_cache(mcp, env, monkeypatch) -> None:
    """SEP 实时路径：首查落缓存，二查缓存命中不再打网络。"""
    monkeypatch.setattr(
        sep_politics, "fetch_search_html",
        _sep_fetch(_sep_block("Justice", "justice", "each person getting their due…")),
    )
    first = await call_tool(mcp, "concept_lookup", term="Justice")  # 归一化为小写
    assert first["results"] and first["results"][0]["source"] == "sep"
    assert first["results"][0]["url"] == "https://plato.stanford.edu/entries/justice/"
    assert first["results"][0]["provenance"]["source"] == "sep"

    async def must_not_fetch(*a, **kw):
        raise AssertionError("缓存命中时不得再打网络")

    monkeypatch.setattr(sep_politics, "fetch_search_html", must_not_fetch)
    second = await call_tool(mcp, "concept_lookup", term="justice")
    assert second["results"][0]["url"].endswith("/entries/justice/")
    assert second["sources"]["sep"]["status"] == "ok"


async def test_concept_lookup_whitelist_curated_fallback(mcp, monkeypatch) -> None:
    """白名单词断网：SEP 腿回 curated 离线锚点（带 note 标注，不冒充实时）。"""
    monkeypatch.setattr(sep_politics, "fetch_search_html", _sep_offline)
    out = await call_tool(mcp, "concept_lookup", term="social contract")
    sep_rows = [r for r in out["results"] if r["source"] == "sep"]
    assert {r["url"] for r in sep_rows} == {
        "https://plato.stanford.edu/entries/contractarianism/",
        "https://plato.stanford.edu/entries/contractarianism-contemporary/",
    }
    assert "curated" in out["sources"]["sep"]["note"]


async def test_concept_lookup_all_sources_miss(mcp) -> None:
    """全源皆空：空结果必带 taxonomy reason（SEP 断网主导 → unreachable）+ hint
    汇总各源说明，绝不静默为空。"""
    out = await call_tool(mcp, "concept_lookup", term="zzznonsense")
    assert out["results"] == []
    assert out["reason"] == "unreachable"
    assert out["reason"] in REASONS
    assert out["hint"]
    assert set(out["sources"]) == {"china-seed", "modern-index", "sep"}


async def test_concept_lookup_blank_term_raises(mcp) -> None:
    with pytest.raises(ValueError):
        await call_tool(mcp, "concept_lookup", term="   ")


# ---------------------------------------------------------------- ideology_query


async def test_ideology_query_anchor_and_marpor_gate(mcp, env) -> None:
    """wikidata 好源照常（label 解析 → P1387 锚），marpor 门关如实
    not_ingested + gate_status=license_unconfirmed（台账口径在分项）。"""
    sid = _ingest_wikidata(env)
    out = await call_tool(mcp, "ideology_query", name="Republican Party")
    rows = [r for r in out["results"] if r["source"] == "wikidata"]
    assert [(r["anchor_pid"], r["anchor_label"]) for r in rows] == [("P1387", "right-wing")]
    assert rows[0]["provenance"]["snapshot_version"] == sid
    marpor_leg = out["sources"]["marpor"]
    assert marpor_leg["status"] == "miss"
    assert marpor_leg["reason"] == NOT_INGESTED
    assert marpor_leg["gate_status"] == "license_unconfirmed"
    assert marpor_leg["gate_detail"]


async def test_ideology_query_qid_direct(mcp, env) -> None:
    """QID 直填同路（不经 label 解析）。"""
    _ingest_wikidata(env)
    out = await call_tool(mcp, "ideology_query", name="Q29468")
    assert any(r["anchor_pid"] == "P1387" for r in out["results"])


async def test_ideology_query_all_sources_empty(mcp) -> None:
    """wikidata 未摄取 + marpor 门关：空结果 not_ingested + hint 覆盖两源。"""
    out = await call_tool(mcp, "ideology_query", name="保守主义")
    assert out["results"] == []
    assert out["reason"] == NOT_INGESTED
    assert out["hint"]
    assert out["sources"]["wikidata"]["reason"] == NOT_INGESTED
    assert out["sources"]["marpor"]["gate_status"] == "license_unconfirmed"


# ---------------------------------------------------------------- relation_query


async def test_relation_query_seed_influence_edge(mcp) -> None:
    """种子师承边：孔子→曾参（曾子）（kind=influence / polarity=positive）；
    SEP 断网与 wikidata 未摄取不影响种子腿（好源不受坏源影响）。"""
    out = await call_tool(mcp, "relation_query", source="孔子", target="曾参（曾子）")
    rows = [r for r in out["results"] if r["source"] == "china-seed"]
    assert len(rows) == 1
    edge = rows[0]
    assert edge["kind"] == "influence" and edge["polarity"] == "positive"
    assert edge["source_id"] == "seed:china/thinker/kongzi"
    assert edge["target_id"] == "seed:china/thinker/zengzi"
    assert edge["provenance"]["snapshot_version"].startswith("china-seed@")
    # D2 行结构补齐：端点人读名 + 学派 + curated 权重 + 出典 key_text
    assert edge["source_name"] == "孔子" and edge["target_name"] == "曾参（曾子）"
    assert edge["school"] == "儒家" and edge["weight"] == 1.0
    assert edge["key_text"].startswith("「参乎")


async def test_relation_query_seed_critique_edge(mcp) -> None:
    """种子批判边：荀况（荀子）→孟轲（孟子）（kind=critique / polarity=critical）。"""
    out = await call_tool(mcp, "relation_query", source="荀况（荀子）", target="孟轲（孟子）")
    rows = [r for r in out["results"] if r["source"] == "china-seed"]
    assert len(rows) == 1
    assert rows[0]["kind"] == "critique" and rows[0]["polarity"] == "critical"


async def test_relation_query_seed_pair_without_edge(mcp) -> None:
    """两人物均在谱系但无边：空结果 not_ingested + hint 指明「无边」。"""
    out = await call_tool(mcp, "relation_query", source="孔子", target="荀况（荀子）")
    assert out["results"] == []
    assert out["reason"] == NOT_INGESTED
    assert "无边" in out["hint"]


async def test_relation_query_wikidata_edge_and_polarity(mcp, env) -> None:
    """wikidata 边（P102 党籍，alliance）：label 双端解析 + 端点对收敛 + 极性直通。"""
    _ingest_wikidata(env)
    out = await call_tool(
        mcp, "relation_query", source="Barack Obama", target="Republican Party"
    )
    rows = [r for r in out["results"] if r["source"] == "wikidata"]
    assert len(rows) == 1
    assert rows[0]["property_pid"] == "P102"
    assert rows[0]["polarity"] == "alliance" and rows[0]["direction"] == "out"

    # 极性直通：alliance 过滤保留该边；opposition 过滤后空（必带原因）
    keep = await call_tool(
        mcp, "relation_query",
        source="Barack Obama", target="Republican Party", polarity="alliance",
    )
    assert [r["property_pid"] for r in keep["results"]] == ["P102"]
    drop = await call_tool(
        mcp, "relation_query",
        source="Barack Obama", target="Republican Party", polarity="opposition",
    )
    assert drop["results"] == []
    assert drop["reason"] == NOT_INGESTED
    assert "opposition" in drop["hint"]


async def test_relation_query_wikidata_no_snapshot(mcp) -> None:
    """wikidata 未摄取：QID 直填也查不到边——空结果 not_ingested（不静默）；
    同库的种子腿（好源）不受影响。"""
    out = await call_tool(mcp, "relation_query", source="Q76", target="Q29468")
    assert out["results"] == []
    assert out["reason"] == NOT_INGESTED
    assert out["sources"]["wikidata"]["reason"] == NOT_INGESTED

    good = await call_tool(mcp, "relation_query", source="孔子", target="曾参（曾子）")
    assert any(r["source"] == "china-seed" for r in good["results"])


async def test_relation_query_bad_polarity_raises(mcp) -> None:
    with pytest.raises(ValueError):
        await call_tool(mcp, "relation_query", source="孔子", target="曾参（曾子）", polarity="bogus")


# ---------------------------------------------------------------- regime_timeseries


async def test_regime_timeseries_no_data(mcp) -> None:
    """vdem 无数据：空结果 not_ingested + hint 指明「尚未摄取」。"""
    out = await call_tool(mcp, "regime_timeseries", country="USA", indicator="v2x_polyarchy")
    assert out["results"] == []
    assert out["reason"] == NOT_INGESTED
    assert "尚未摄取" in out["hint"]


async def test_regime_timeseries_after_ingest(mcp, env) -> None:
    """vdem 已摄取：直通结果带指标口径说明与快照 provenance；years/indicator 过滤。"""
    sid = _ingest_vdem(env)
    out = await call_tool(mcp, "regime_timeseries", country="USA")
    assert [r["year"] for r in out["results"]] == [2000, 2001]
    assert set(out["results"][0]["indicators"]) == {"v2x_polyarchy", "v2x_libdem"}
    # 指标口径说明：命中指标逐一给口径，非空
    assert set(out["indicator_notes"]) == {"v2x_polyarchy", "v2x_libdem"}
    assert all(note for note in out["indicator_notes"].values())
    assert out["provenance"]["source"] == "v-dem"
    assert out["provenance"]["snapshot_version"] == sid
    assert "CC BY-SA 4.0" in out["provenance"]["attribution"]

    one = await call_tool(
        mcp, "regime_timeseries",
        country="USA", indicator="v2x_polyarchy", years=[2001],
    )
    assert [r["year"] for r in one["results"]] == [2001]
    assert list(one["results"][0]["indicators"]) == ["v2x_polyarchy"]


async def test_regime_timeseries_unaffected_by_other_outages(mcp, env) -> None:
    """SEP 断网 + wikidata 未摄取 + marpor 门关：vdem（已摄取）照常返回——
    好源不受坏源影响。"""
    _ingest_vdem(env)
    out = await call_tool(mcp, "regime_timeseries", country="CHN", indicator="v2x_polyarchy")
    assert out["results"] and out["results"][0]["country_code"] == "CHN"


# ---------------------------------------------------------------- source_status（六源台账）


async def test_source_status_six_sources_after_bootstrap_and_ingests(mcp, env, monkeypatch) -> None:
    """六源台账：状态从台账/许可门读——curated 两源 ok（启动钩子），已摄取源
    ok 并附快照（dataset 名映射：wikidata→wikidata-politics），marpor 门关
    license_unconfirmed + gate_detail，sep 缓存计数不含种子行。"""
    _ingest_wikidata(env)
    _ingest_vdem(env)
    monkeypatch.setattr(
        sep_politics, "fetch_search_html",
        _sep_fetch(_sep_block("Justice", "justice", "each person getting their due…")),
    )
    sep_out = await call_tool(mcp, "concept_lookup", term="justice")  # 物化 sep 缓存
    assert sep_out["sources"]["sep"]["status"] == "ok"

    out = source_status()
    by = {r["source"]: r for r in out["results"]}
    assert set(by) == {"v-dem", "marpor", "wikidata", "sep", "china-seed", "modern-index"}

    for source in ("v-dem", "wikidata", "sep", "china-seed", "modern-index"):
        assert by[source]["status"] == "ok", (source, by[source])
    assert by["v-dem"]["snapshot"]["snapshot_id"].startswith("v-dem@v16#")
    # dataset 映射：台账源名 wikidata 的在用快照属 wikidata-politics 数据集
    assert by["wikidata"]["snapshot"]["snapshot_id"].split("#")[0].startswith("wikidata-politics@")
    assert by["china-seed"]["snapshot"]["snapshot_id"].startswith("china-seed@")
    assert by["modern-index"]["snapshot"]["snapshot_id"].startswith("china-modern-index@")
    # sep 缓存只数非种子行（concepts 表 50 条种子概念 + 1 条 sep 缓存）
    assert by["sep"]["detail"] == "on-demand 缓存 1 条"

    # marpor：许可门关 → not_ingested + license_unconfirmed + 评审待办
    assert by["marpor"]["status"] == "not_ingested"
    assert by["marpor"]["reason"] == "license_unconfirmed"
    assert by["marpor"]["gate_detail"]


def test_source_status_curated_not_ingested_without_bootstrap(env) -> None:
    """未走启动钩子（直调台账）：curated 两源 not_ingested 且缺口可见。"""
    out = source_status()
    by = {r["source"]: r for r in out["results"]}
    for source in ("china-seed", "modern-index"):
        assert by[source]["status"] == "not_ingested"
        assert by[source]["reason"] == "not_ingested"
