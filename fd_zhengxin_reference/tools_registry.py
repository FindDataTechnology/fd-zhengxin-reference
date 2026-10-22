"""摄取台账工具：源状态与缺口，未摄取必带原因。

六源口径（四外部源 + 两 curated 层）：
- 外部源：许可门是第一道门——未过门即不摄取，缺口原因指名许可评审
  （marpor：license_unconfirmed）；已过门未摄取 → not_ingested。
- curated 层（china-seed / modern-index）：编辑产出的结构化数据，非外部语料
  摄取，不走许可门；create_server 启动钩子幂等加载，状态纯读台账。
- 台账附最近一次可得性探测与在用快照版本——否则「源挂了」与「解析器坏了」
  在台账上不可分。快照 dataset 名与源名不一致处（wikidata→wikidata-politics、
  modern-index→china-modern-index）在 SOURCES 显式映射，防漂移。
"""

from __future__ import annotations

from fastmcp import FastMCP

from . import china_lineage
from .config import Settings
from .license import license_confirmed, not_ingested_reason
from .schema import init

# 六源基线：未摄取时按此给出缺口原因（spec：缺口可见并注明原因）。
# log_source=台账 ingest_log 的 source 名；dataset=快照 dataset 名；curated=内部层无许可门。
SOURCES = {
    "v-dem": {
        "log_source": "v-dem",
        "dataset": "v-dem",
        "expectation": "政体指标时序（国家×年，v2x_* 等指数），许可已核验（CC BY-SA 4.0），官方 CSV 人工下载后 ingest_vdem 落库",
    },
    "marpor": {
        "log_source": "marpor",
        "dataset": "marpor",
        "expectation": "政党宣言立场编码（rile 等），许可三点未确认（docs/data-license.md §2），确认前不摄取",
    },
    "wikidata": {
        "log_source": "wikidata",
        "dataset": "wikidata-politics",
        "expectation": "政治实体/思潮 QID 对齐与关系边（P1142/P1387 坐标锚、极性边），ingest_wikidata_politics 摄取（探针样本见 docs/wikidata-probe-samples/）",
    },
    "sep": {
        "log_source": "sep",
        "dataset": None,  # on-demand 缓存层，无批量快照
        "expectation": "概念引用层（on-demand 缓存，覆盖面见 docs/sep-coverage.md），concept_lookup 调用时自动建立",
    },
    "china-seed": {
        "log_source": "china-seed",
        "dataset": "china-seed",
        "curated": True,
        "expectation": "先秦诸子 curated 种子谱系（人工提炼结构化数据，非外部语料摄取，不走 china-line 语料许可门）；create_server 启动钩子幂等加载",
    },
    "modern-index": {
        "log_source": "china-modern-index",
        "dataset": "china-modern-index",
        "curated": True,
        "expectation": "近现代五思潮文献索引（docs/china-modern-sources.md 固化，只存条目元数据与链接）；create_server 启动钩子幂等加载",
    },
}


def _probe_of(conn, source: str) -> dict | None:
    row = conn.execute(
        "SELECT source, checked_at, status, reason, http_status, elapsed_ms,"
        " content_sha256, detail FROM source_status WHERE source=?"
        " ORDER BY checked_at DESC LIMIT 1",
        (source,),
    ).fetchone()
    return {k: row[k] for k in row.keys()} if row else None


def _snapshot_of(conn, dataset: str) -> dict | None:
    row = conn.execute(
        "SELECT snapshot_id, dataset, version, fetched_at, sha256, attribution,"
        " probe_status FROM snapshots WHERE dataset=?"
        " ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (dataset,),
    ).fetchone()
    return {k: row[k] for k in row.keys()} if row else None


def _attach_snapshot(entry: dict, snap: dict | None) -> None:
    """在用快照版本（dataset 名经 SOURCES 映射后查询，与源名解耦）。"""
    if snap:
        entry["snapshot"] = {
            "snapshot_id": snap["snapshot_id"],
            "version": snap["version"],
            "fetched_at": snap["fetched_at"],
            "sha256": snap["sha256"],
            "probe_status": snap["probe_status"],
        }


