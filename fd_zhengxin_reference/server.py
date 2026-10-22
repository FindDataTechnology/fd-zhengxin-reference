"""政心参照系 FastMCP 服务器。

骨架阶段：四类查询（概念/思潮坐标/关系/政体时序）以占位工具回报 not_ingested
（带原因，含许可门状态），绝不编造数据。HTTP 面挂 /mcp（MCP
streamable-http），/healthz 供探活（cli.serve 拼装）。工具面按模块分：
tools_placeholder（四类查询占位）+ tools_registry（摄取台账）。
"""

from __future__ import annotations

from fastmcp import FastMCP

from . import tools_placeholder, tools_registry
from .config import Settings

SERVER_NAME = "fd-zhengxin-reference"

INSTRUCTIONS = (
    "政心参照系：政治概念（SEP 引用层 + 中国线）、思潮坐标（MARPOR + 中国线）、"
    "思想关系边（Wikidata + 中国线）、政体指标时序（V-Dem + MARPOR）。"
    "所有结果携带 provenance（source/stable_id/as_of/attribution/snapshot_version）；"
    "空结果必带 reason（blocked/upstream/unreachable/not_ingested），"
    "源未过许可门则 not_ingested 且注明原因，绝不编造。"
)


def create_server(settings: Settings | None = None) -> FastMCP:
    settings = settings or Settings.from_env()
    mcp: FastMCP = FastMCP(SERVER_NAME, instructions=INSTRUCTIONS)
    tools_placeholder.register(mcp, settings)
    tools_registry.register(mcp, settings)
    return mcp
