"""MARPOR（Manifesto Project）摄取器——关门形态。

许可门第一（docs/data-license.md §2 / LICENSE_REGISTRY["marpor"] status=pending）：
Terms of Use 明文 "Redistribution … is forbidden except when … authorized in
writing"，且三点待人工确认（主数据集下载是否强制注册 / 产品语境是否满足
scientific research / 查询导出面是否构成 Redistribution）→ license_unconfirmed。

关门纪律（本模块的契约）：
- ingest_marpor 首查许可门 → 拒摄取，返回带 reason 的结构化结果；**不读文件、
  不写库、不走解析**（喂一个不存在的路径也只得到门拒绝，证明门在 IO 之前）。
- query_marpor_timeseries 同拒：查询口径 reason=not_ingested（errors.py taxonomy），
  另附台账口径 gate_status=license_unconfirmed（两层口径不混用，见 errors.py）。
- 真摄取逻辑已就位但被门封住（_parse_mpds_rows / _mpds_country_year_means /
  _ingest_marpor_open）：三点确认落定、registry 翻为 confirmed 之前不可达。

数据形状（MPDS CSV：party × 年 × rile 维度表）：country / countryname / party /
partyname / edate（选举日）/ date（选举年）/ rile（左右立场综合分）等。
regime_indicators 是国家-年表，party 细粒度入表需后续波次扩 party_indicators 表；
门开前的骨架按国家-年对 rile 取均值（聚合口径在台账 detail 注明，不冒充 party 级）。
"""

from __future__ import annotations

import csv
import io
import json
import re
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

from . import schema, snapshot
from .config import Settings
from .errors import NOT_INGESTED, UPSTREAM, empty_result
from .license import (
    LicenseNotConfirmed,
    license_confirmed,
    license_entry,
    not_ingested_reason,
    require_license,
)
from .provenance import ATTRIBUTIONS, CITATION_URLS, provenance

SOURCE = "marpor"    # 与 LICENSE_REGISTRY / tools_registry.SOURCES 同名
DATASET = "marpor"   # 快照 dataset 名（台账按源名查快照，须一致）
DATA_PAGE = f"{CITATION_URLS['marpor']}/datasets/MPDS2026a"
DOI = "https://doi.org/10.25522/manifesto.mpds.2026a"  # MPDS2026a 推荐引用（引用型 URL）

# MPDS CSV 必备列（缺任一即结构异常）
MPDS_REQUIRED_COLUMNS = ("country", "party", "date", "rile")
# 立场指标列（party 级；骨架先接 rile，其余域分量后续波次扩列）
MPDS_POSITION_INDICATORS = ("rile",)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _attribution(version: str) -> str:
    """归属声明：MPDS 推荐引用 + Terms of Use（再分发须书面授权）。"""
    return (
        f"{ATTRIBUTIONS[SOURCE]}；建议引用：Lehmann, Pola / Franzmann, Simon / … / "
        f"Zehnter, Lisa ({version[:4] if version[:4].isdigit() else '2026'}): "
        f"\"The Manifesto Data Collection. Manifesto Project (MRG/CMP/MARPOR). "
        f"Version {version}.\" Berlin: WZB / Göttingen: IfDem. {DOI}"
    )


class MpdsFormatError(ValueError):
    """CSV 结构不符合 MPDS schema（缺必备列 / 无数据行）。"""


def parse_mpds_rows(content: bytes) -> Iterator[dict]:
    """解析 MPDS CSV → party 级行（纯函数，门无关，供骨架单测）。

    行形：{country, countryname, party, partyname, year, rile, version}；
    国家/年份/rile 缺失的行跳过（计数不入——骨架层以能入行数为准）。
    """
    reader = csv.DictReader(io.StringIO(content.decode("utf-8-sig")))
    header = list(reader.fieldnames or [])
    missing = [c for c in MPDS_REQUIRED_COLUMNS if c not in header]
    if missing:
        raise MpdsFormatError(f"缺 MPDS 必备列 {missing}（header={header[:8]}…）")
    for rec in reader:
        country = (rec.get("country") or "").strip()
        # MPDS date 是 YYYYMM（选举月，如 202011=2020-11）；旧版纯年份（≤4 位）兼容
        try:
            dv = int(float((rec.get("date") or "").strip()))
        except ValueError:
            continue
        year = dv // 100 if dv > 9999 else dv
        if not 1700 <= year <= 2100:
            continue
        raw_rile = (rec.get("rile") or "").strip()
        if not country or not raw_rile:
            continue
        try:
            rile = float(raw_rile)
        except ValueError:
            continue
        yield {
            "country": country,
            "countryname": (rec.get("countryname") or "").strip(),
            "party": (rec.get("party") or "").strip(),
            "partyname": (rec.get("partyname") or "").strip(),
            "year": year,
            "rile": rile,
            "version": (rec.get("version") or "").strip(),
        }


