"""V-Dem 摄取器（许可门：confirmed，CC BY-SA 4.0——docs/data-license.md §1）。

官方 CSV（Country-Date / Country-Year: V-Dem Core 变体的 V-Dem 聚合指标表）
→ 熔成长表按 (country_text_id, year) 入 regime_indicators（国家-年政体指标时序）。

- 快照版本化：CSV 原件内容寻址入库（snapshot.store），行级 snapshot_version 指向
  快照 id（<dataset>@<version>#<sha256 前 12>）——同一快照重复解析结果一致。
- 归属：CC BY-SA 4.0 + v-dem.net + 官网建议引用（DOI），随快照 attribution 留存；
  ShareAlike 只在导出/下载面触发（许可约束，见 LICENSE_REGISTRY["v-dem"]）。
- 摄取口径：指标列只取 v2x_ 前缀（V-Dem 自产聚合指数族）；country_*/year/
  version 等元数据列不入指标表——Full+Others 变体的第三方列被该规则天然排除，
  但仍建议直接用 Core / Country-Date 纯变体（许可约束第 3 条）。
- 空值/不可解析值跳过并计数（skipped_values），不落 NULL 行、不编造。
- 数据集版本号优先级：参数 version > CSV version 列 > 文件名（v16 等）> unknown。

真数据获取——下载配方（2026-10-23 实测打通）：裸域 v-dem.net 直连可达（www 子域不通勿用）；GET /data/the-v-dem-dataset/country-year-v-dem-core-v16/ 带 cookie jar 取 CSRF，POST 回同页（注意不是 /register-newsletter/ 表单），字段：email/gender=man/dataset_file=221(CSV)/accept_terms=on；响应页给出媒体链接 www.v-dem.net/media/datasets/V-Dem-CY-Core-v16_csv.zip（16.4MB，v16 实测 4,017,210 行/202 国）。长期定时拉取走爬虫平台 mihomo 代理方案（用户已定）。"""

from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from . import schema, snapshot
from .config import Settings
from .errors import NOT_INGESTED, UPSTREAM, empty_result
from .license import require_license
from .provenance import ATTRIBUTIONS, CITATION_URLS, provenance

SOURCE = "v-dem"      # 与 LICENSE_REGISTRY / tools_registry.SOURCES 同名
DATASET = "v-dem"     # 快照 dataset 名（台账 _snapshot_of 按源名查快照，须一致）
DATA_PAGE = f"{CITATION_URLS['v-dem']}/data/the-v-dem-dataset"
DOI = "https://doi.org/10.23696/vdemds26"  # v16 官网建议引用 DOI（引用型 URL）

# 官方 CSV 必备列（Core / Country-Date 变体共有；缺失即结构异常）
REQUIRED_COLUMNS = ("country_text_id", "country_name", "year")
# 已知元数据列（不入指标表；其余非 v2x_ 列一律视为元数据忽略）
METADATA_COLUMNS = frozenset(
    {"country_id", "historical_date", "version", "project"}
)

# 指标口径说明（五大高层民主指数；其余 v2x_* 用通用口径，详见随快照 Codebook）
INDICATOR_NOTES: dict[str, str] = {
    "v2x_polyarchy": (
        "选举民主指数（electoral democracy index）：公职经由自由公平的周期性"
        "竞争选举产生、成年人普遍平等参与的自由是否落实；0-1 连续"
    ),
    "v2x_libdem": (
        "自由民主指数（liberal democracy index）：选举民主之上加权法治、"
        "行政/立法/司法约束与公民自由平等保护；0-1 连续"
    ),
    "v2x_partipdem": (
        "参与民主指数（participatory democracy index）：公民社会参与、"
        "直接民主与地方/区域政府参与的加权测量；0-1 连续"
    ),
    "v2x_delibdem": (
        "协商民主指数（deliberative democracy index）：政策制定是否基于"
        "理性协商与公共理由（胁迫/贿选成分为负向）；0-1 连续"
    ),
    "v2x_egaldem": (
        "平等民主指数（egalitarian democracy index）：社会群体政治权力平等"
        "与基础资源分配平等的加权；0-1 连续"
    ),
}
GENERIC_NOTE = "V-Dem 聚合指数（v2x_* 前缀，0-1 连续量表）；逐指标口径以随快照保留的 Codebook 为准"


class VdemFormatError(ValueError):
    """CSV 结构不符合官方 schema（缺必备列 / 无数据行）。"""


