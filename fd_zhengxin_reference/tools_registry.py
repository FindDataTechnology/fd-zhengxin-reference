"""摄取台账工具：源状态与缺口，未摄取必带原因。

骨架期口径：源未过许可门 → not_ingested（reason=license_unconfirmed，带评审
待办）；已过门未摄取 → not_ingested。台账附最近一次可得性探测与在用快照版本
（表已就位，探测/摄取在后续波次写入）——否则「源挂了」与「解析器坏了」在
台账上不可分。
"""

from __future__ import annotations

from fastmcp import FastMCP

from .config import Settings
from .license import license_confirmed, not_ingested_reason
from .schema import init

# 摄取型源基线：未摄取时按此给出缺口原因（spec：缺口可见并注明原因）
SOURCES = {
    "v-dem": "政体指标时序（国家×年，v2x_* 等指数），许可已核验（CC BY-SA 4.0），摄取在接线波次",
    "marpor": "政党宣言立场编码（rile 等），许可三点未确认（docs/data-license.md §2），确认前不摄取",
    "wikidata": "全量层对齐（政治实体/思潮 QID 回填；探针样本见 docs/wikidata-probe-samples/）",
    "sep": "概念引用层（on-demand 缓存，覆盖面见 docs/sep-coverage.md），concept_lookup 调用时自动建立",
    "china-line": "中国线（政治思想词条/人物/关系），源清单与许可评审待定",
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


def source_status(settings: Settings | None = None) -> dict:
    """源状态：五源的摄取状态与缺口（未摄取必带原因，许可门是第一道门）。"""
    settings = settings or Settings.from_env()
    conn = settings.connect()
    try:
        init(conn)
        rows = conn.execute(
            "SELECT source, status, detail, as_of FROM ingest_log "
            "WHERE as_of = (SELECT MAX(as_of) FROM ingest_log l2 WHERE l2.source = ingest_log.source)"
        ).fetchall()
        sep_cached = conn.execute("SELECT COUNT(*) AS c FROM concepts").fetchone()["c"]
        probes = {s: _probe_of(conn, s) for s in SOURCES}
        snapshots = {s: _snapshot_of(conn, s) for s in SOURCES}
    finally:
        conn.close()

    by_source = {r["source"]: r for r in rows}
    results = []
    for source, expectation in SOURCES.items():
        entry: dict = {
            "source": source,
            "probe": probes.get(source),
            "expectation": expectation,
        }

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

        row = by_source.get(source)
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
        snapshot_entry = snapshots.get(source)
        if snapshot_entry:
            entry["snapshot"] = {
                "snapshot_id": snapshot_entry["snapshot_id"],
                "version": snapshot_entry["version"],
                "fetched_at": snapshot_entry["fetched_at"],
                "sha256": snapshot_entry["sha256"],
                "probe_status": snapshot_entry["probe_status"],
            }
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
        """源状态：五源的摄取状态与缺口。未摄取必带 reason（not_ingested /
        license_unconfirmed），未过许可门的源另带 gate_detail（评审待办），
        并附最近一次可得性探测与在用快照版本——让「源挂了」与「解析器坏了」
        可区分。"""

        return source_status(settings)

    source_status_tool.__name__ = "source_status"
    mcp.tool(source_status_tool)
