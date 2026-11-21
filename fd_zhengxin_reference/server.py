"""政心参照系 FastMCP 服务器。

四类查询（概念/思潮坐标/关系/政体时序）已接线到真源模块：tools_placeholder
（模块历史名保留）挂 concept_lookup / ideology_query / relation_query /
regime_timeseries，tools_registry 挂 source_status（六源台账）。HTTP 面挂 /mcp
（MCP streamable-http），/healthz 供探活，/api/v1/* 是个人层只读 REST 锚点面
（rest_api，D3）——cli.serve 拼装见 build_http_app。

启动初始化：create_server 以**启动钩子**形式（非首次惰性）幂等加载 curated 层
（china_seed 种子谱系 + modern_index 近现代索引），详见 bootstrap_curated 的说明。
"""

from __future__ import annotations

from fastmcp import FastMCP

from . import china_lineage, rest_api, schema, tools_placeholder, tools_registry
from .config import Settings

SERVER_NAME = "fd-zhengxin-reference"

INSTRUCTIONS = (
    "政心参照系：政治概念（SEP 引用层 + 中国线种子/近现代索引）、思潮坐标"
    "（Wikidata P1142/P1387 + MARPOR 立场）、思想关系边（Wikidata + 中国线种子）、"
    "政体指标时序（V-Dem）。所有结果携带 provenance（source/stable_id/as_of/"
    "attribution/snapshot_version）；空结果必带 reason（blocked/upstream/"
    "unreachable/not_ingested），多源融合时各源状态在 sources 分项如实标注；"
    "源未过许可门则 not_ingested 且注明原因（marpor license_unconfirmed），绝不编造。"
)


def bootstrap_curated(settings: Settings) -> dict:
    """启动初始化（启动钩子形态）：加载 curated 层，幂等接线。

    - china_seed 与 modern_index 需在 schema.init 之后调用（china_lineage 模块
      说明）；本函数自开连接、自 init，加载完即关——之后所有工具按连接读库。
    - 幂等：快照内容寻址复用 + 行覆写式重建（load_china_seed / load_modern_index
      契约），create_server 重复调用结果一致。
    - 选启动钩子而非首次惰性的原因：curated 层是全库基座（种子概念/关系、五思潮
      索引），启动即就位使 source_status 与首个查询的结果确定；惰性初始化会把
      加载延迟与加载失败混进查询路径，且 :memory: 形态下每次连接都要重载。
      DB 为 :memory: 时跳过（每条连接各自新建内存库，加载即弃，写快照根只会
      留垃圾）。
    - seed 文件缺失 → 跳过并如实回报（Docker 等部署形态可不带 seed/，缺口在
      source_status 可见）；seed 结构违例 → 抛 SeedInvalid（数据坏了不静默）。
    """
    if settings.db_path == ":memory:":
        skipped = {"status": "skipped", "reason": ":memory: 库按连接新建，加载即弃"}
        return {"china-seed": skipped, "modern-index": skipped}

    report: dict = {}
    conn = settings.connect()
    try:
        schema.init(conn)
        try:
            report["china-seed"] = {"status": "ok", **china_lineage.load_china_seed(conn)}
        except FileNotFoundError:
            report["china-seed"] = {
                "status": "skipped",
                "reason": "seed 文件缺失（china_lineage.default_seed_path）；source_status 如实记 not_ingested",
            }
        report["modern-index"] = {"status": "ok", **china_lineage.load_modern_index(conn)}
    finally:
        conn.close()
    return report


def create_server(settings: Settings | None = None) -> FastMCP:
    settings = settings or Settings.from_env()
    bootstrap_curated(settings)  # 启动钩子：curated 层先就位，再挂工具面
    mcp: FastMCP = FastMCP(SERVER_NAME, instructions=INSTRUCTIONS)

    from . import wellknown

    wellknown.register(mcp)  # /.well-known/line-manifest.json（公开、免鉴权、零网络）
    tools_placeholder.register(mcp, settings)
    from . import entity_dossier

    entity_dossier.register(mcp, settings)
    tools_registry.register(mcp, settings)
    return mcp


def build_http_app(settings: Settings | None = None, middleware=None):
    """完整 HTTP 面：/mcp（MCP streamable-http）+ /healthz + /api/v1/*（只读 REST）。

    cli.serve 与测试共用本拼装（route 追加顺序不影响：三个面路径互不相交）。

    ``middleware`` 由调用方决定（change converge-line-identity-and-keys 8.6：配了
    ``MCP_BEARER_TOKEN`` 才传共享 bearer 门进来）。默认 ``None`` = 与接入前逐字节
    一致。门对 /healthz 有精确豁免，探针不受影响。
    """
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    settings = settings or Settings.from_env()
    app = create_server(settings).http_app(path="/mcp", middleware=middleware)

    async def healthz(_: Request) -> JSONResponse:
        from . import __version__

        return JSONResponse(
            {"status": "ok", "service": SERVER_NAME, "version": __version__}
        )

    app.routes.append(Route("/healthz", healthz))
    rest_api.add_routes(app, settings)
    return app
