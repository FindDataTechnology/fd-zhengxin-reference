"""失败口径 taxonomy 与骨架期诚实回报，绝不编造数据。

四类失败口径（占位工具与接线后的查询工具共用）：
- blocked       出站被许可红线/条款拦截（不可绕过）
- upstream      源可达但响应异常（解析失败/结构变化）
- unreachable   源不可达（DNS/连接失败/超时）
- not_ingested  源未过许可门或数据尚未摄取（骨架期占位工具的统一口径）

铁律：空结果必带 reason（取值限于上述 taxonomy），绝不静默为空。
台账（source_status）另有一层缺口口径（license_unconfirmed 等），属台账字段，
不与查询失败口径混用。
"""

from __future__ import annotations

BLOCKED = "blocked"
UPSTREAM = "upstream"
UNREACHABLE = "unreachable"
NOT_INGESTED = "not_ingested"

# 合法 reason 全集：empty_result 之外的口径一律视为 bug（直接抛错，不降级）
REASONS = frozenset({BLOCKED, UPSTREAM, UNREACHABLE, NOT_INGESTED})


def empty_result(reason: str, hint: str | None = None, **extra) -> dict:
    """构造带原因的空结果。reason 不在 taxonomy 内即抛 ValueError（防口径漂移）。"""
    if reason not in REASONS:
        raise ValueError(f"未知失败口径 {reason!r}（合法值：{sorted(REASONS)}）")
    out: dict = {"results": [], "reason": reason}
    if hint:
        out["hint"] = hint
    out.update(extra)
    return out


class HonestError(RuntimeError):
    """工具未接线时的诚实回报。message 必须含 tracking task 编号。"""

    def __init__(self, tool: str, tracking_task: str):
        super().__init__(
            f"{tool} 未接线：数据摄取尚未完成（tracking task {tracking_task}）。"
            "不返回编造数据；接线后本工具按 spec 契约返回带 provenance 的真实结果。"
        )
        self.tool = tool
        self.tracking_task = tracking_task
