"""四类查询占位工具（骨架期）。

契约：占位工具一律返回 {"results": [], "reason": "not_ingested", "hint": ...}——
hint 指明喂数据的源与该源当前的许可门状态；数据未落库前绝不返回编造结果。

四类查询与喂源的对应（接线波次按此拆分为 per-source 模块）：
- concept_lookup     概念：SEP（已过许可门，只链不抄）+ 中国线（评审中）
- ideology_query     思潮坐标：MARPOR（评审中）+ 中国线（评审中）
- relation_query     关系边：Wikidata（CC0）+ 中国线（评审中）
- regime_timeseries  政体时序：V-Dem（评审中）+ MARPOR（评审中）
"""

from __future__ import annotations

from fastmcp import FastMCP

from .config import Settings
from .errors import NOT_INGESTED, empty_result
from .license import license_entry

CONCEPT_SOURCES = ("sep", "china-line")
IDEOLOGY_SOURCES = ("marpor", "china-line")
RELATION_SOURCES = ("wikidata", "china-line")
REGIME_SOURCES = ("v-dem", "marpor")


def _gate_note(sources: tuple[str, ...]) -> str:
    """拼各喂源的许可门状态（hint 用，与 LICENSE_REGISTRY 同源不漂移）。"""
    parts = []
    for source in sources:
        entry = license_entry(source) or {}
        parts.append(f"{source} 许可门={entry.get('status', '未登记')}")
    return "；".join(parts)


def placeholder(sources: tuple[str, ...], what: str) -> dict:
    """占位返回：空结果 + not_ingested + 喂源许可门状态（空结果必带原因）。"""
    return empty_result(
        NOT_INGESTED,
        hint=(
            f"{what}尚未摄取（骨架占位）。喂源状态——{_gate_note(sources)}；"
            "过许可门并完成摄取后，本工具返回带 provenance 的真实结果。"
        ),
    )


def register(mcp: FastMCP, settings: Settings) -> None:
    """挂工具：闭包绑定 settings（每个 server 实例各自的 DB）。"""

    async def concept_lookup(term: str) -> dict:
        """概念查询：政治概念的定义摘要与引用链接（SEP 引用层 + 中国线词条，
        只链不抄，正文不落库）。骨架占位：数据未摄取，返回空结果并带原因
        （not_ingested，hint 含各喂源许可门状态），绝不编造。"""

        return placeholder(CONCEPT_SOURCES, "概念层")

    async def ideology_query(name: str) -> dict:
        """思潮坐标查询：思潮/思想家/政党家族在政治坐标轴上的位置
        （MARPOR 立场编码 + 中国线）。骨架占位：数据未摄取，返回空结果并带
        原因（not_ingested，hint 含各喂源许可门状态），绝不编造。"""

        return placeholder(IDEOLOGY_SOURCES, "思潮坐标层")

    async def relation_query(source: str, target: str) -> dict:
        """关系查询：两个思潮/思想家之间的影响/批判/论战边（Wikidata 关系
        对齐 + 中国线）。骨架占位：数据未摄取，返回空结果并带原因
        （not_ingested，hint 含各喂源许可门状态），绝不编造。"""

        return placeholder(RELATION_SOURCES, "关系层")

    async def regime_timeseries(country: str, indicator: str) -> dict:
        """政体时序查询：国家×年份的政体指标序列（V-Dem 政体指数 +
        MARPOR 立场指标）。骨架占位：数据未摄取，返回空结果并带原因
        （not_ingested，hint 含各喂源许可门状态），绝不编造。"""

        return placeholder(REGIME_SOURCES, "政体时序层")

    mcp.tool(concept_lookup)
    mcp.tool(ideology_query)
    mcp.tool(relation_query)
    mcp.tool(regime_timeseries)
