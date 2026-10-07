---
name: agentbase-build-deploy
description: "Workflow for shipping an agentbase-build standard AI agent (src/backend, uv, Docker) to GreenNode AgentBase Runtime: pre-flight (test, eval, lint, checklist), per-environment env files, build linux/amd64 image with uv, push to AgentBase Container Registry and create/update the runtime via /agentbase-deploy, verify health + Langfuse traces + logs, rollback. Use when deploying/shipping/releasing an agent built to the standard, creating dev/staging/prod environments, updating versions. Trigger: deploy standard agent, release agent, ship agent, push agent to runtime, update agent version, đưa agent lên runtime, cập nhật version agent. DO NOT use for the OpenClaw chatbot template (call /agentbase-deploy directly) or viewing logs (use /agentbase-monitor)."
---

# Deploy an AgentBase agent

This skill **prepares and verifies**; platform operations (CR, runtime, endpoint) are performed by **`/agentbase-deploy`** under that skill's HARD GATE.

## Step 1 — Pre-flight (all must PASS)

```bash
make lint && make test
make eval MIN=<threshold from spec>
cd src/backend && uv lock --check
```

- Run `/agentbase-build` → `references/checklist.md`; all **[B]** items must PASS.
- Bump `AGENT_VERSION` (semver) — used as the image tag + `release` in Langfuse.

## Step 2 — Per-environment env file

Create `src/backend/.env.<env>` (not committed) from `.env.example`. To review per-environment config in PRs ⇒ commit `.env.<env>.example` (non-secret values only, secrets as `<set-in-deploy>`; `.gitignore` already allows `!.env.*.example`). Note `env_ignore_empty=True`: empty variables = use the default in `config.py`.

| Variable | dev | prod |
|---|---|---|
| `APP_ENV` | `dev` | `prod` — **required**: `local` is refused on the Runtime |
| `MEMORY_BACKEND` / `MEMORY_ID` | `agentbase` / dev memory | `agentbase` / prod memory (separate) |
| `AUTH_MODE` | `jwt` | `jwt` + `AUTH_JWKS_URL`, `AUTH_ISSUER`, `AUTH_AUDIENCE` (or `AUTH_ALLOW_NO_AUDIENCE=true` + `AUTH_ALLOWED_CLIENT_IDS`) |
| `LLM_*` | dev key | prod key |
| `LANGFUSE_*` | dev project/env | prod project, `LANGFUSE_SAMPLE_RATE` per load |
| `MCP_GATEWAY_URL`, `HITL_TOOLS` | per environment | |

**Do not** include `GREENNODE_CLIENT_ID`, `GREENNODE_CLIENT_SECRET`, `GREENNODE_AGENT_IDENTITY`, `GREENNODE_ENDPOINT_URL` (the runtime injects them). Remind the user to review the env file themselves; **do not** print the contents of secret-bearing files into chat.

## Step 3 — Build the image

```bash
make docker-build IMAGE=<registry>/<repo>/<project> TAG=<AGENT_VERSION>
make docker-run IMAGE=... TAG=...   # local smoke test: curl :8080/health
make docker-run IMAGE=... TAG=... ENV_FILE=src/backend/.env.prod   # boots with the DEPLOY config: catches a missing APP_ENV / AUTH_* before the Runtime does
```

Standard Dockerfile: `python:3.13-slim` + uv, `uv sync --frozen --no-dev`, non-root, `EXPOSE 8080`, build with `--platform linux/amd64` (mandatory when building on ARM Macs).

## Step 4 — Push & create/update the runtime

Call **`/agentbase-deploy`** with: image + tag, env file `src/backend/.env.<env>`, user-chosen flavor/autoscaling, network mode (PUBLIC; VPC if MCP server/Langfuse are internal). Save `runtime_id` and endpoint to `.agentbase-state.json`.

## Run for real (2026-10, runtime `test-agent`, flavor `runtime-s2-general-2x4`)

Push CR → create runtime → ACTIVE in ~40s; version update ~30s. Checks used: health 200 · no key/wrong key ⇒ 401 · chat · short-term (AgentBaseMemoryEvents) · long-term across sessions · user isolation · stream · MCP via gateway (controlled policy deny). Caught 2 memory SDK bugs that only surface on real memory (see `/agentbase-build-memory`) ⇒ **always smoke test on the runtime after deploy**. Find the runtime's principal in the logs: `runtime.sh logs <id> --query "IAM principal"`, then grant MCP permissions via `/agentbase-policy`.

## Step 5 — Verify

1. `curl <endpoint>/health` → 200.
2. Call `/invocations` with a real JWT (or via BFF) → `status: success`.
3. Langfuse: traces with `environment=<env>`, `release=<AGENT_VERSION>`, standard tree, cost present.
4. `/agentbase-monitor`: no ERROR in logs, CPU/RAM healthy.
5. (Optional) `make eval` pointed at the freshly deployed environment.

## Scale (multiple replicas)

- Memory: limit of 10 concurrent requests per IAM account, shared across all replicas ⇒ `MEMORY_MAX_CONCURRENCY ≈ 10 / max replicas`.
- A2A: `InMemoryTaskStore` is not shared across replicas ⇒ use `DatabaseTaskStore` when `max_replicas > 1`.
- In-process caches (MCP tool list, negative cache, JWKS) are per-replica — acceptable.

## CI/CD & permissions

- CI uses a **dedicated service account** (IAM) with only CR push + runtime update permissions for the project, never a personal SA. Secrets live in the CI secret store.
- Standard pipeline: `make lint test` → `make eval MIN=…` → build amd64 → push CR → `runtime.sh update --env-file` (env file from the secret store) → smoke test `/health` + 1 keyed request → check traces.

## Cleanup after testing

Runtimes are billed continuously. Delete test runtimes, memory, API keys, connectors with `/agentbase-teardown` (or each platform skill); revoke the agent's API keys (remove hashes from `AUTH_API_KEY_SHA256`).

## Rollback

Use `/agentbase-deploy`'s endpoint/version controls to point DEFAULT back to the previous version; compare traces by `release` to confirm recovery.

## Official docs

- [create-runtime](https://docs.greennode.ai/ai-stack/agent-base/agent-runtime/create-runtime) — create a runtime: image, env, autoscaling (1–10 replicas), Security Settings
- [manage-runtime](https://docs.greennode.ai/ai-stack/agent-base/agent-runtime/manage-runtime) — update, stop/start, endpoints/versions, rollback
- [runtime-reference](https://docs.greennode.ai/ai-stack/agent-base/agent-runtime/runtime-reference) — PATCH requires every field except `imageAuth`; reset-service-account
- [container-registry](https://docs.greennode.ai/ai-stack/agent-base/container-registry) — vCR / AgentBase Container Registry
- [logs-and-metrics](https://docs.greennode.ai/ai-stack/agent-base/agent-runtime/logs-and-metrics) — logs and metrics after deploy
- [manage-agentbase-with-the-greennode-cli](https://docs.greennode.ai/ai-stack/agent-base/manage-agentbase-with-the-greennode-cli) — `grn agentbase …` CLI incl. `deploy up`
