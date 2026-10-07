# MCP server for AgentBase Runtime: 0.0.0.0:8080, GET /health, POST /mcp. Build linux/amd64.
FROM python:3.13-slim
COPY --from=ghcr.io/astral-sh/uv:0.10 /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY . .
RUN uv sync --frozen --no-dev && useradd --create-home --uid 10001 mcp && chown -R mcp /app
USER mcp
EXPOSE 8080
CMD ["python", "server.py"]
