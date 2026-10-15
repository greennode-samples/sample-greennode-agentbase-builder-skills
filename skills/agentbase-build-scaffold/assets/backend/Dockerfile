# AgentBase Runtime: listen 0.0.0.0:8080, GET /health -> 200. Build for linux/amd64 (`make docker-build`).
# Two stages: uv and its cache stay in the builder; the runtime image only gets Python, the venv and the code.
FROM python:3.13-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.10 /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependency layer (cache-friendly): copy only the lockfile first
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

COPY . .
RUN uv sync --locked --no-dev && python -m compileall -q main.py app

# python:3.13-slim ships /usr/share/zoneinfo; the tzdata package in the venv covers APP_TIMEZONE regardless
FROM python:3.13-slim

# Release shown in Langfuse (tracing `release`) = the image tag: `make docker-build` and CI pass --build-arg.
# A deploy env file must not set AGENT_VERSION (it would override this value).
ARG AGENT_VERSION=dev
ENV AGENT_VERSION=$AGENT_VERSION \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

RUN useradd --create-home --uid 10001 agent
WORKDIR /app
# Code and venv stay owned by root (read-only for the agent user) — no `chown -R` layer
COPY --from=builder /app /app
USER agent

EXPOSE 8080
CMD ["python", "main.py"]
