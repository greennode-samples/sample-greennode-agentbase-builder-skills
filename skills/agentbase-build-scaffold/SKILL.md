---
name: agentbase-build-scaffold
description: "Scaffold a standard AI agent project for GreenNode AgentBase: src/backend (Python 3.13 + uv + LangGraph + greennode-agentbase) and optional src/frontend (Expo React Native), plus Makefile, Dockerfile, tests, evals. Use when starting a new agent project, initializing the structure, or managing dependencies/env with uv (uv add, uv sync, uv lock, uv run). Trigger: scaffold agent, init project agent, create project structure, initialize agent backend, manage env with uv, add dependency, tạo cấu trúc project, khởi tạo backend agent, quản lý env bằng uv, thêm dependency. DO NOT use for deploy (/agentbase-build-deploy) or configuring individual components (memory, tracing, mcp, auth — use the corresponding agentbase-build-* skill)."
---

# Scaffold an AgentBase agent project (uv + LangGraph)

## Step 1 — Gather input (ask the user, don't decide yourself)

- **Project name**: `^[a-z][a-z0-9-]{1,38}[a-z0-9]$` — 3–40 chars, lowercase letters/digits/`-`, starts with a letter, does not end with `-` (used for the image name, Langfuse tag, `AGENT_NAME`). Name with spaces/uppercase ⇒ propose a normalized version and confirm.
- **Target directory** (default: current directory).
- **Need a UI?** ⇒ add `--with-frontend` (Expo React Native).

Present a summary and **wait for confirmation (HARD GATE)** before creating files. If `src/backend` already exists ⇒ stop and ask the user (the script also refuses to overwrite).

## Step 2 — Run the scaffold script

The script lives in this skill's directory (the one containing SKILL.md):

```bash
bash <skill-dir>/scripts/scaffold.sh <project-name> <target-dir> [--with-frontend] [--no-sync]
```

The script merges the `assets/backend/` of these skills: scaffold · llm · memory · tracing · mcp · auth · identity · hitl · eval · a2a (each skill owns its module), adds the root files, replaces `__PROJECT_NAME__`, creates `.env` from `.env.example`, runs `uv sync` and `uv run pytest`. If sync or tests fail, the script prints the recovery command (`cd src/backend && uv sync && uv run pytest`); if it fails while writing files, it says what to remove before re-running.

**All backend modules are required.** Optional components (long-term memory, reflection, HITL, MCP, A2A) ship code but are **disabled via env** — enable per the decision guide; do not delete their files: the modules import each other (`app/tools` imports `app/a2a`, the graph imports `app/reflection`/`app/hitl`, `app/service` imports `app/tools.mcp`/`app/hitl`…). Only `app/identity.py` (+ `tests/test_identity.py`) may be removed when no tool uses outbound credentials.

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
| Install env from lock | `uv sync` (CI: `uv sync --locked`; Docker: `uv sync --locked --no-dev` — both fail when `uv.lock` no longer matches `pyproject.toml`) |
| Add dependency | `uv add <pkg>` · dev: `uv add --dev <pkg>` · extra: `uv add "pkg[extra]"` |
| Remove dependency | `uv remove <pkg>` |
| Upgrade version | `uv lock --upgrade-package <pkg>` then `uv sync` |
| Run commands | `uv run python main.py` · `uv run pytest` · `uv run ruff check .` |
| Python version | **Single standard: Python 3.13** — `.python-version` = `3.13`, `requires-python = ">=3.13,<3.14"`, Docker `python:3.13-slim`. uv downloads 3.13 automatically if missing (`uv python install 3.13`) |

Rules: **every Python command runs via `uv run`** (including one-liners: `uv run python -c ...`) — don't call `python`/`python3` directly since macOS/Linux often ship an old system Python (e.g. 3.9) with the wrong version and none of the project's dependencies; **no** `pip install`, **no** `requirements.txt`, **no** manual venv activation in instructions; always commit `uv.lock`; the Dockerfile and CI use `uv sync --locked` for reproducible builds.

### Env files

| File | Used for | Commit? |
|---|---|---|
| `.env.example` | Full template of every `Settings` variable | ✔ |
| `.env` | Local dev (`APP_ENV=local`, may use `AUTH_MODE=none`, `MEMORY_BACKEND=inmemory`) | ✖ |
| `.env.dev.example` / `.env.staging.example` / `.env.prod.example` | Per-environment **non-secret** runtime config (secrets `<set-in-deploy>`); CI merges it with the secret store | ✔ |
| `.env.dev` / `.env.staging` / `.env.prod` | Copy of the `.example` with secrets filled by the user — passed to the runtime by `/agentbase-deploy --env-file` | ✖ |
| `.greennode.json` | IAM credentials for local dev (read by the SDK). The **user** creates it from `.greennode.json.example` — never via chat | ✖ |
| `.greennode.json.example` | Template `{client_id, client_secret, agent_identity}` (`agent_identity`: the **dev** identity, only when tools use Identity providers) | ✔ |

`make check-creds` (`scripts/check_creds.py`) verifies IAM, the LLM key against **every** tier's primary and fallback model (FAIL for a primary, WARN for a fallback), the agent identity and Langfuse keys, and prints only `OK/WARN/FAIL` + non-secret IDs (the **developer's** IAM principal `iam:<sub>` from the local `.greennode.json` — the runtime has its own, see `/agentbase-build-deploy` Step 5). Run it after the user fills the files.

