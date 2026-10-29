# MCP server for AgentBase Runtime: 0.0.0.0:8080, GET /health, POST /mcp. Build linux/amd64.
# Fails closed: MCP_APP_ENV=prod refuses MCP_AUTH_MODE=none ⇒ the runtime env must set api_key or jwt.
# Two stages: uv and its cache stay in the builder; the runtime image only gets Python, the venv and the code.
# Base images are pinned by digest (the tag stays for readability): a build never changes under you. Dependabot's
# docker ecosystem proposes the new digests; both Dockerfiles of these skills move together (check_skills.py).
FROM ghcr.io/astral-sh/uv:0.10@sha256:72ab0aeb448090480ccabb99fb5f52b0dc3c71923bffb5e2e26517a1c27b7fec AS uv
FROM python:3.13-slim@sha256:2d44738ebf4ce5f63b9d9ea871e9c7cfdc0426f7b0c97cac915fd88ded1d86bb AS builder
COPY --from=uv /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
# Dependency layer (cache-friendly); --locked fails if uv.lock is out of date with pyproject.toml
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY . .
RUN uv sync --locked --no-dev

FROM python:3.13-slim@sha256:2d44738ebf4ce5f63b9d9ea871e9c7cfdc0426f7b0c97cac915fd88ded1d86bb
# /app stays owned by root (read-only for the server user) — no `chown -R` layer. The throwaway SQLite demo
# (MCP_ALLOW_SQLITE=true) therefore writes to /tmp; production uses Postgres (store.py).
ENV PYTHONUNBUFFERED=1 PATH="/app/.venv/bin:$PATH" MCP_APP_ENV=prod MCP_DB_PATH=/tmp/mcp.db
RUN useradd --create-home --uid 10001 mcp
WORKDIR /app
COPY --from=builder /app /app
USER mcp
EXPOSE 8080
CMD ["python", "server.py"]
