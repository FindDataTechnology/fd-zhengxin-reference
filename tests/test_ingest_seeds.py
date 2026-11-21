"""摄取脚本：delta 导出只带 wikidata 表、不碰别的源、能独立复现。

这个脚本的价值全在「不重踩四个坑」（见记忆 wikidata-ingest-four-traps）。
测试守的是它最容易腐的两处：导出的表集合、元数据过滤条件。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from fd_zhengxin_reference import schema
from scripts.ingest_seeds import DELTA_TABLES, META_FILTERS, export_delta


def _make_source(db: Path) -> None:
    """造一个带「别的源」数据的库，验证导出不会把它们带走。"""
    conn = sqlite3.connect(db)
    try:
        schema.init(conn)
        # wikidata 面
        for ddl in (
            "CREATE TABLE IF NOT EXISTS wd_political_entities (qid TEXT PRIMARY KEY, kind TEXT)",
            "CREATE TABLE IF NOT EXISTS wd_political_relations (rid INTEGER PRIMARY KEY, source_id TEXT)",
            "CREATE TABLE IF NOT EXISTS wd_ideology_anchors (qid TEXT, anchor_label TEXT)",
        ):
            conn.execute(ddl)
        conn.execute("INSERT INTO wd_political_entities VALUES ('Q15031','politician')")
        conn.execute("INSERT INTO wd_political_relations VALUES (1,'Q15031')")
        conn.execute("INSERT INTO wd_ideology_anchors VALUES ('Q15031','共产主义')")
        # 别的源：必须不被带走
        conn.execute(
            "INSERT INTO snapshots (snapshot_id, dataset, version, fetched_at, sha256,"
            " probe_status, storage_path, created_at) VALUES (?,?,?,?,?,?,?,?)",
            ("wd#1", "wikidata-politics", "v", "2026-11-22", "x", "available", "/p", "t"),
        )
        conn.execute(
            "INSERT INTO snapshots (snapshot_id, dataset, version, fetched_at, sha256,"
            " probe_status, storage_path, created_at) VALUES (?,?,?,?,?,?,?,?)",
            ("vdem#1", "v-dem", "v16", "2026-11-22", "y", "available", "/p", "t"),
        )
        conn.execute(
            "INSERT INTO ingest_log (source, status, detail, as_of) VALUES (?,?,?,?)",
            ("wikidata", "ok", "{}", "2026-11-22"),
        )
        conn.execute(
            "INSERT INTO ingest_log (source, status, detail, as_of) VALUES (?,?,?,?)",
            ("v-dem", "ok", "{}", "2026-11-22"),
        )
        conn.commit()
    finally:
        conn.close()


def test_delta_excludes_other_sources(tmp_path) -> None:
    src = tmp_path / "src.db"
    out = tmp_path / "delta.db"
    _make_source(src)
    counts = export_delta(str(src), str(out))

    assert counts["wd_political_entities"] == 1
    conn = sqlite3.connect(out)
    try:
        datasets = {r[0] for r in conn.execute("SELECT dataset FROM snapshots")}
        logs = {r[0] for r in conn.execute("SELECT source FROM ingest_log")}
    finally:
        conn.close()
    assert datasets == {"wikidata-politics"}      # v-dem 快照没跟过来
    assert logs == {"wikidata"}                   # v-dem 台账没跟过来


def test_delta_does_not_carry_core_tables(tmp_path) -> None:
    """concepts/currents/relations/regime_indicators 不导——那是别的源，pod 内不该被覆盖。"""
    src = tmp_path / "src.db"
    out = tmp_path / "delta.db"
    _make_source(src)
    export_delta(str(src), str(out))
    conn = sqlite3.connect(out)
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()
    for core in ("concepts", "currents", "relations", "regime_indicators"):
        assert core not in tables, f"{core} 不该出现在 delta 里"
    assert set(DELTA_TABLES) <= tables


def test_delta_tables_constant_matches_reality() -> None:
    """DELTA_TABLES 是本脚本的核心契约，改动必须显式。"""
    assert DELTA_TABLES == (
        "wd_political_entities", "wd_political_relations", "wd_ideology_anchors",
    )
    assert META_FILTERS["snapshots"] == "dataset='wikidata-politics'"
    assert META_FILTERS["ingest_log"] == "source='wikidata'"
