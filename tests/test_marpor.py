"""MARPOR 许可门测试：关门形态（license_unconfirmed）。

覆盖：ingest 双拒（不读文件、不写库——喂不存在的路径也只得到门拒绝）、
query 双口径（reason=not_ingested + gate_status=license_unconfirmed）、
require_license 抛错；另证被封住的真摄取骨架是真实逻辑（纯函数单测），
而非空壳——门开即可用，但门在 registry 翻转前恒关。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from fd_zhengxin_reference import marpor
from fd_zhengxin_reference.config import Settings
from fd_zhengxin_reference.errors import NOT_INGESTED
from fd_zhengxin_reference.license import LicenseNotConfirmed, require_license

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


def test_gate_closed_ingest_rejects(mpds_csv, env):
    """ingest 双拒：license_unconfirmed + 三点确认清单 + ingested=False。"""
    settings, _ = env
    out = marpor.ingest_marpor(mpds_csv, settings=settings)
    assert out["source"] == "marpor"
    assert out["status"] == "not_ingested"
    assert out["ingested"] is False
    # 台账口径缺口原因：license_unconfirmed（区别于查询口径，见 errors.py）
    assert out["reason"] == "license_unconfirmed"
    assert out["gate_status"] == "pending"
    assert out["gate_detail"]  # Terms of Use 概要
    # 三点待人工确认清单随拒绝结果给出（不静默）
    constraints = " ".join(out["gate_constraints"])
    assert "scientific" in constraints or "注册" in constraints
    assert "书面" in constraints or "Redistribution" in constraints
    assert "docs/data-license.md" in out["evidence"]


def test_gate_before_io_nonexistent_path(env):
    """门在 IO 之前：不存在的路径也只得到门拒绝（证明门先于文件读取）。"""
    settings, _ = env
    out = marpor.ingest_marpor("/nonexistent/MPDS2026a.csv", settings=settings)
    assert out["ingested"] is False
    assert out["reason"] == "license_unconfirmed"


def test_gate_closed_ingest_writes_nothing(mpds_csv, env):
    """零副作用：拒摄取后库与快照根均无 marpor 痕迹。"""
    settings, tmp_path = env
    for _ in range(2):  # 多次尝试同样零副作用
        marpor.ingest_marpor(mpds_csv, settings=settings)
    assert table_count(settings, "regime_indicators") == 0
    assert table_count(settings, "ingest_log") == 0
    assert table_count(settings, "snapshots") == 0
    assert not (tmp_path / "snapshots" / "marpor").exists()


def test_gate_closed_query_rejects(env):
    """query 双拒：查询口径 not_ingested + 台账口径 gate_status=license_unconfirmed。"""
    settings, _ = env
    out = marpor.query_marpor_timeseries("USA", (2015, 2020), settings=settings)
    assert out["results"] == []
    assert out["reason"] == NOT_INGESTED  # 查询口径（errors.py taxonomy）
    assert out["gate_status"] == "license_unconfirmed"  # 台账口径补充字段
    assert out["gate_detail"]
    assert out["hint"]  # 空结果必带原因说明


def test_require_license_marpor_raises():
    """许可门本体：marpor 未确认 → require_license 抛 LicenseNotConfirmed。"""
    with pytest.raises(LicenseNotConfirmed) as excinfo:
        require_license("marpor")
    assert "marpor" in str(excinfo.value)
    assert excinfo.value.reason  # 拒绝原因非空


def test_sealed_parse_skeleton_is_real(mpds_csv):
    """被封住的真摄取骨架是真实逻辑（纯函数直测，不经门也不开门）：
    party 级行解析 + 国家-年 rile 均值聚合。"""
    rows = list(marpor.parse_mpds_rows(mpds_csv.read_bytes()))
    assert len(rows) == 4
    usa = [r for r in rows if r["country"] == "USA"]
    assert {r["party"] for r in usa} == {"62220", "62230"}
    assert rows[0]["version"] == "2026a"

    means = marpor._mpds_country_year_means(rows)
    assert means[("USA", 2020)] == pytest.approx((-10.5 + 20.3) / 2)
    assert means[("DEU", 2017)] == pytest.approx((-27.0 + 10.0) / 2)


def test_sealed_parse_rejects_bad_schema(tmp_path):
    """骨架解析同样守结构口径：缺 MPDS 必备列即抛错，不静默。"""
    bad = tmp_path / "bad.csv"
    bad.write_text("a,b\n1,2\n", encoding="utf-8")
    with pytest.raises(marpor.MpdsFormatError):
        list(marpor.parse_mpds_rows(bad.read_bytes()))
