"""实体读面（entity_dossier）：单实体全边 + 覆盖三态 + 解析歧义。

全离线：种子层走真实 `china_seed.json`（curated，无网络）；wikidata 面用
`ensure_tables` 建空表模拟「未摄取」，再用最小行插入模拟 available /
not_in_snapshot 两态。

覆盖三态是本文件的核心断言对象——它是 change design D2 的硬要求：
`not_in_snapshot`（快照在但无此实体）与 `not_ingested`（压根没快照）必须
分开，两者修复方向不同。
"""

from __future__ import annotations

import asyncio

import pytest

from fd_zhengxin_reference import china_lineage, wikidata_politics as wpx
from fd_zhengxin_reference.config import Settings
from fd_zhengxin_reference.server import bootstrap_curated, create_server


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("FDZX_REF_DB", str(tmp_path / "ref.db"))
    monkeypatch.setenv(
        "FDZX_REF_CHINA_SEED",
        str((__import__("pathlib").Path(__file__).resolve().parent.parent
             / "seed" / "china_seed.json")),
    )
    bootstrap_curated(Settings.from_env())


def _call(ref: str) -> dict:
    async def go():
        mcp = create_server()
        fn = next(t.fn for t in await mcp.list_tools() if t.name == "entity_dossier")
        return await fn(ref=ref)

    return asyncio.run(go())


# ---------------------------------------------------------------- 种子面（生产当前唯一可用面）

def test_seed_figure_full_edges() -> None:
    """种子人物：出入双向全边，带极性与学派（relation_query 做不到的那件事）。"""
    out = _call("孔子")
    assert out["resolution"]["status"] == "found"
    assert out["coverage"]["status"] == "available"
    assert out["identity"]["school"] == "儒家"
    assert out["identity"]["years"] == "前551–前479"
    assert out["relation_count"] > 0
    assert out["relations"]["out"] and out["relations"]["in"], "孔子应同时有出边与入边"
    for e in out["relations"]["out"] + out["relations"]["in"]:
        assert e["kind"] and e["polarity"] is not None
        assert e["ref"].startswith("china-seed:relation:")


def test_qid_resolves_to_seed_figure() -> None:
    """种子人物的 qid（Q4604）应解析到种子面而非空的 wikidata 面。"""
    out = _call("Q4604")
    assert out["resolution"]["status"] == "found"
    assert out["resolution"]["face"] == "china-seed"
    assert out["identity"]["label"] == "孔子"


def test_current_id_direct() -> None:
    """current_id 直填（seed:china/thinker/kongzi）与标签等价。"""
    assert _call("seed:china/thinker/kongzi")["identity"]["ref"] == \
           _call("孔子")["identity"]["ref"]


def test_modern_current_without_edges() -> None:
    """近现代思潮：实体可查、边为 0（不是错误，是数据面事实），仍报 available。"""
    out = _call("自由主义思潮")
    assert out["resolution"]["status"] == "found"
    assert out["coverage"]["status"] == "available"
    assert out["relation_count"] == 0


# ---------------------------------------------------------------- 未命中 / 歧义

def test_not_found_carries_reason() -> None:
    """未命中必带 reason（errors.py taxonomy），且 coverage 不为 available。"""
    out = _call("不存在的人")
    assert out["resolution"]["status"] == "not_found"
    assert out["reason"] == "not_ingested"
    assert out["coverage"]["status"] != "available"
    assert out["results"] == []