@dataclass
class ParsedVdem:
    """解析产物：长表行 + 统计（纯函数输出，同一快照重复解析结果一致）。"""

    rows: list[tuple[str, int, str, float]]  # (country_code, year, indicator, value)
    country_names: dict[str, str]            # country_text_id -> country_name
    indicators: list[str]                    # 命中的 v2x_* 指标列
    skipped_values: int                      # 空值/坏值跳过计数
    years: tuple[int, int] | None            # 年份覆盖范围
    version: str


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _attribution(version: str) -> str:
    """归属声明：CC BY-SA 4.0 + 官网建议引用格式（含 DOI）。"""
    return (
        f"{ATTRIBUTIONS[SOURCE]}；建议引用：Coppedge, Michael / Edelson, John / "
        f"Golder, Sona N. / … / Wang, Yi-ting. \"V-Dem [Country-Year/Country-Date] "
        f"Dataset {version}\" Varieties of Democracy (V-Dem) Project. {DOI}"
        "（完整作者名单与年份以快照 ZIP 内 Suggested Citations 为准）"
    )


def _detect_version_from_filename(name: str) -> str | None:
    m = re.search(r"v(\d+(?:\.\d+)?[a-z]?)", name, re.IGNORECASE)
    return f"v{m.group(1)}" if m else None


def parse_vdem_csv(
    content: bytes, *, version: str | None = None, filename: str = ""
) -> ParsedVdem:
    """解析官方 CSV（纯函数）。结构异常抛 VdemFormatError，不静默降级。"""
    reader = csv.DictReader(io.StringIO(content.decode("utf-8-sig")))
    header = list(reader.fieldnames or [])
    missing = [c for c in REQUIRED_COLUMNS if c not in header]
    if missing:
        raise VdemFormatError(f"缺官方必备列 {missing}（header={header[:8]}…）")
    indicators = [c for c in header if c.startswith("v2x_")]
    if not indicators:
        raise VdemFormatError("无 v2x_* 指标列（非 V-Dem 聚合指标表？）")

    rows: list[tuple[str, int, str, float]] = []
    names: dict[str, str] = {}
    skipped = 0
    years: set[int] = set()
    csv_version: str | None = None
    for rec in reader:
        code = (rec.get("country_text_id") or "").strip()
        name = (rec.get("country_name") or "").strip()
        if not code or not name:  # 国家标识缺失的行整体跳过
            skipped += len(indicators)
            continue
        names[code] = name
        raw_year = (rec.get("year") or "").strip()
        try:
            year = int(float(raw_year))
        except ValueError:
            skipped += len(indicators)
            continue
        years.add(year)
        if csv_version is None:
            v = (rec.get("version") or "").strip()
            csv_version = v or None
        for ind in indicators:
            raw = rec.get(ind)
            if raw is None or not str(raw).strip():
                skipped += 1  # 官方 CSV 以空串表缺失
                continue
            try:
                rows.append((code, year, ind, float(raw)))
            except ValueError:
                skipped += 1
    if not rows:
        raise VdemFormatError(f"CSV 无可入行（skipped={skipped}）")
    detected = (
        version
        or csv_version
        or _detect_version_from_filename(filename)
        or "unknown"
    )
    return ParsedVdem(
        rows=rows,
        country_names=names,
        indicators=indicators,
        skipped_values=skipped,
        years=(min(years), max(years)),
        version=detected,
    )


def _log(conn, status: str, detail: dict) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO ingest_log (source, status, detail, as_of)"
        " VALUES (?,?,?,?)",
        (SOURCE, status, json.dumps(detail, ensure_ascii=False), _now()),
    )
    conn.commit()


def ingest_vdem(
    path: str | Path,
    *,
    settings: Settings | None = None,
    version: str | None = None,
    source_url: str | None = None,
) -> dict:
    """摄取官方 V-Dem CSV：许可门 → 快照 → 长表入库 → 台账。

    幂等：同内容文件重复摄取 → 复用同一快照（内容寻址），行 INSERT OR REPLACE，
    结果一致。结构异常不静默：返回 status=failed（reason=upstream）并写台账。
    """
    require_license(SOURCE)  # 许可门第一（confirmed；若评审状态翻转在此拦下）
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"V-Dem CSV 不存在：{path}")
    settings = settings or Settings.from_env()
    try:
        return _ingest_vdem_streaming(
            path, settings=settings, version=version, source_url=source_url
        )
    except VdemFormatError as exc:
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


