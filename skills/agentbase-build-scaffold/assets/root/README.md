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
make set-secret      # in YOUR terminal: hidden prompt for the IAM client_id/secret (→ src/backend/.greennode.json)
                     # and LLM_API_KEY (→ src/backend/.env); NAME=langfuse etc. for one group, LIST=1 shows what is set
# non-secret settings (LLM_MODEL, MEMORY_ID…): edit src/backend/.env (the scaffold created it — never re-copy over it)
make check-creds     # prints OK/FAIL only
make test
make dev
make invoke MSG="hello"
```

Secrets never go into a chat with a coding agent (everything pasted there is logged by the agent host): the agent asks
you to run `make set-secret` instead — it cannot type into the hidden prompt. One leaked anyway (chat, commit, log)?
Rotate it: reset the IAM service account secret in the IAM console, create a new LLM API key with `/agentbase-llm` and
delete the old one, regenerate Langfuse keys in the project settings — then `make set-secret` the new values.

## Architecture & conventions

See `src/backend/app/*` — each module has a docstring describing its responsibility. API contract: `src/backend/app/service.py`.

## Deploy

- Per-environment config is committed in `src/backend/.env.<env>.example` (non-secret; secrets are `<set-in-deploy>`). Lint with `make check-env ENV_FILE=src/backend/.env.<env>.example` — the platform reads env files literally (no quotes, no inline comments, no `export`).
- CI (`.github/workflows/ci.yml`): run the workflow manually with `deploy_env` (and `image_tag` to redeploy an existing image) — secrets come from the GitHub Environment: set each one yourself with `gh secret set <NAME> --env <env>` (it prompts without echo; list in the header of `ci.yml`).
- Manual: `make set-secret ENV_FILE=src/backend/.env.<env>` creates the git-ignored copy of the example and asks for each `<set-in-deploy>` secret (hidden), `make check-env ENV_FILE=src/backend/.env.<env>`, `make docker-build TAG=<tag>`, then `/agentbase-deploy` (push + create/update only — the image is built). Details: skill `agentbase-build-deploy`.
