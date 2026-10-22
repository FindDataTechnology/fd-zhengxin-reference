"""中国线种子谱系测试：seed JSON 结构契约（粒度/枚举/双锚/来源）、入库幂等、
is_seed 标记、种子不造数值坐标；近现代思潮文献索引层快照版本化。
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from fd_zhengxin_reference import china_lineage, schema
from fd_zhengxin_reference.china_lineage import (
    MODERN_CURRENTS,
    SeedInvalid,
    default_seed_path,
    is_seed_concept_url,
    is_seed_current,
    load_china_seed,
    load_modern_index,
    validate_seed,
)


@pytest.fixture
def seed_data() -> dict:
    return china_lineage.load_seed_data(default_seed_path())


@pytest.fixture
def conn(tmp_path) -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    schema.init(c)
    yield c
    c.close()


# ---------------------------------------------------------------- seed JSON 结构契约


def test_seed_validates_clean(seed_data) -> None:
    assert validate_seed(seed_data) == []


def test_granularity_bounds(seed_data) -> None:
    """粒度落 docs/china-modern-sources.md 定案区间（meta.granularity 声明并断言）。"""
    gran = seed_data["meta"]["granularity"]
    actual = {
        "schools": len(seed_data["schools"]),
        "figures": len(seed_data["figures"]),
        "concepts": len(seed_data["concepts"]),
        "edges": len(seed_data["edges"]),
    }
    for key, (lo, hi) in gran.items():
        assert lo <= actual[key] <= hi, (key, actual[key], lo, hi)
    assert actual == {
        "schools": 8, "figures": 40, "concepts": 50, "edges": 83
    }


def test_polarity_and_kind_enums(seed_data) -> None:
    polarity = set(seed_data["meta"]["enums"]["polarity"])
    kind = set(seed_data["meta"]["enums"]["kind"])
    assert polarity == {"继承", "批判"}
    for e in seed_data["edges"]:
        assert e["polarity"] in polarity
        assert e["kind"] in kind


def test_dual_anchors_complete(seed_data) -> None:
    """每概念双锚完备：维基条目名 + ctext 书名/篇名；边带出典篇目。"""
    for c in seed_data["concepts"]:
        assert c["anchors"]["wikipedia"]
        assert c["anchors"]["ctext"]["work"]
        assert c["anchors"]["ctext"]["chapter"]
    for e in seed_data["edges"]:
        assert e["anchors"]["ctext"]["work"]
        assert e["anchors"]["ctext"]["chapter"]
        assert e["evidence"]


def test_source_and_qid_fields(seed_data) -> None:
    """每条目带 zh.wikipedia 条目名（source）与 QID 键（未核实留空，不硬编）。"""
    for section in ("schools", "figures", "concepts"):
        for item in seed_data[section]:
            assert item["source"], (section, item["id"])
            assert "qid" in item
            if item["qid"]:
                assert item["qid"].startswith("Q") and item["qid"][1:].isdigit()
    qids = {f["id"]: f["qid"] for f in seed_data["figures"]}
    verified = {k for k, v in qids.items() if v}
    assert len(verified) >= 10  # 实测核过的 QID 群（2026-10-23 检索核实）
    assert qids["kongzi"] == "Q4604" and qids["laozi"] == "Q9333"


def test_seed_has_no_numeric_coordinates(seed_data) -> None:
    """种子是谱系坐标不是数值坐标：数据里不允许任何坐标/轴字段。"""
    for section in ("schools", "figures", "concepts", "edges"):
        for item in seed_data[section]:
            assert "coordinates" not in item and "axes" not in item


def test_every_figure_has_at_least_one_edge(seed_data) -> None:
    ids = {f["id"] for f in seed_data["figures"]}
    touched = {e["source"] for e in seed_data["edges"]} | {e["target"] for e in seed_data["edges"]}
    assert touched == ids  # 无孤立人物


def test_validate_seed_rejects_granularity_violation(seed_data) -> None:
    bad = json.loads(json.dumps(seed_data))
    bad["schools"] = bad["schools"][:3]  # 低于 6 家下限
    violations = validate_seed(bad)
    assert any("粒度越界" in v for v in violations)


def test_validate_seed_rejects_bad_polarity_and_missing_anchor(seed_data) -> None:
    bad = json.loads(json.dumps(seed_data))
    bad["edges"][0]["polarity"] = "中立"
    bad["concepts"][0]["anchors"].pop("ctext")
    violations = validate_seed(bad)
    assert any("极性" in v for v in violations)
    assert any("双锚不完备" in v for v in violations)


# ---------------------------------------------------------------- 入库与幂等


def test_load_china_seed_writes_marked_rows(conn, tmp_path) -> None:
    out = load_china_seed(conn, snapshot_root=tmp_path)
    assert out["is_seed"] is True
    assert (out["schools"], out["figures"], out["concepts"], out["edges"]) == (8, 40, 50, 83)

    currents = china_lineage.list_seed_currents(conn)
    assert len(currents) == 48  # 8 学派 + 40 人物
    assert all(c["is_seed"] and is_seed_current(c["current_id"]) for c in currents)
    assert all(c["coordinates"] is None for c in currents)  # 种子不造数值坐标
    assert all(c["snapshot_version"] == out["snapshot_id"] for c in currents)
    kinds = {c["kind"] for c in currents}
    assert kinds == {"school", "thinker"}

    concepts = conn.execute("SELECT term, url, snippet FROM concepts").fetchall()
    assert len(concepts) == 50
    assert all(is_seed_concept_url(r["url"]) for r in concepts)
    assert all(r["url"].startswith("https://zh.wikipedia.org/wiki/") for r in concepts)
    assert all("原典" in r["snippet"] for r in concepts)

    relations = china_lineage.list_seed_relations(conn)
    assert len(relations) == 83
    assert {r["kind"] for r in relations} <= {"influence", "critique", "debate"}
    assert {r["polarity"] for r in relations} == {"positive", "critical"}

    log = conn.execute(
        "SELECT detail FROM ingest_log WHERE source='china-seed'"
    ).fetchone()
    assert json.loads(log["detail"])["snapshot_id"] == out["snapshot_id"]


def test_load_china_seed_idempotent(conn, tmp_path) -> None:
    first = load_china_seed(conn, snapshot_root=tmp_path)
    second = load_china_seed(conn, snapshot_root=tmp_path)  # 同内容同快照
    assert first["snapshot_id"] == second["snapshot_id"]
    for table, expect in (("currents", 48), ("concepts", 50), ("relations", 83)):
        n = conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]
        assert n == expect, table  # 不重复插入


def test_load_china_seed_rejects_invalid(conn, tmp_path) -> None:
    bad = {
        "meta": {"version": "0", "enums": {"polarity": ["继承", "批判"], "kind": ["师承"]},
                 "granularity": {"schools": [6, 8], "figures": [30, 40], "concepts": [40, 60], "edges": [80, 120]}},
        "schools": [{"id": "x", "label": "X", "qid": "", "source": "X", "summary": ""}],
        "figures": [], "concepts": [], "edges": [],
    }
    p = tmp_path / "bad_seed.json"
    p.write_text(json.dumps(bad, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(SeedInvalid):
        load_china_seed(conn, seed_path=p, snapshot_root=tmp_path)


# ---------------------------------------------------------------- 近现代文献索引层


def test_modern_index_data_shape() -> None:
    assert len(MODERN_CURRENTS) == 5  # 五思潮
    labels = " ".join(c["label"] for c in MODERN_CURRENTS)
    for key in ("维新变法", "辛亥革命", "自由主义", "文化保守主义", "社会主义"):
        assert key in labels
    docs = [d for c in MODERN_CURRENTS for d in c["documents"]]
    assert len(docs) >= 20
    for d in docs:
        assert d["title"] and d["url"]
        assert d["form"] in {"原文全文", "原文全文合集", "期刊汇集页", "原刊扫描索引", "原书扫描"}
        assert d["url"].startswith(("https://zh.wikisource.org/", "https://commons.wikimedia.org/"))
    # 维基文库全文层 + Commons 扫描层双通道都在
    hosts = {d["url"].split("/")[2] for d in docs}
    assert "zh.wikisource.org" in hosts and "commons.wikimedia.org" in hosts


def test_load_modern_index_snapshots_and_currents(conn, tmp_path) -> None:
    out = load_modern_index(conn, snapshot_root=tmp_path)
    assert out["currents"] == 5 and out["documents"] >= 20

    snap = conn.execute(
        "SELECT snapshot_id, dataset, attribution FROM snapshots WHERE dataset='china-modern-index'"
    ).fetchone()
    assert snap["snapshot_id"] == out["snapshot_id"]
    assert "维基文库" in snap["attribution"] and "National Library of China" in snap["attribution"]

    rows = conn.execute(
        "SELECT current_id, kind, coordinates FROM currents WHERE current_id LIKE 'modern:china/%'"
    ).fetchall()
    assert len(rows) == 5
    assert all(r["kind"] == "current" and r["coordinates"] is None for r in rows)

    # 快照内容可读回（同快照一致解析）且含文献条目元数据
    manifest = conn.execute(
        "SELECT * FROM snapshots WHERE snapshot_id=?", (out["snapshot_id"],)
    ).fetchone()
    from fd_zhengxin_reference import snapshot as snapshot_mod

    payload = json.loads(snapshot_mod.read(manifest))
    assert len(payload["currents"]) == 5
    assert payload["meta"]["known_gaps"]  # 诚实缺口随快照固化


def test_load_modern_index_idempotent(conn, tmp_path) -> None:
    first = load_modern_index(conn, snapshot_root=tmp_path)
    second = load_modern_index(conn, snapshot_root=tmp_path)
    assert first["snapshot_id"] == second["snapshot_id"]
    n = conn.execute(
        "SELECT COUNT(*) AS c FROM currents WHERE current_id LIKE 'modern:china/%'"
    ).fetchone()["c"]
    assert n == 5
