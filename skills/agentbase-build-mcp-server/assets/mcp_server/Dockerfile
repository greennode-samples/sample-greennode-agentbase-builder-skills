# MCP server for AgentBase Runtime: 0.0.0.0:8080, GET /health, POST /mcp. Build linux/amd64.
# Fails closed: MCP_APP_ENV=prod refuses MCP_AUTH_MODE=none ⇒ the runtime env must set api_key or jwt.
# Two stages: uv and its cache stay in the builder; the runtime image only gets Python, the venv and the code.
FROM python:3.13-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:0.10 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
# Dependency layer (cache-friendly); --locked fails if uv.lock is out of date with pyproject.toml
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY . .
RUN uv sync --locked --no-dev

FROM python:3.13-slim
# /app stays owned by root (read-only for the server user) — no `chown -R` layer. The throwaway SQLite demo
# (MCP_ALLOW_SQLITE=true) therefore writes to /tmp; production uses Postgres (store.py).
ENV PYTHONUNBUFFERED=1 PATH="/app/.venv/bin:$PATH" MCP_APP_ENV=prod MCP_DB_PATH=/tmp/mcp.db
RUN useradd --create-home --uid 10001 mcp
WORKDIR /app
COPY --from=builder /app /app
USER mcp
EXPOSE 8080
CMD ["python", "server.py"]
