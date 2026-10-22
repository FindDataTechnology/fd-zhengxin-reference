"""MARPOR 许可门测试：开门形态（2026-10-23 产品方裁决翻 confirmed）。

覆盖：门开 → fixture MPDS 真摄取成功（快照版本化+国家-年 rile 均值入库）+ 幂等；
查询带数据返回行与数据集版本标注；无数据时空结果 not_ingested（纯数据口径，
无 license_unconfirmed）；坏 schema 拒收并写台账；解析骨架纯函数单测保留。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from fd_zhengxin_reference import marpor
from fd_zhengxin_reference.config import Settings
from fd_zhengxin_reference.errors import NOT_INGESTED
from fd_zhengxin_reference.license import license_confirmed, require_license

MPDS_CSV = """country,countryname,party,partyname,edate,date,rile,version
USA,United States,62220,Democratic Party,20201103,2020,-10.5,2026a
USA,United States,62230,Republican Party,20201103,2020,20.3,2026a
DEU,Germany,41920,SPD,20170924,2017,-27.0,2026a
DEU,Germany,41930,CDU,20170924,2017,10.0,2026a
"""


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """隔离环境：独立 DB + 独立快照根。"""
    monkeypatch.setenv("FDZX_SNAPSHOT_DIR", str(tmp_path / "snapshots"))
    return Settings(db_path=str(tmp_path / "ref.db")), tmp_path


@pytest.fixture()
def mpds_csv(tmp_path) -> Path:
    p = tmp_path / "MPDataset_MPDS2026a.csv"
    p.write_text(MPDS_CSV, encoding="utf-8")
    return p


def table_count(settings: Settings, table: str) -> int:
    conn: sqlite3.Connection = settings.connect()
    try:
        from fd_zhengxin_reference import schema

        schema.init(conn)
        return conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]
    finally:
        conn.close()


def test_gate_open_registry_confirmed():
    """2026-10-23 用户裁决翻门：registry marpor=confirmed，require_license 放行。"""
    assert license_confirmed("marpor") is True
    require_license("marpor")  # 不抛即过


def test_ingest_open_success(mpds_csv, env):
    """门开 → fixture MPDS 真摄取：均值入库 + 快照版本化 + 台账 ok。"""
    settings, _ = env
    out = marpor.ingest_marpor(mpds_csv, settings=settings)
    assert out["source"] == "marpor"
    assert out["status"] == "ok", out
    assert out["ingested"] is True
    assert out["party_rows"] == 4
    assert out["country_year_rows"] == 2  # USA-2020 + DEU-2017 均值
    assert "2026a" in (out.get("version") or "")
    assert out.get("snapshot_id")
    assert table_count(settings, "regime_indicators") > 0
    assert table_count(settings, "snapshots") == 1


def test_ingest_open_idempotent(mpds_csv, env):
    """幂等：同内容重复摄取复用快照，行数一致。"""
    settings, _ = env
    first = marpor.ingest_marpor(mpds_csv, settings=settings)
    second = marpor.ingest_marpor(mpds_csv, settings=settings)
    assert second["snapshot_id"] == first["snapshot_id"]
    assert second["party_rows"] == first["party_rows"]


def test_query_with_data(mpds_csv, env):
    """摄取后查询：国家-年均值行 + 数据集版本标注。"""
    settings, _ = env
    marpor.ingest_marpor(mpds_csv, settings=settings)
    out = marpor.query_marpor_timeseries("USA", (2015, 2021), settings=settings)
    assert out["results"], "USA 2020 应有均值行"
    row = out["results"][0]
    assert row["year"] == 2020
    assert row["value"] == pytest.approx((-10.5 + 20.3) / 2)
    assert "2026a" in (out.get("dataset_version") or out.get("version") or "")


def test_query_without_data_is_not_ingested(env):
    """门开但未摄取：空结果纯数据口径 not_ingested（无 license_unconfirmed 字段）。"""
    settings, _ = env
    out = marpor.query_marpor_timeseries("USA", (2015, 2020), settings=settings)
    assert out["results"] == []
    assert out["reason"] == NOT_INGESTED
    assert "gate_status" not in out  # 门已开，不再带台账口径字段


def test_ingest_open_missing_path_raises(env):
    """门开后路径缺失如实抛 FileNotFoundError（不再被门拒绝掩盖）。"""
    settings, _ = env
    with pytest.raises(FileNotFoundError):
        marpor.ingest_marpor("/nonexistent/MPDS2026a.csv", settings=settings)


def test_ingest_open_bad_schema_logged(tmp_path, env):
    """坏 schema 拒收：status=failed/reason=upstream 并写台账，不静默。"""
    settings, _ = env
    bad = tmp_path / "bad.csv"
    bad.write_text("a,b\n1,2\n", encoding="utf-8")
    out = marpor.ingest_marpor(bad, settings=settings)
    assert out["status"] == "failed"
    assert out["reason"] == "upstream"
    assert table_count(settings, "ingest_log") >= 1


def test_parse_skeleton_is_real(mpds_csv):
    """解析骨架纯函数直测：party 级行 + 国家-年 rile 均值聚合。"""
    rows = list(marpor.parse_mpds_rows(mpds_csv.read_bytes()))
    assert len(rows) == 4
    usa = [r for r in rows if r["country"] == "USA"]
    assert {r["party"] for r in usa} == {"62220", "62230"}
    assert rows[0]["version"] == "2026a"

    means = marpor._mpds_country_year_means(rows)
    assert means[("USA", 2020)] == pytest.approx((-10.5 + 20.3) / 2)
    assert means[("DEU", 2017)] == pytest.approx((-27.0 + 10.0) / 2)


def test_parse_rejects_bad_schema(tmp_path):
    """解析守结构口径：缺 MPDS 必备列即抛错，不静默。"""
    bad = tmp_path / "bad.csv"
    bad.write_text("a,b\n1,2\n", encoding="utf-8")
    with pytest.raises(marpor.MpdsFormatError):
        list(marpor.parse_mpds_rows(bad.read_bytes()))