def _mpds_country_year_means(rows: list[dict]) -> dict[tuple[str, int], float]:
    """party 级行 → (country, year) 对 rile 取均值（国家-年聚合口径）。"""
    sums: dict[tuple[str, int], float] = {}
    counts: dict[tuple[str, int], int] = {}
    for r in rows:
        key = (r["country"], r["year"])
        sums[key] = sums.get(key, 0.0) + r["rile"]
        counts[key] = counts.get(key, 0) + 1
    return {k: sums[k] / counts[k] for k in sums}


def _log(conn, status: str, detail: dict) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO ingest_log (source, status, detail, as_of)"
        " VALUES (?,?,?,?)",
        (SOURCE, status, json.dumps(detail, ensure_ascii=False), _now()),
    )
    conn.commit()


def ingest_marpor(
    path: str | Path, *, settings: Settings | None = None
) -> dict:
    """MARPOR 摄取入口：许可门第一，门不开即拒（结构化结果，零副作用）。

    关门形态：LICENSE_REGISTRY["marpor"] status=pending → 返回 not_ingested +
    license_unconfirmed（台账口径）+ 三点确认清单；不读文件、不写库。
    门开后的真摄取走 _ingest_marpor_open（当前不可达：registry 翻转需人工评审）。
    """
    try:
        require_license(SOURCE)
    except LicenseNotConfirmed as exc:
        reason, gate_detail = not_ingested_reason(SOURCE)  # license_unconfirmed
        entry = license_entry(SOURCE) or {}
        return {
            "source": SOURCE,
            "status": "not_ingested",
            "ingested": False,
            # 台账口径缺口原因（license_unconfirmed 属台账字段，不进查询口径）
            "reason": reason,
            "gate_status": entry.get("status"),
            "gate_detail": gate_detail,
            "gate_constraints": entry.get("constraints", []),
            "evidence": entry.get("evidence"),
            "path": str(path),
            "detail": str(exc),
        }
    return _ingest_marpor_open(path, settings=settings)


def _ingest_marpor_open(path: str | Path, *, settings: Settings | None) -> dict:
    """门开后的真摄取（骨架就位；被许可门封住，registry 翻转前不可达）。"""
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"MPDS CSV 不存在：{path}")
    settings = settings or Settings.from_env()
    content = path.read_bytes()
    try:
        rows = list(parse_mpds_rows(content))
        if not rows:
            raise MpdsFormatError("CSV 无可入行（party/date/rile 全空？）")
    except MpdsFormatError as exc:
        conn = settings.connect()
        try:
            schema.init(conn)
            _log(conn, "failed", {"reason": UPSTREAM, "detail": str(exc)})
        finally:
            conn.close()
        return {
            "source": SOURCE,
            "status": "failed",
            "reason": UPSTREAM,
            "ingested": False,
            "detail": str(exc),
            "path": str(path),
        }

    version = rows[0]["version"] or (
        m.group(0) if (m := re.search(r"MPDS\d{4}[a-z]", path.name, re.I)) else "unknown"
    )
    manifest = snapshot.store(
        DATASET,
        version,
        content,
        source_url=DATA_PAGE,
        attribution=_attribution(version),
        probe_status="available",
        filename=path.name,
    )
    means = _mpds_country_year_means(rows)
    conn = settings.connect()
    try:
        schema.init(conn)
        snapshot.record(conn, manifest)
        ts = _now()
        conn.executemany(
            "INSERT OR REPLACE INTO regime_indicators"
            " (country_code, year, indicator, value, ingested_at, snapshot_version)"
            " VALUES (?,?,?,?,?,?)",
            [
                (country, year, "rile", mean, ts, manifest.snapshot_id)
                for (country, year), mean in means.items()
            ],
        )
        _log(
            conn,
            "ok",
            {
                "party_rows": len(rows),
                "country_year_rows": len(means),
                "aggregate": "party→国家-年 rile 均值（party 细粒度待 party_indicators 表）",
                "version": version,
                "snapshot_id": manifest.snapshot_id,
                "file": path.name,
            },
        )
    finally:
        conn.close()
    return {
        "source": SOURCE,
        "status": "ok",
        "ingested": True,
        "party_rows": len(rows),
        "country_year_rows": len(means),
        "version": version,
        "snapshot_id": manifest.snapshot_id,
        "attribution": manifest.attribution,
        "provenance": provenance(
            SOURCE,
            stable_id=f"marpor:snapshot:{manifest.snapshot_id}",
            snapshot_version=manifest.snapshot_id,
        ),
    }


