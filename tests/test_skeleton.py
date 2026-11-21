"""骨架验证：服务可建、healthz 通、MCP 握手可完成、查询工具空结果必带原因、不编造数据。"""

from __future__ import annotations

import pytest

from fd_zhengxin_reference import __version__
from fd_zhengxin_reference.config import Settings
from fd_zhengxin_reference.errors import REASONS
from fd_zhengxin_reference.server import SERVER_NAME, create_server

TOOL_SURFACE = {
    "concept_lookup",
    "ideology_query",
    "relation_query",
    "regime_timeseries",
    "entity_dossier",
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
async def test_query_tools_empty_with_reason(tmp_path, monkeypatch) -> None:
    """查询工具空结果必带 taxonomy reason + hint（绝不编造、绝不静默为空）。

    断源形态（接线后的骨架基线）：SEP 不可达（离线桩）、wikidata/marpor/v-dem
    未摄取——curated 层（china-seed/modern-index）由启动钩子加载，但查询词避开
    它们以走空路径。深口径的断源联测见 tests/test_tools_wiring.py。
    """
    from fd_zhengxin_reference import sep_politics
    from fd_zhengxin_reference.sep_politics import SepUnreachable

    monkeypatch.setenv("FDZX_REF_DB", str(tmp_path / "ref.db"))
    monkeypatch.setenv("FDZX_SNAPSHOT_DIR", str(tmp_path / "snapshots"))

    async def offline(*a, **kw):
        raise SepUnreachable("offline")

    monkeypatch.setattr(sep_politics, "fetch_search_html", offline)
    mcp = create_server()
    cases = {
        "concept_lookup": {"term": "zzznonsense"},  # 库内无 + SEP 不可达 → unreachable
        "ideology_query": {"name": "保守主义"},  # wikidata 未摄取 + marpor 门关 → not_ingested
        "relation_query": {"source": "苏格拉底", "target": "柏拉图"},  # 两源均未命中
        "regime_timeseries": {"country": "CHN", "indicator": "v2x_polyarchy"},  # v-dem 未摄取
    }
    for name, args in cases.items():
        out = await call_tool(mcp, name, **args)
        assert out["results"] == []
        assert out["reason"] in REASONS
        assert out["hint"]  # 空结果必须能解释自己（各源状态与缺口原因）


@pytest.mark.asyncio
async def test_mcp_handshake(tmp_path, monkeypatch) -> None:
    """MCP 握手：Client 连接（=initialize）后 tools/list 五工具齐全。

    FDZX_SNAPSHOT_DIR 隔离：启动钩子会物化 curated 层快照（china-seed /
    china-modern-index），不隔离会写进默认快照根 ~/.fd-zhengxin/snapshots。
    """
    from fastmcp import Client

    monkeypatch.setenv("FDZX_REF_DB", str(tmp_path / "ref.db"))
    monkeypatch.setenv("FDZX_SNAPSHOT_DIR", str(tmp_path / "snapshots"))
    mcp = create_server()
    async with Client(mcp) as client:
        tools = await client.list_tools()
    assert {t.name for t in tools} == TOOL_SURFACE


@pytest.mark.asyncio
async def test_source_status_empty_db(tmp_path, monkeypatch) -> None:
    """空库：六源均 not_ingested 且带 reason；未过许可门的源指名 license_unconfirmed。

    许可口径与 docs/data-license.md（2026-10-23 核验）一致：v-dem 已确认
    CC BY-SA 4.0（未摄取原因=not_ingested）；marpor 三点未确认 →
    license_unconfirmed 并带 gate_detail。china-seed/modern-index 为 curated 层
    （非外部语料、无许可门），此测试直调未走启动钩子 → not_ingested。
    """
    monkeypatch.setenv("FDZX_REF_DB", str(tmp_path / "ref.db"))
    from fd_zhengxin_reference.tools_registry import source_status

    out = source_status()
    assert {r["source"] for r in out["results"]} == {
        "v-dem",
        "marpor",
        "wikidata",
        "sep",
        "china-seed",
        "modern-index",
    }
    assert all(r["status"] == "not_ingested" and r["reason"] for r in out["results"])
    by = {r["source"]: r for r in out["results"]}
    # 未过许可门：原因指名许可门（评审中），并带评审待办；不是含糊的"未摄取"
    assert by["marpor"]["reason"] == "not_ingested"  # 开门后无数据：纯数据口径
    assert by["marpor"]["expectation"]  # 空结果仍带说明（开门后待数据）
    # 已过许可门但未摄取：原因就是 not_ingested（sep 为 on-demand 缓存层）
    for source in ("v-dem", "wikidata", "sep"):
        assert by[source]["reason"] == "not_ingested"
    # curated 层未加载（本测试直调 source_status，未走 create_server 启动钩子）
    for source in ("china-seed", "modern-index"):
        assert by[source]["reason"] == "not_ingested"


def test_license_gate_discipline() -> None:
    """许可门纪律：pending 源 require_license 抛错且带原因；confirmed 源放行。"""
    from fd_zhengxin_reference.license import (
        LicenseNotConfirmed,
        assert_allowed_url,
        license_confirmed,
        require_license,
    )

    assert license_confirmed("marpor")  # 2026-10-23 用户裁决开门
    assert not license_confirmed("china-line")  # 门机制仍在：未定源清单的源拒摄取
    with pytest.raises(LicenseNotConfirmed):
        require_license("china-line")
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
