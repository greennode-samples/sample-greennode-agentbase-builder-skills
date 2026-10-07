---
name: agentbase-build-scaffold
description: "Scaffold a standard AI agent project for GreenNode AgentBase: src/backend (Python 3.13 + uv + LangGraph + greennode-agentbase) and optional src/frontend (Expo React Native), plus Makefile, Dockerfile, tests, evals. Use when starting a new agent project, initializing the structure, or managing dependencies/env with uv (uv add, uv sync, uv lock, uv run). Trigger: scaffold agent, init project agent, create project structure, initialize agent backend, manage env with uv, add dependency, tạo cấu trúc project, khởi tạo backend agent, quản lý env bằng uv, thêm dependency. DO NOT use for deploy (/agentbase-build-deploy) or configuring individual components (memory, tracing, mcp, auth — use the corresponding agentbase-build-* skill)."
---

# Scaffold an AgentBase agent project (uv + LangGraph)

## Step 1 — Gather input (ask the user, don't decide yourself)

- **Project name**: `^[a-z][a-z0-9-]{2,39}$` (used for the image name, Langfuse tag, `AGENT_NAME`). Name with spaces/uppercase ⇒ propose a normalized version and confirm.
- **Target directory** (default: current directory).
- **Need a UI?** ⇒ add `--with-frontend` (Expo React Native).

Present a summary and **wait for confirmation (HARD GATE)** before creating files. If `src/backend` already exists ⇒ stop and ask the user (the script also refuses to overwrite).

## Step 2 — Run the scaffold script

The script lives in this skill's directory (the one containing SKILL.md):

```bash
bash <skill-dir>/scripts/scaffold.sh <project-name> <target-dir> [--with-frontend] [--no-sync]
```

The script merges the `assets/backend/` of these skills: scaffold · llm · memory · tracing · mcp · auth · hitl · eval · a2a (each skill owns its module; optional components ship code but are **disabled via env** — enable per the decision guide), replaces `__PROJECT_NAME__`, creates `.env` from `.env.example`, runs `uv sync` and `uv run pytest`.

Can't run the script (Windows without bash, missing skill) ⇒ copy manually following the table in the `scripts/scaffold.sh` header.

## Step 3 — Verify

```bash
cd <target-dir>
make test        # must be green (offline tests: fake LLM, in-memory memory, auth none)
make lint
```

Report to the user: list of created files + next steps (`/agentbase-build-llm` to get a key, `/agentbase-build-memory` to create memory).

## Managing the environment with uv (MANDATORY)

| Task | Command (run in `src/backend`) |
|---|---|
| Install env from lock | `uv sync` (CI/Docker: `uv sync --frozen --no-dev`) |
| Add dependency | `uv add <pkg>` · dev: `uv add --dev <pkg>` · extra: `uv add "pkg[extra]"` |
| Remove dependency | `uv remove <pkg>` |
| Upgrade version | `uv lock --upgrade-package <pkg>` then `uv sync` |
| Run commands | `uv run python main.py` · `uv run pytest` · `uv run ruff check .` |
| Python version | **Single standard: Python 3.13** — `.python-version` = `3.13`, `requires-python = ">=3.13,<3.14"`, Docker `python:3.13-slim`. uv downloads 3.13 automatically if missing (`uv python install 3.13`) |

Rules: **every Python command runs via `uv run`** (including one-liners: `uv run python -c ...`) — don't call `python`/`python3` directly since macOS/Linux often ship an old system Python (e.g. 3.9) with the wrong version and none of the project's dependencies; **no** `pip install`, **no** `requirements.txt`, **no** manual venv activation in instructions; always commit `uv.lock`; the Dockerfile uses `uv sync --frozen` for reproducible builds.

### Env files

| File | Used for | Commit? |
|---|---|---|
| `.env.example` | Full template of every `Settings` variable | ✔ |
| `.env` | Local dev (`APP_ENV=local`, may use `AUTH_MODE=none`, `MEMORY_BACKEND=inmemory`) | ✖ |
| `.env.dev` / `.env.staging` / `.env.prod` | Passed to the runtime on deploy (`/agentbase-deploy --env-file`) | ✖ |
| `.greennode.json` | IAM credentials for local dev (read by the SDK) | ✖ |

Deploy env files **must not contain** `GREENNODE_CLIENT_ID`, `GREENNODE_CLIENT_SECRET`, `GREENNODE_AGENT_IDENTITY`, `GREENNODE_ENDPOINT_URL` (the runtime injects them).

## What the scaffold creates

See the directory tree and module responsibilities in `/agentbase-build` → `references/architecture.md`. Files owned by this skill:

- `assets/backend/pyproject.toml` — standard dependencies, pinned major versions.
- `assets/backend/main.py` — thin entrypoint; **keep `XAccelBufferingMiddleware`** when adding middleware (the SDK drops its default middleware if you pass `middleware=`; without it SSE gets buffered).
- `assets/backend/app/config.py` — `Settings` (single env source, `env_ignore_empty` ⇒ `KEY=` uses the default) + safety validation (forbids `AUTH_MODE=none` outside local) + `normalize_iam_env()` (prevents mismatched IAM pairs).
- `assets/backend/app/service.py` — payload contract, orchestration, HITL resume (interrupt taken from graph output), feedback token, `run_chat()`/`run_resume()`/`pending_tool_calls()` for eval & A2A.
- `assets/backend/app/graph/{state,builder}.py` — the single graph.
- `assets/backend/app/prompts/system.md` — system prompt (rewrite for your domain).
- `assets/backend/tests/{conftest,test_agent,test_isolation}.py` — `fake_llm` fixture (patches `get_llm` in every module listed in `LLM_MODULES`; add new LLM-calling modules there); conftest overrides env so tests don't depend on `.env`; per-user isolation tests.
- `assets/backend/.python-version` — `3.13`.
- `assets/backend/Dockerfile` — `python:3.13-slim` + uv, non-root, port 8080.
- `assets/root/*` — Makefile, README, .gitignore, `.agentbase-state.json`.

## After scaffolding — customize for your domain

1. Write `app/prompts/system.md` per the Agent Spec.
2. Add tools in `app/tools/local_tools.py` or via MCP (`/agentbase-build-mcp`).
3. Add domain nodes in `app/graph/builder.py` (if you need a router/sub-agent), state fields in `state.py`.
4. Add tests for each tool/node; add eval items to `evals/datasets/`.
