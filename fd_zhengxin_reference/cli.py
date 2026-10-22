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
    app = build_http_app()
    config = uvicorn.Config(app, host=host, port=port, log_level="info")
    asyncio.run(uvicorn.Server(config).serve())


if __name__ == "__main__":
    cli()