Deploy env files are read **literally** by the platform (`runtime.sh --env-file`) and by `docker run --env-file`, unlike python-dotenv for the local `.env`: one `KEY=VALUE` per line, no quotes, no inline comments, no `export`, no `${VAR}`. `make check-env ENV_FILE=src/backend/.env.<env>` (`deploy/env_file.py`) lints a file and prints key names only.

`APP_ENV=local` (the default) is **refused on the Runtime** (detected: `GREENNODE_AGENT_IDENTITY` injected by the platform **and** no `.greennode.json` — locally the IAM pair always lives in that file), so a deploy env file that forgets `APP_ENV` cannot fall back to `AUTH_MODE=none`. `make dev` binds `127.0.0.1` when `APP_ENV=local` + `AUTH_MODE=none` outside a container (`0.0.0.0` otherwise, as the Runtime requires).

Deploy env files **must not contain** `GREENNODE_CLIENT_ID`, `GREENNODE_CLIENT_SECRET`, `GREENNODE_AGENT_IDENTITY` (the runtime injects them) or `GREENNODE_ENDPOINT_URL` (platform-reserved name; set `A2A_PUBLIC_URL` instead).

## What the scaffold creates

See the directory tree and module responsibilities in `/agentbase-build` → `references/architecture.md`. Files owned by this skill:

- `assets/backend/pyproject.toml` — standard dependencies: every package the code imports directly is declared, each capped at its next major (`greennode-agentbase` at the next minor — the code relies on SDK internals); test-only imports in the `dev` group.
- `assets/backend/main.py` — thin entrypoint; **keep `XAccelBufferingMiddleware`** when adding middleware (the SDK drops its default middleware if you pass `middleware=`; without it SSE gets buffered).
- `assets/backend/app/config.py` — `Settings` (single env source, `env_ignore_empty` ⇒ `KEY=` uses the default) + safety validation (forbids `AUTH_MODE=none` outside local) + `normalize_iam_env()` (prevents mismatched IAM pairs).
- `assets/backend/app/service.py` — payload contract, orchestration, HITL resume (interrupt taken from graph output), feedback token, `run_chat()`/`run_resume()`/`pending_tool_calls()` for eval & A2A.
- `assets/backend/app/graph/{state,builder}.py` — the single graph.
- `assets/backend/app/prompts/system.md` — system prompt (rewrite for your domain, but keep its "Dates & time" and "Memory" sections).
- `assets/backend/tests/{conftest,test_agent,test_isolation,test_service,test_turn_context}.py` — `fake_llm` fixture (patches `get_llm` in every module listed in `LLM_MODULES`; add new LLM-calling modules there); conftest overrides env so tests don't depend on `.env`; per-user isolation tests; service error mapping (stale resume ⇒ 409, LLM config errors); per-turn context (current date/time in `APP_TIMEZONE`, recalled notes framed as data).
- `assets/backend/.python-version` — `3.13`.
- `assets/backend/Dockerfile` — multi-stage `python:3.13-slim`: uv only in the builder; the runtime stage gets the venv + code (root-owned, read-only for the non-root `agent` user), `ARG AGENT_VERSION` → `ENV AGENT_VERSION` (Langfuse release = image tag), port 8080. `.dockerignore` uses `**/` patterns so nested caches/secrets are excluded too.
- `assets/root/*` — Makefile, README, .gitignore, `.agentbase-state.json` (flat keys used by the platform wizard + `standard_version`, `scaffold_version` = the skills' git SHA or version, and `environments.{dev,staging,prod}` with runtime/endpoint/network/inbound auth/identity/memory/LLM key/gateway/policy group/principal per environment), `ci.yml` → `.github/workflows/ci.yml` (lint + test, eval gate, manual deploy/redeploy), `agent.yaml.tpl` → `deploy/agent.yaml.tpl` (manifest for `grn agentbase deploy up`, rendered in CI), `render_runtime_spec.py` → `deploy/render_runtime_spec.py` (merges config + secrets, spec for `grn agentbase runtime update`), `env_file.py` → `deploy/env_file.py` (`make check-env`) and `env.deploy.example` → `src/backend/.env.{dev,staging,prod}.example`. Existing files are never overwritten — see `/agentbase-build-deploy` *CI/CD*.

## After scaffolding — customize for your domain

1. Write `app/prompts/system.md` per the Agent Spec. Keep the **"Dates & time"** and **"Memory"** sections: they tell the model how to use the per-turn date/time and to treat recalled notes as data, not instructions.
2. Add tools in `app/tools/local_tools.py` or via MCP (`/agentbase-build-mcp`).
3. Add domain nodes in `app/graph/builder.py` (if you need a router/sub-agent), state fields in `state.py`.
4. Add tests for each tool/node; add eval items to `evals/datasets/`.

## Official docs

- [runtime-reference](https://docs.greennode.ai/ai-stack/agent-base/agent-runtime/runtime-reference) — service contract the scaffold implements (port, `/health`, injected `GREENNODE_*`, session/user headers)
- [getting-started](https://docs.greennode.ai/ai-stack/agent-base/getting-started) — creating the IAM service account and `.greennode.json` for local dev
- [supporting-services](https://docs.greennode.ai/ai-stack/agent-base/supporting-services) — SDK install, vCR robot accounts, AIP API keys
