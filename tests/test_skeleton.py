"""骨架验证：服务可建、healthz 通、MCP 握手可完成、占位工具空结果必带原因、不编造数据。"""

from __future__ import annotations

import pytest

from fd_zhengxin_reference import __version__
from fd_zhengxin_reference.config import Settings
from fd_zhengxin_reference.errors import NOT_INGESTED
from fd_zhengxin_reference.server import SERVER_NAME, create_server

TOOL_SURFACE = {
    "concept_lookup",
    "ideology_query",
    "relation_query",
    "regime_timeseries",
    "source_status",
}


async def call_tool(mcp, name: str, /, **args) -> dict:
    tools = await mcp.list_tools()
    fn = next(t.fn for t in tools if t.name == name)
    return await fn(**args)


def test_version() -> None:
    assert __version__ == "0.1.0"


def test_server_name() -> None:
    assert SERVER_NAME == "fd-zhengxin-reference"


@pytest.mark.asyncio
async def test_tool_surface() -> None:
    """工具面完整性：四类查询占位 + 台账。"""
    mcp = create_server(Settings(db_path=":memory:"))
    tools = await mcp.list_tools()
    assert {t.name for t in tools} == TOOL_SURFACE


@pytest.mark.asyncio
async def test_placeholder_tools_empty_with_reason() -> None:
    """占位工具一律空结果 + not_ingested + hint（绝不编造数据）。"""
    mcp = create_server(Settings(db_path=":memory:"))
    cases = {
        "concept_lookup": {"term": "自由主义"},
        "ideology_query": {"name": "保守主义"},
        "relation_query": {"source": "孔子", "target": "荀子"},
        "regime_timeseries": {"country": "CHN", "indicator": "v2x_polyarchy"},
    }
    for name, args in cases.items():
        out = await call_tool(mcp, name, **args)
        assert out["results"] == []
        assert out["reason"] == NOT_INGESTED
        assert out["hint"]  # 空结果必须能解释自己（喂源与许可门状态）


@pytest.mark.asyncio
async def test_mcp_handshake(tmp_path, monkeypatch) -> None:
    """MCP 握手：Client 连接（=initialize）后 tools/list 五工具齐全。"""
    from fastmcp import Client

    monkeypatch.setenv("FDZX_REF_DB", str(tmp_path / "ref.db"))
    mcp = create_server()
    async with Client(mcp) as client:
        tools = await client.list_tools()
    assert {t.name for t in tools} == TOOL_SURFACE


@pytest.mark.asyncio
async def test_source_status_empty_db(tmp_path, monkeypatch) -> None:
    """空库：五源均 not_ingested 且带 reason；未过许可门的源指名 license_unconfirmed。

    许可口径与 docs/data-license.md（2026-10-23 核验）一致：v-dem 已确认
    CC BY-SA 4.0（未摄取原因=not_ingested）；marpor 三点未确认、china-line
    源清单未定 → license_unconfirmed 并带 gate_detail。
    """
    monkeypatch.setenv("FDZX_REF_DB", str(tmp_path / "ref.db"))
    from fd_zhengxin_reference.tools_registry import source_status

    out = source_status()
    assert {r["source"] for r in out["results"]} == {
        "v-dem",
        "marpor",
        "wikidata",
        "sep",
        "china-line",
    }
    assert all(r["status"] == "not_ingested" and r["reason"] for r in out["results"])
    by = {r["source"]: r for r in out["results"]}
    # 未过许可门：原因指名许可门（评审中），并带评审待办；不是含糊的"未摄取"
    for source in ("marpor", "china-line"):
        assert by[source]["reason"] == "license_unconfirmed"
        assert by[source]["gate_detail"]
    # 已过许可门但未摄取：原因就是 not_ingested（sep 为 on-demand 缓存层）
    for source in ("v-dem", "wikidata", "sep"):
        assert by[source]["reason"] == "not_ingested"


def test_license_gate_discipline() -> None:
    """许可门纪律：pending 源 require_license 抛错且带原因；confirmed 源放行。"""
    from fd_zhengxin_reference.license import (
        LicenseNotConfirmed,
        assert_allowed_url,
        license_confirmed,
        require_license,
    )

    assert not license_confirmed("marpor")
    with pytest.raises(LicenseNotConfirmed):
        require_license("marpor")
    # v-dem 许可事实已核验（CC BY-SA 4.0，非传闻中的 CC BY）：过门但未摄取
    assert license_confirmed("v-dem")
    assert require_license("v-dem")["license"] == "CC BY-SA 4.0"
    assert license_confirmed("wikidata")
    assert require_license("wikidata")["license"] == "CC0"
    # 红线守卫机制就位（骨架期无红线条目 → 放行，后续波次只补条目）
    assert assert_allowed_url("https://www.v-dem.net") == "https://www.v-dem.net"


@pytest.mark.asyncio
async def test_healthz() -> None:
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Route
    from starlette.testclient import TestClient

    mcp = create_server(Settings(db_path=":memory:"))
    app = mcp.http_app(path="/mcp")

    async def healthz(request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "service": SERVER_NAME})

    app.routes.append(Route("/healthz", healthz))
    client = TestClient(app)
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
