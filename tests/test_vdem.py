"""V-Dem 摄取器测试：合成 fixture CSV（10 国 × 30 年，官方 schema 列名）。

覆盖：摄取入库与口径（v2x_* 列筛选、空值跳过、版本探测）、摄取幂等
（同内容同快照）、查询时序（年份区间 + 指标口径说明 + provenance）、
国名解析、空结果纪律（reason=not_ingested + hint）。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from fd_zhengxin_reference import vdem
from fd_zhengxin_reference.config import Settings
from fd_zhengxin_reference.errors import NOT_INGESTED

# 合成名录：country_text_id -> country_name（官方列名）
COUNTRIES = [
    ("USA", "United States of America"),
    ("CHN", "China"),
    ("DEU", "Germany"),
    ("FRA", "France"),
    ("GBR", "United Kingdom"),
    ("IND", "India"),
    ("BRA", "Brazil"),
    ("ZAF", "South Africa"),
    ("JPN", "Japan"),
    ("MEX", "Mexico"),
]
YEARS = list(range(1991, 2021))  # 30 年
INDICATORS = [
    "v2x_polyarchy",
    "v2x_libdem",
    "v2x_partipdem",
    "v2x_delibdem",
    "v2x_egaldem",
]
# GBR/2005 整行指标空串（官方 CSV 以空串表缺失）→ 该国-年 5 个值全部跳过
BLANK_CELL = ("GBR", 2005)


def expected_value(code: str, year: int, indicator: str) -> float:
    """确定性合成值（0-1 连续，贴合 v2x_* 量表）。"""
    ci = next(i for i, (c, _) in enumerate(COUNTRIES) if c == code)
    yi = year - YEARS[0]
    base = (ci * 131 + yi * 17 + len(indicator) * 7) % 1001
    return round(base / 1000.0, 3)


def write_vdem_csv(path: Path, *, version_column: str = "v16") -> Path:
    header = (
        ["country_id", "country_text_id", "country_name", "year"]
        + INDICATORS
        + ["historical_date", "version", "project"]
    )
    lines = [",".join(header)]
    for code, name in COUNTRIES:
        for year in YEARS:
            values = []
            for ind in INDICATORS:
                if (code, year) == BLANK_CELL:
                    values.append("")  # 空串=缺失
                else:
                    values.append(f"{expected_value(code, year, ind):.3f}")
            lines.append(
                ",".join(
                    [str(abs(hash(code)) % 1000), code, name, str(year)]
                    + values
                    + [f"{year}-01-01", version_column, "V-Dem"]
                )
            )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """隔离环境：独立 DB + 独立快照根（不污染 ~/.fd-zhengxin）。"""
    monkeypatch.setenv("FDZX_SNAPSHOT_DIR", str(tmp_path / "snapshots"))
    return Settings(db_path=str(tmp_path / "ref.db")), tmp_path


@pytest.fixture()
def vdem_csv(tmp_path) -> Path:
    return write_vdem_csv(
        tmp_path / "V-Dem-CY-Core-v16.csv", version_column="v16"
    )


def db_count(settings: Settings, table: str) -> int:
    conn: sqlite3.Connection = settings.connect()
    try:
        return conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]
    finally:
        conn.close()


def test_ingest_ok(vdem_csv, env):
    """摄取：长表入库（国家-年×指标）、v2x_* 筛选、空值跳过、版本探测。"""
    settings, _ = env
    out = vdem.ingest_vdem(vdem_csv, settings=settings)
    assert out["status"] == "ok" and out["ingested"] is True
    assert out["countries"] == 10
    assert out["indicators"] == INDICATORS
    # 10 国 × 30 年 × 5 指标 − GBR/2005 整行 5 个空值
    assert out["rows"] == 10 * 30 * 5 - 5
    assert out["skipped_values"] == 5
    assert out["version"] == "v16"  # CSV version 列
    assert out["snapshot_id"].startswith("v-dem@v16#")
    assert db_count(settings, "regime_indicators") == out["rows"]
    # 归属：CC BY-SA 4.0 + v-dem.net + 官网建议引用 DOI
    assert "CC BY-SA 4.0" in out["attribution"]
    assert "v-dem.net" in out["attribution"]
    assert "10.23696/vdemds26" in out["attribution"]
    assert out["provenance"]["snapshot_version"] == out["snapshot_id"]


def test_ingest_idempotent(vdem_csv, env):
    """幂等：同内容重复摄取 → 复用同一快照（内容寻址）、行数不变。"""
    settings, tmp_path = env
    first = vdem.ingest_vdem(vdem_csv, settings=settings)
    second = vdem.ingest_vdem(vdem_csv, settings=settings)
    assert second["status"] == "ok"
    assert second["snapshot_id"] == first["snapshot_id"]
    assert second["rows"] == first["rows"]
    assert db_count(settings, "regime_indicators") == first["rows"]
    # 快照根只落一份内容（同 sha256 复用路径，不堆副本）
    snap_root = tmp_path / "snapshots" / "v-dem"
    assert len(list(snap_root.iterdir())) == 1


def test_version_param_overrides_and_fallback(env, tmp_path):
    """版本优先级：参数 > CSV version 列 > 文件名 > unknown。"""
    settings, _ = env
    csv1 = write_vdem_csv(tmp_path / "V-Dem-CY-Core-v16.csv", version_column="v16")
    out = vdem.ingest_vdem(csv1, settings=settings, version="v16.1")
    assert out["version"] == "v16.1"
    assert out["snapshot_id"].startswith("v-dem@v16.1#")
    # 无 version 列 → 文件名兜底
    csv2 = write_vdem_csv(tmp_path / "V-Dem-CY-Core-v15.csv", version_column="")
    out2 = vdem.ingest_vdem(csv2, settings=settings)
    assert out2["version"] == "v15"
    # 无 version 列、文件名无模式 → unknown
    csv3 = write_vdem_csv(tmp_path / "vdem.csv", version_column="")
    out3 = vdem.ingest_vdem(csv3, settings=settings)
    assert out3["version"] == "unknown"


def test_query_timeseries(vdem_csv, env):
    """查询：年份闭区间时序 + 指标值与 fixture 一致 + 口径说明 + provenance。"""
    settings, _ = env
    ing = vdem.ingest_vdem(vdem_csv, settings=settings)
    out = vdem.query_regime_timeseries("USA", (1995, 1999), settings=settings)
    assert [r["year"] for r in out["results"]] == [1995, 1996, 1997, 1998, 1999]
    for r in out["results"]:
        assert r["country_code"] == "USA"
        assert set(r["indicators"]) == set(INDICATORS)
        for ind, val in r["indicators"].items():
            assert val == pytest.approx(expected_value("USA", r["year"], ind))
    # 指标口径说明：五大指数逐一给口径，非空
    assert set(out["indicator_notes"]) == set(INDICATORS)
    assert all(out["indicator_notes"][ind] for ind in INDICATORS)
    assert "0-1" in out["indicator_notes"]["v2x_polyarchy"]
    # provenance：source/stable_id/attribution/snapshot_version（回到具体快照）
    prov = out["provenance"]
    assert prov["source"] == "v-dem"
    assert prov["stable_id"] == "v-dem:regime:USA"
    assert prov["snapshot_version"] == ing["snapshot_id"]
    assert "CC BY-SA 4.0" in prov["attribution"]


def test_query_indicator_filter_and_years_set(vdem_csv, env):
    """查询：显式年份集合 + 单指标过滤。"""
    settings, _ = env
    vdem.ingest_vdem(vdem_csv, settings=settings)
    out = vdem.query_regime_timeseries(
        "CHN", [1991, 2000, 2010], indicator="v2x_libdem", settings=settings
    )
    assert [r["year"] for r in out["results"]] == [1991, 2000, 2010]
    for r in out["results"]:
        assert list(r["indicators"]) == ["v2x_libdem"]
    assert list(out["indicator_notes"]) == ["v2x_libdem"]


def test_query_by_country_name(vdem_csv, env):
    """国名解析：官方英文全称（大小写不敏感）经快照名录落到 country_text_id。"""
    settings, _ = env
    ing = vdem.ingest_vdem(vdem_csv, settings=settings)
    out = vdem.query_regime_timeseries(
        "united states of america", 1991, settings=settings
    )
    assert len(out["results"]) == 1
    assert out["results"][0]["country_code"] == "USA"
    assert out["results"][0]["country_name"] == "United States of America"
    assert out["provenance"]["snapshot_version"] == ing["snapshot_id"]


def test_query_blank_cell_missing_year(vdem_csv, env):
    """空值纪律：GBR/2005 指标空串 → 该年无行（跳过不落 NULL），其余年在。"""
    settings, _ = env
    vdem.ingest_vdem(vdem_csv, settings=settings)
    out = vdem.query_regime_timeseries("GBR", (2004, 2006), settings=settings)
    assert [r["year"] for r in out["results"]] == [2004, 2006]


def test_query_before_ingest(env):
    """未摄取：空结果 + not_ingested + hint（许可门已过、数据未落库）。"""
    settings, _ = env
    out = vdem.query_regime_timeseries("USA", (1990, 2000), settings=settings)
    assert out["results"] == []
    assert out["reason"] == NOT_INGESTED
    assert "尚未摄取" in out["hint"]


def test_query_unknown_country(vdem_csv, env):
    """已摄取但国家未命中：空结果 + not_ingested + hint 指明未匹配。"""
    settings, _ = env
    vdem.ingest_vdem(vdem_csv, settings=settings)
    out = vdem.query_regime_timeseries("Atlantis", (1990, 2020), settings=settings)
    assert out["results"] == []
    assert out["reason"] == NOT_INGESTED
    assert "Atlantis" in out["hint"]


def test_ingest_bad_schema(env, tmp_path):
    """结构异常：缺必备列 → status=failed（reason=upstream）+ 台账记账，不静默。"""
    import json

    settings, _ = env
    bad = tmp_path / "not-vdem.csv"
    bad.write_text("foo,bar\n1,2\n", encoding="utf-8")
    out = vdem.ingest_vdem(bad, settings=settings)
    assert out["status"] == "failed"
    assert out["reason"] == "upstream"
    assert out["ingested"] is False
    assert db_count(settings, "regime_indicators") == 0
    conn = settings.connect()
    try:
        row = conn.execute(
            "SELECT status, detail FROM ingest_log WHERE source='v-dem'"
        ).fetchone()
    finally:
        conn.close()
    assert row["status"] == "failed"
    assert json.loads(row["detail"])["reason"] == "upstream"
