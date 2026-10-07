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

Call **`/agentbase-deploy`** with: image + tag, env file `src/backend/.env.<env>`, user-chosen flavor/autoscaling, and:

- **Agent identity** — a Runtime is always bound to an identity, and staging/prod may share one. Use the identity that holds this agent's providers (`/agentbase-build-identity`); otherwise `@agent_api_key` / `@user_access_token` return 404 or "can't retrieve credential". The Runtime injects it as `GREENNODE_AGENT_IDENTITY`.
  - With per-user credentials, `IDENTITY_CALLBACK_URL` in `.env.<env>` must be this environment's page and must be listed in the identity's `allowedReturnUrls`.
- **Network mode** — Public by default. Private (VPC, Subnet, Route CIDRs; needs VPC Peering) only when agent code must reach an internal API or a self-hosted service; internal MCP servers go through a Private MCP Gateway instead — see `/agentbase-build` `references/private-networking.md`.

Save `runtime_id`, endpoint, identity name and network mode to `.agentbase-state.json`.

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

## Alternative: GreenNode CLI (`grn agentbase deploy`)

[`grn`](https://docs.greennode.ai/ai-stack/agent-base/manage-agentbase-with-the-greennode-cli) composes identity + (memory) + runtime under one **name** from a manifest:
- `grn agentbase deploy generate` prints a template.
- `deploy up --file agent.yaml` is idempotent: it creates what's missing and waits for `ACTIVE`.
- `deploy status <name>` shows the state across services.
- `deploy destroy <name>` deletes the runtime and memory; `--purge` also deletes the identity and cannot be undone.

The scaffold ships `deploy/agent.yaml.tpl`, which the CI renders.

- **Install from GitHub Releases with a pinned version.** Asset names carry the version (`grn-linux-amd64-v1.13.0`), and the docs' `releases/latest/download/grn-linux-amd64` link returns 404. Verify the asset against `SHA256SUMS`.
- **Auth:**
  - CI: a service account via `GRN_ACCESS_KEY_ID` / `GRN_SECRET_ACCESS_KEY` (machine mode).
  - People: `grn login` (PKCE).
  - One profile per environment (`grn configure --profile staging`). `grn agentbase context current` shows which environment is active.
- **Manifest rules for this standard** (the docs' sample manifest differs; ours follows the other official pages):
  - `name` = agent / identity name (3–50 chars, `^[a-zA-Z0-9_-]+$`). This is the identity that must hold your providers.
  - `runtime.command: ["python", "main.py"]` (Dockerfile CMD); `runtime.env` = the full `.env.<env>` incl. `APP_ENV`.
  - Autoscaling thresholds must be within **25–75 %** (the docs' sample uses 80); replicas 1–10.
  - Memory: create it once with `/agentbase-memory` and pass `MEMORY_ID`/`MEMORY_STRATEGY_ID`. The template omits the `memory:` block. If you do use it, `namespaceTemplate` must be `/strategies/{memoryStrategyId}/actors/{actorId}` (the sample's `/strategies/USER_PREFERENCE/…` would not match `long_term.py`), and `eventExpiryDuration` is documented in days 1–365 on the Memory page vs `3600` in the CLI sample — check with `grn agentbase memory --help` before relying on it.
- **Failure behavior:**
  - `deploy up` does **not** roll back a partial failure; re-run it, or `destroy`.
  - `-o json` prints secrets in clear text, so never use it in CI logs.
- Not yet run end-to-end from this skill: verify the manifest once with `deploy up` on a dev environment.

## CI/CD (`.github/workflows/ci.yml`, from the scaffold)

| Job | When | Does |
|---|---|---|
| `test` | every push / PR | `uv sync --frozen`, ruff check + format check, pytest |
| `eval` | push to main / manual | `run_eval --concurrency 1` gate (MaaS 10 RPM); skipped with a notice until `EVAL_LLM_API_KEY` exists |
| `deploy` | manual (`workflow_dispatch`, input `deploy_env`) | pinned `grn` + SHA256 check → build `linux/amd64` → push to vCR → render manifest with `envsubst` from Environment secrets → `deploy up` → `deploy status` → `/health` smoke test → delete the rendered file |

- One **GitHub Environment** per `dev` / `staging` / `prod`, with its own secrets and vars (listed at the top of `ci.yml`). Require reviewers on `prod`.
- **Credentials:**
  - CI uses a **dedicated service account**, never a person's.
  - The documented policies are `AgentBaseFullAccess`, `vcrFullAccess` and `AiPlatformFullAccess`; narrower ones are not documented. See `/agentbase-build` `references/iam-permissions.md`.
  - Image pull/push uses a vCR robot account.
- **Not run on GitHub from this skill yet:** `ci.yml` and the template are validated as YAML only. Run the workflow once on a dev Environment and fix variables before relying on it.

## Operate the runtime

([manage-runtime](https://docs.greennode.ai/ai-stack/agent-base/agent-runtime/manage-runtime))

- **Versions:**
  - Every update creates an immutable **Version**.
  - The **DEFAULT** endpoint follows the latest version; the old one serves until the rollout completes.
  - Extra endpoints can be **pinned** to a version, e.g. `canary`.
- **Rollback:**
  - Point DEFAULT to an older version: `PATCH …/endpoints/{id}?version=N`, or via `/agentbase-deploy`.
  - Compare Langfuse traces by `release` (= `AGENT_VERSION`) to confirm recovery.
  - Check whether DEFAULT moves again on the next deploy before relying on the pin.
- **Stop / Start:** saves compute. A `STOPPED` runtime answers every request with an error until it's `ACTIVE` again (`STARTING` → `ACTIVE`).
- **Update via API:** the PATCH body must contain **every field except `imageAuth`**; a partial body is not accepted.
- **Reset service account** (`…/reset-service-account`): regenerates `GREENNODE_CLIENT_ID/SECRET` and **restarts** the runtime. Use it when the SA was revoked or rotated. Policy principals (`iam:<sub>`) may change, so re-check them in the agent log.
- **Delete:** irreversible (versions, endpoints, logs). An identity that still has runtimes can't be deleted.
- **Autoscaling:** replicas 1–10, CPU/RAM thresholds 25–75 %. Remember `MEMORY_MAX_CONCURRENCY ≈ 10 / max replicas` (above).

## Cleanup after testing

Runtimes are billed continuously (a `STOPPED` runtime keeps its config without compute). Delete test runtimes, memory, API keys, connectors with `/agentbase-teardown` (or each platform skill; with the CLI `grn agentbase deploy destroy <name>`); revoke the agent's API keys (remove hashes from `AUTH_API_KEY_SHA256`) and revoke the **Orphaned** service account the deleted agent leaves behind (IAM).

## Official docs

- [create-runtime](https://docs.greennode.ai/ai-stack/agent-base/agent-runtime/create-runtime) — create a runtime: image, env, autoscaling (1–10 replicas), Security Settings
- [manage-runtime](https://docs.greennode.ai/ai-stack/agent-base/agent-runtime/manage-runtime) — update, stop/start, endpoints/versions, rollback
- [runtime-reference](https://docs.greennode.ai/ai-stack/agent-base/agent-runtime/runtime-reference) — PATCH requires every field except `imageAuth`; reset-service-account
- [container-registry](https://docs.greennode.ai/ai-stack/agent-base/container-registry) — vCR / AgentBase Container Registry
- [logs-and-metrics](https://docs.greennode.ai/ai-stack/agent-base/agent-runtime/logs-and-metrics) — logs and metrics after deploy
- [manage-agentbase-with-the-greennode-cli](https://docs.greennode.ai/ai-stack/agent-base/manage-agentbase-with-the-greennode-cli) — `grn agentbase …` CLI incl. `deploy up`
- [private-networking](https://docs.greennode.ai/ai-stack/agent-base/private-networking) — Private Runtime (VPC Peering, VPC/Subnet/Route CIDRs)
- [access-control](https://docs.greennode.ai/ai-stack/agent-base/access-control) — the identity a runtime is bound to