def _ingest_vdem_streaming(
    path: Path,
    *,
    settings: Settings,
    version: str | None,
    source_url: str | None,
) -> dict:
    """流式摄取主体（2026-10-23 生产 OOM ×2 后改流式，勿再物化整表）。"""
    # 轻窥：header 校验 + 首行 version（避免整读 212MB 才发现结构错）
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        header = list(reader.fieldnames or [])
        missing = [c for c in REQUIRED_COLUMNS if c not in header]
        if missing:
            raise VdemFormatError(
                f"缺官方必备列 {missing}（header={header[:8]}…）"
            )
        indicators_scan = [c for c in header if c.startswith("v2x_")]
        if not indicators_scan:
            raise VdemFormatError("无 v2x_* 指标列（非 V-Dem 聚合指标表？）")
        first = next(iter(reader), None)
        csv_version = (
            (first.get("version") or "").strip() if first else ""
        ) or None
    detected = (
        version
        or csv_version
        or _detect_version_from_filename(path.name)
        or "unknown"
    )
    manifest = snapshot.store_file(
        DATASET,
        detected,
        path,
        source_url=source_url or DATA_PAGE,
        attribution=_attribution(detected),
        probe_status="available",
        filename=path.name,
    )
    conn = settings.connect()
    try:
        schema.init(conn)
        snapshot.record(conn, manifest)
        ts = _now()
        # 流式入库：不物化 parsed.rows（4M 行字典会把 512Mi~2Gi 的容器顶穿，
        # 2026-10-23 生产实测 OOM ×2 后改流式）
        names: dict[str, str] = {}
        skipped = 0
        years: set[int] = set()
        row_count = 0
        batch: list[tuple[str, int, str, float]] = []
        with path.open("r", encoding="utf-8-sig", newline="") as fh:
            reader = csv.DictReader(fh)
            indicators = [c for c in (reader.fieldnames or []) if c.startswith("v2x_")]
            for rec in reader:
                code = (rec.get("country_text_id") or "").strip()
                name = (rec.get("country_name") or "").strip()
                if not code or not name:
                    skipped += len(indicators)
                    continue
                names[code] = name
                raw_year = (rec.get("year") or "").strip()
                try:
                    year = int(float(raw_year))
                except ValueError:
                    skipped += len(indicators)
                    continue
                years.add(year)
                for ind in indicators:
                    raw = rec.get(ind)
                    if raw is None or not str(raw).strip():
                        skipped += 1
                        continue
                    try:
                        batch.append((code, year, ind, float(raw)))
                    except ValueError:
                        skipped += 1
                if len(batch) >= 100_000:
                    conn.executemany(
                        "INSERT OR REPLACE INTO regime_indicators"
                        " (country_code, year, indicator, value, ingested_at,"
                        " snapshot_version) VALUES (?,?,?,?,?,?)",
                        [(c_, y, i, v, ts, manifest.snapshot_id)
                         for c_, y, i, v in batch],
                    )
                    row_count += len(batch)
                    batch.clear()
        if batch:
            conn.executemany(
                "INSERT OR REPLACE INTO regime_indicators"
                " (country_code, year, indicator, value, ingested_at,"
                " snapshot_version) VALUES (?,?,?,?,?,?)",
                [(c_, y, i, v, ts, manifest.snapshot_id)
                 for c_, y, i, v in batch],
            )
            row_count += len(batch)
        if not row_count:
            raise VdemFormatError(f"CSV 无可入行（skipped={skipped}）")
        _log(
            conn,
            "ok",
            {
                "rows": row_count,
                "countries": len(names),
                "indicators": len(indicators),
                "skipped_values": skipped,
                "version": detected,
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
        "rows": row_count,
        "countries": len(names),
        "indicators": indicators,
        "skipped_values": skipped,
        "version": detected,
        "years": {"from": min(years), "to": max(years)},
        "snapshot_id": manifest.snapshot_id,
        "attribution": manifest.attribution,
        "provenance": provenance(
            SOURCE,
            stable_id=f"v-dem:snapshot:{manifest.snapshot_id}",
            snapshot_version=manifest.snapshot_id,
        ),
    }


def _year_set(years: tuple[int, int] | Iterable[int] | int | None) -> set[int] | None:
    """年份参数归一：None=全部；二元组 (起, 止)=闭区间；其余=显式年份集合。"""
    if years is None:
        return None
    if isinstance(years, int):
        return {years}
    seq = list(years)
    if (
        len(seq) == 2
        and isinstance(seq[0], int)
        and isinstance(seq[1], int)
        and seq[0] <= seq[1]
    ):
        return set(range(seq[0], seq[1] + 1))
    return {int(y) for y in seq}


def _load_country_names(snap: dict | None) -> dict[str, str]:
    """从快照 CSV 读 country_text_id -> country_name 名录（供按国名解析）。

    流式读（2026-10-23：快照已是 212MB 级，read_bytes 整读曾把查询进程 OOM）——
    名录是增强层，快照文件缺失/损坏时返回空表不炸查询。
    """
    if not snap:
        return {}
    path = Path(snap.get("storage_path") or "")
    if not path.is_file():
        return {}
    names: dict[str, str] = {}
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as fh:
            reader = csv.DictReader(fh)
            for rec in reader:
                code = (rec.get("country_text_id") or "").strip()
                name = (rec.get("country_name") or "").strip()
                if code and name and code not in names:
                    names[code] = name
    except (OSError, UnicodeDecodeError):
        return {}
    return names


def _load_one_country_name(snap: dict | None, code: str) -> dict[str, str]:
    """单国显示名（查询面用）：流式扫到即停，不整读快照、不建全表。"""
    path = Path(snap.get("storage_path") or "") if snap else Path()
    if not path.is_file():
        return {}
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as fh:
            for rec in csv.DictReader(fh):
                if (rec.get("country_text_id") or "").strip() == code:
                    name = (rec.get("country_name") or "").strip()
                    if name:
                        return {code: name}
                    return {}
    except (OSError, UnicodeDecodeError):
        pass
    return {}


def _resolve_country(conn, country: str, snap: dict | None) -> str | None:
    """国家解析：先按 country_text_id（大小写不敏感），再按快照国名全称。"""
    row = conn.execute(
        "SELECT country_code FROM regime_indicators WHERE country_code=?"
        " COLLATE NOCASE LIMIT 1",
        (country.strip(),),
    ).fetchone()
    if row:
        return row["country_code"]
    target = country.strip().casefold()
    for code, name in _load_country_names(snap).items():
        if name.casefold() == target or code.casefold() == target:
            return code
    return None


def query_regime_timeseries(
    country: str,
    years: tuple[int, int] | Iterable[int] | int | None = None,
    *,
    indicator: str | None = None,
    settings: Settings | None = None,
) -> dict:
    """政体时序查询：国家 × 年份的 V-Dem 指标序列（带指标口径说明与 provenance）。

    - country：country_text_id（如 USA）或官方英文国名（经快照名录解析）。
    - years：None=全部；(起, 止) 二元组=闭区间；其余可迭代=显式年份集合。
    - 空结果必带 reason（not_ingested），hint 说明是未摄取还是无该国行。
    """
    settings = settings or Settings.from_env()
    conn = settings.connect()
    try:
        schema.init(conn)
        snap = snapshot.latest(conn, DATASET)
        code = _resolve_country(conn, country, snap)
        rows = (
            conn.execute(
                "SELECT year, indicator, value, snapshot_version FROM"
                " regime_indicators WHERE country_code=? ORDER BY year, indicator",
                (code,),
            ).fetchall()
            if code
            else []
        )
    finally:
        conn.close()

    want_years = _year_set(years)
    rows = [
        r for r in rows if want_years is None or r["year"] in want_years
    ]
    if not rows:
        hint = (
            "V-Dem 已过许可门（CC BY-SA 4.0）但库中无该国家/年份的行——"
            f"country={country!r} 未命中（country_text_id 或官方国名均未匹配），"
            "或所选年份不在已摄取范围内。"
            if snap
            else "V-Dem 已过许可门（CC BY-SA 4.0）但数据尚未摄取"
            f"（库中无 v-dem 快照）——先 ingest_vdem 落库。"
        )
        return empty_result(NOT_INGESTED, hint=hint, source=SOURCE)

    by_year: dict[int, dict[str, float]] = {}
    snap_ids: set[str] = set()
    for r in rows:
        by_year.setdefault(r["year"], {})[r["indicator"]] = r["value"]
        if r["snapshot_version"]:
            snap_ids.add(r["snapshot_version"])
    present = sorted({ind for m in by_year.values() for ind in m})
    if indicator is not None:
        by_year = {
            year: {ind: val for ind, val in m.items() if ind == indicator}
            for year, m in by_year.items()
        }
        by_year = {year: m for year, m in by_year.items() if m}
        if not by_year:  # 过滤后无行：按空结果纪律回报
            return empty_result(
                NOT_INGESTED,
                hint=(
                    f"指标 {indicator!r} 不在已摄取范围内（country={country!r} 命中，"
                    f"但无该指标的行）。"
                ),
                source=SOURCE,
            )
    present = sorted({ind for m in by_year.values() for ind in m})
    notes = {ind: INDICATOR_NOTES.get(ind, GENERIC_NOTE) for ind in present}
    names = _load_one_country_name(snap, code) if snap else {}
    results = [
        {
            "country_code": code,
            "country_name": names.get(code),
            "year": year,
            "indicators": dict(sorted(by_year[year].items())),
        }
        for year in sorted(by_year)
    ]
    return {
        "results": results,
        "indicator_notes": notes,  # 指标口径说明（口径随 Codebook 版本，见快照）
        "attribution": snap["attribution"] if snap else ATTRIBUTIONS[SOURCE],
        "provenance": provenance(
            SOURCE,
            stable_id=f"v-dem:regime:{code}",
            snapshot_version=sorted(snap_ids)[-1] if snap_ids else None,
        ),
    }