def test_ambiguous_returns_candidates_not_silent_pick() -> None:
    """同一标签在多个实体面命中 → 返回候选清单，不静默择一。

    spec 场景「标签解析歧义」：`_resolve_wd_entity` 的 `LIMIT 1` 会静默择一，
    实体读面必须显式拒选，否则用户拿到的是随机的那一个。
    """
    settings = Settings.from_env()
    snap = "wikidata-politics@test#amb"
    conn = settings.connect()
    try:
        wpx.ensure_tables(conn)
        conn.execute(
            "INSERT INTO snapshots (snapshot_id, dataset, version, fetched_at,"
            " sha256, probe_status, storage_path, created_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (snap, "wikidata-politics", "test", "2026-11-21T00:00:00+00:00",
             "amb", "available", "/tmp/x", "2026-11-21T00:00:00+00:00"),
        )
        # 与种子人物「孔子」同名的 wikidata 实体 → 两面各一命中
        conn.execute(
            "INSERT INTO wd_political_entities (qid, kind, label_zh, snapshot_version,"
            " ingested_at) VALUES (?,?,?,?,?)",
            ("Q999999", "politician", "孔子", snap, "2026-11-21T00:00:00+00:00"),
        )
        conn.commit()
    finally:
        conn.close()

    out = _call("孔子")
    assert out["resolution"]["status"] == "ambiguous", \
        f"期望歧义，实得 {out['resolution']}"
    faces = {c["face"] for c in out["resolution"]["candidates"]}
    assert {"china-seed", "wikidata"} <= faces
    assert out["results"] == []


# ---------------------------------------------------------------- 覆盖三态（核心）

def test_wikidata_not_ingested_when_no_snapshot() -> None:
    """wikidata 面无快照 → not_ingested（生产实测形态：0 行 + pod 无出口）。"""
    out = _call("Q15031")  # 习近平：种子无此 qid，只能走 wikidata 面
    assert out["coverage"]["status"] in ("not_ingested", "not_in_snapshot")
    assert out["reason"] == "not_ingested"
    detail = (out["coverage"].get("detail") or "").lower()
    assert "摄取" in detail or "不可达" in detail


def test_wikidata_not_in_snapshot_distinct_from_not_ingested(tmp_path) -> None:
    """两态必须可区分：有快照但无此实体 = not_in_snapshot，不等于 not_ingested。"""
    settings = Settings.from_env()
    conn = settings.connect()
    try:
        wpx.ensure_tables(conn)
        conn.execute(
            "INSERT INTO snapshots (snapshot_id, dataset, version, fetched_at,"
            " sha256, probe_status, storage_path, created_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            ("wikidata-politics@test#abc", "wikidata-politics", "test",
             "2026-11-21T00:00:00+00:00", "abc", "available", "/tmp/x",
             "2026-11-21T00:00:00+00:00"),
        )
        conn.commit()
    finally:
        conn.close()

    out = _call("Q15031")
    assert out["coverage"]["status"] == "not_in_snapshot"
    assert "补摄取" in (out["coverage"].get("detail") or "")


def test_wikidata_available_when_entity_in_snapshot(tmp_path) -> None:
    """实体在快照内 → available + 关系边从 wd_political_relations 召回。"""
    settings = Settings.from_env()
    snap = "wikidata-politics@test#abc"
    conn = settings.connect()
    try:
        wpx.ensure_tables(conn)
        conn.execute(
            "INSERT INTO snapshots (snapshot_id, dataset, version, fetched_at,"
            " sha256, probe_status, storage_path, created_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (snap, "wikidata-politics", "test", "2026-11-21T00:00:00+00:00",
             "abc", "available", "/tmp/x", "2026-11-21T00:00:00+00:00"),
        )
        conn.executemany(
            "INSERT INTO wd_political_entities (qid, kind, label_en, label_zh,"
            " snapshot_version, ingested_at) VALUES (?,?,?,?,?,?)",
            [
                ("Q15031", "politician", "Xi Jinping", None, snap, "2026-11-21T00:00:00+00:00"),
                ("Q17427", "party", "CCP", None, snap, "2026-11-21T00:00:00+00:00"),
            ],
        )
        conn.execute(
            "INSERT INTO wd_political_relations (source_id, target_id, property_pid,"
            " kind, polarity, weight, label_source, label_target, snapshot_version,"
            " ingested_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("Q15031", "Q17427", "P102", "party_membership", "alliance", 1.0,
             "Xi Jinping", "CCP", snap, "2026-11-21T00:00:00+00:00"),
        )
        conn.commit()
    finally:
        conn.close()

    out = _call("Q15031")
    assert out["coverage"]["status"] == "available"
    assert out["identity"]["kind"] == "politician"
    assert out["relation_count"] == 1
    edge = (out["relations"]["out"] or out["relations"]["in"])[0]
    assert edge["pid"] == "P102"
