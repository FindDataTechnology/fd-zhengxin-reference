"""CLI：serve（HTTP 面 = /mcp + /healthz + /api/v1/* 只读 REST 锚点）。"""

from __future__ import annotations

import asyncio

import click
import uvicorn

from . import __version__
from .server import build_http_app


@click.group()
@click.version_option(__version__)
def cli() -> None:
    """fd-zhengxin-reference：政心参照系 MCP。"""


@cli.command()
@click.option("--host", default="0.0.0.0", show_default=True)
@click.option("--port", default=8000, show_default=True)
def serve(host: str, port: int) -> None:
    """启动 HTTP 服务（/mcp + /healthz + /api/v1/*）。"""
    from .auth import BearerAuthMiddleware, configured_token

    middleware = None
    if configured_token():
        # 配了 token 才挂共享 bearer 门（change converge-line-identity-and-keys 8.6）；
        # 未配 = 与接入前逐字节一致（不挂任何中间件）。门对 /healthz 精确豁免。
        from starlette.middleware import Middleware

        middleware = [Middleware(BearerAuthMiddleware)]

    app = build_http_app(middleware=middleware)
    config = uvicorn.Config(app, host=host, port=port, log_level="info")
    asyncio.run(uvicorn.Server(config).serve())


if __name__ == "__main__":
    cli()
