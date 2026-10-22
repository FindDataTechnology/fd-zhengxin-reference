# fd-zhengxin-reference image — 政心（lex 第四模块）参照系 MCP。
#
# 发布通道：GitHub Actions -> TCR（照 fd-zhesi-reference / fd-legal-search-mcp 的
# image.yml 配方）。TCR 出片与集群部署在后续波次接通；本仓暂为本地仓（无 remote）。
# 上游源为默认；国内镜像源一律 ARG 且默认空；单层 <300MB。
FROM python:3.12-slim

ARG PIP_INDEX_URL=""

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY fd_zhengxin_reference ./fd_zhengxin_reference
COPY seed ./seed

RUN if [ -n "$PIP_INDEX_URL" ]; then pip install --no-cache-dir -i "$PIP_INDEX_URL" .; else pip install --no-cache-dir .; fi \
 && useradd -m -u 1000 appuser

USER appuser

EXPOSE 8000

CMD ["fd-zhengxin-reference", "serve", "--host", "0.0.0.0", "--port", "8000"]