def source_status(settings: Settings | None = None) -> dict:
    """源状态：六源的摄取状态与缺口（未摄取必带原因，许可门是第一道门）。"""
    settings = settings or Settings.from_env()
    conn = settings.connect()
    try:
        init(conn)
        rows = conn.execute(
            "SELECT source, status, detail, as_of FROM ingest_log "
            "WHERE as_of = (SELECT MAX(as_of) FROM ingest_log l2 WHERE l2.source = ingest_log.source)"
        ).fetchall()
        # SEP 缓存行数只数非种子行（concepts 表混存 china-seed 的 50 条种子概念）
        sep_cached = conn.execute(
            "SELECT COUNT(*) AS c FROM concepts WHERE url NOT LIKE ?",
            ("%" + china_lineage.SEED_CONCEPT_MARKER,),
        ).fetchone()["c"]
        probes = {s: _probe_of(conn, s) for s in SOURCES}
        snapshots = {
            s: _snapshot_of(conn, spec["dataset"])
            for s, spec in SOURCES.items()
            if spec["dataset"]
        }
    finally:
        conn.close()

    by_source = {r["source"]: r for r in rows}
    results = []
    for source, spec in SOURCES.items():
        entry: dict = {
            "source": source,
            "probe": probes.get(source),
            "expectation": spec["expectation"],
        }

        # curated 层：内部编辑产出，无许可门——状态纯读台账（启动钩子应已写入 ok）
        if spec.get("curated"):
            row = by_source.get(spec["log_source"])
            if row is None:
                entry["status"] = "not_ingested"
                entry["reason"] = "not_ingested"
                entry["detail"] = (
                    "启动加载未执行或被跳过（server.bootstrap_curated；"
                    "seed 文件缺失时跳过，缺口在此可见）"
                )
            else:
                entry["status"] = row["status"]
                entry["as_of"] = row["as_of"]
                if row["status"] != "ok":
                    entry["reason"] = _reason_from(row["detail"]) or row["detail"] or row["status"]
            _attach_snapshot(entry, snapshots.get(source))
            results.append(entry)
            continue

        # 许可门是第一道门：未过门即不摄取，缺口原因指名许可评审
        if not license_confirmed(source):
            reason, gate_detail = not_ingested_reason(source)
            entry["status"] = "not_ingested"
            entry["reason"] = reason
            if gate_detail:
                entry["gate_detail"] = gate_detail
            results.append(entry)
            continue

        if source == "sep":
            # SEP 是按需缓存层（无批量摄取）：状态由 concepts 缓存行数决定
            entry.update(
                {
                    "status": "ok" if sep_cached else "not_ingested",
                    "detail": f"on-demand 缓存 {sep_cached} 条" if sep_cached else None,
                    "reason": None if sep_cached else "not_ingested",
                }
            )
            results.append(entry)
            continue

        row = by_source.get(spec["log_source"])
        if row is None:
            entry["status"] = "not_ingested"
            entry["reason"] = "not_ingested"
            results.append(entry)
            continue

        entry["status"] = row["status"]
        entry["as_of"] = row["as_of"]
        if row["status"] != "ok":
            # 缺口原因优先取台账 detail 里的 reason，否则用原 detail
            entry["reason"] = _reason_from(row["detail"]) or row["detail"] or row["status"]
        _attach_snapshot(entry, snapshots.get(source))
        results.append(entry)

    return {
        "results": results,
        "provenance": {
            "source": "fd-zhengxin-reference",
            "stable_id": "ingest-log",
            "as_of": max((r["as_of"] for r in rows), default=None),
            "snapshot_version": None,
        },
    }


def _reason_from(detail: str | None) -> str | None:
    """从台账 detail（JSON 字符串）里取 reason 字段。"""
    if not detail:
        return None
    import json

    try:
        parsed = json.loads(detail)
    except (TypeError, ValueError):
        return None
    if isinstance(parsed, dict):
        reason = parsed.get("reason")
        return str(reason) if reason else None
    return None


def register(mcp: FastMCP, settings: Settings) -> None:
    """挂工具：闭包绑定 settings（每个 server 实例各自的 DB）。"""

    async def source_status_tool() -> dict:
        """源状态：六源（v-dem/marpor/wikidata/sep/china-seed/modern-index）的
        摄取状态与缺口。未摄取必带 reason（not_ingested / license_unconfirmed），
        未过许可门的源另带 gate_detail（评审待办），curated 层状态读台账（启动
        钩子加载），并附最近一次可得性探测与在用快照版本——让「源挂了」与
        「解析器坏了」可区分。"""

        return source_status(settings)

    source_status_tool.__name__ = "source_status"
    mcp.tool(source_status_tool)