def query_marpor_timeseries(
    country: str,
    years: tuple[int, int] | None = None,
    *,
    settings: Settings | None = None,
) -> dict:
    """MARPOR 思潮坐标查询：国家-年 rile 均值序列。

    - 门未过：恒空结果 + 双层口径原因（license_unconfirmed，过门前绝不编造）
    - 门已过未摄取：空结果 not_ingested（纯数据口径）
    - 已摄取：country 收 code（USA/DEU）或国名（从快照原始 CSV 建 code↔name
      映射解析），返回 rile 序列 + dataset_version + provenance
    """
    entry = license_entry(SOURCE) or {}
    reason, gate_detail = not_ingested_reason(SOURCE)
    if not license_confirmed(SOURCE):
        return empty_result(
            NOT_INGESTED,
            hint=(
                f"MARPOR 许可门未通过（{gate_detail}）：再分发须书面授权、"
                "三点待人工确认（docs/data-license.md §2），未摄取任何数据；"
                "过门前本查询恒为空。"
            ),
            gate_status=reason,  # license_unconfirmed（台账口径，补充字段）
            gate_detail=gate_detail,
            gate_constraints=entry.get("constraints", []),
            source=SOURCE,
        )

    settings = settings or Settings.from_env()
    conn = settings.connect()
    try:
        schema.init(conn)
        rows = conn.execute(
            "SELECT country_code, year, value, snapshot_version"
            " FROM regime_indicators"
            " WHERE indicator='rile' AND snapshot_version LIKE 'marpor@%'"
            " ORDER BY country_code, year"
        ).fetchall()
        if not rows:
            return empty_result(
                NOT_INGESTED,
                hint="MARPOR 许可门已过但尚未摄取数据（2026-10-23 开门裁决；"
                "数据集获取步骤见 docs/data-license.md §2.1）。",
                source=SOURCE,
            )
        # 国名解析：从最近 marpor 快照回读原始 CSV 建 code↔name 映射
        name_map: dict[str, str] = {}
        latest = rows[-1]["snapshot_version"]
        snap = snapshot.get(conn, latest)
        if snap:
            try:
                for rec in parse_mpds_rows(snapshot.read(snap)):
                    if rec["country"] and rec["countryname"]:
                        name_map.setdefault(rec["countryname"].casefold(), rec["country"])
            except (MpdsFormatError, OSError):
                pass  # 映射是增强，解析失败按 code 直配
        code = country.strip()
        if code.casefold() not in {r["country_code"].casefold() for r in rows}:
            code = name_map.get(country.strip().casefold(), code)
        matched = [
            r for r in rows if r["country_code"].casefold() == code.casefold()
        ]
        if years is not None:
            lo, hi = (years if isinstance(years, tuple) else (years, years))
            matched = [r for r in matched if lo <= r["year"] <= hi]
        if not matched:
            known = sorted({r["country_code"] for r in rows})[:12]
            return empty_result(
                NOT_INGESTED,
                hint=f"MARPOR 无 {country!r} 的 rile 行（库内 code 样例：{known}）；"
                "party 细粒度待 party_indicators 表（后续波次）。",
                source=SOURCE,
            )
        dataset_version = matched[0]["snapshot_version"].split("#", 1)[0].split("@", 1)[-1]
        return {
            "source": SOURCE,
            "country_code": matched[0]["country_code"],
            "indicator": "rile（party→国家-年均值；左右合成维度，正值偏右）",
            "dataset_version": dataset_version,
            "results": [
                {"year": r["year"], "value": r["value"]} for r in matched
            ],
            "provenance": provenance(
                SOURCE,
                matched[0]["snapshot_version"],
                DATA_PAGE,
                _attribution(dataset_version),
            ),
        }
    finally:
        conn.close()
