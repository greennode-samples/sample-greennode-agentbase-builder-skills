# __PROJECT_NAME__

AgentBase agent on GreenNode AgentBase — standardized by the `agentbase-build` skill.

| Component | Technology |
|---|---|
| Backend | Python 3.13 · uv · LangGraph · `greennode-agentbase` (`src/backend`) |
| LLM | GreenNode AI Platform MaaS (OpenAI-compatible) |
| Short-term memory | AgentBase Memory — `AgentBaseMemoryEvents` checkpointer |
| Long-term memory | AgentBase Memory records — auto-recall + tools `remember`/`recall_memory` |
| Context compression | Rolling summary + hard trim |
| Tools | Local tools + MCP via AgentBase MCP Gateway (Connector + Policy Group) |
| Auth | JWT (OIDC/JWKS) inbound · AgentBase Identity outbound |
| Tracing | Langfuse |
| Frontend | Expo React Native (`src/frontend`, optional) |

## Run locally

```bash
make setup
# the scaffold already created src/backend/.env — edit it (LLM_API_KEY, LLM_MODEL), never re-copy over it
# IAM: copy src/backend/.greennode.json.example to .greennode.json and fill it in; then: make check-creds
make test
make dev
make invoke MSG="hello"
```

## Architecture & conventions

See `src/backend/app/*` — each module has a docstring describing its responsibility. API contract: `src/backend/app/service.py`.

## Deploy

- Per-environment config is committed in `src/backend/.env.<env>.example` (non-secret; secrets are `<set-in-deploy>`). Lint with `make check-env ENV_FILE=src/backend/.env.<env>.example` — the platform reads env files literally (no quotes, no inline comments, no `export`).
- CI (`.github/workflows/ci.yml`): run the workflow manually with `deploy_env` (and `image_tag` to redeploy an existing image) — secrets come from the GitHub Environment.
- Manual: copy the example to `src/backend/.env.<env>` and fill the secrets yourself, `make docker-build TAG=<tag>`, then `/agentbase-deploy` (push + create/update only — the image is built). Details: skill `agentbase-build-deploy`.
