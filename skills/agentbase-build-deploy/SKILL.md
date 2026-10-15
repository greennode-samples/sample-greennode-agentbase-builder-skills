---
name: agentbase-build-deploy
description: "Workflow for shipping an agentbase-build standard AI agent (src/backend, uv, Docker) to GreenNode AgentBase Runtime: pre-flight (test, eval, lint, checklist), per-environment env files (strict KEY=VALUE, make check-env), build linux/amd64 image with uv, push to AgentBase Container Registry and create/update the runtime via /agentbase-deploy or CI (grn), keep network/security settings across updates, verify health + Langfuse release + logs, rollback, teardown. Use when deploying/shipping/releasing an agent built to the standard, creating dev/staging/prod environments, updating versions, rolling back. Trigger: deploy standard agent, release agent, ship agent, push agent to runtime, update agent version, rollback agent, đưa agent lên runtime, cập nhật version agent. DO NOT use for the OpenClaw chatbot template (call /agentbase-deploy directly) or viewing logs (use /agentbase-monitor)."
---

# Deploy an AgentBase agent

This skill **prepares and verifies**; platform operations (CR, runtime, endpoint) are performed by **`/agentbase-deploy`** under that skill's HARD GATE, or by the scaffolded CI workflow. Run platform scripts with **cwd = `src/backend`** (they read `./.greennode.json` and `./.env`) — see `/agentbase-build` → *Credentials*.

## Step 1 — Pre-flight (all must PASS)

```bash
make lint && make test
make eval MIN=<threshold from spec>
cd src/backend && uv lock --check
```

- Run `/agentbase-build` → `references/checklist.md`; all **[B]** items must PASS.
- **Release = image tag.** `make docker-build TAG=<tag>` bakes `AGENT_VERSION=<tag>` into the image (`ARG`/`ENV` in the Dockerfile) and Langfuse shows it as `release`. Default `TAG` = git short SHA (CI does the same); use a semver tag (`TAG=1.4.0`) for named releases. A deploy env file must **not** set `AGENT_VERSION` (it would override the image value).

## Step 2 — Per-environment env file

The scaffold commits one **non-secret** config file per environment: `src/backend/.env.dev.example`, `.env.staging.example`, `.env.prod.example` (reviewed in PRs; required secrets are `<set-in-deploy>`, optional ones empty). CI merges it with the secret store; for a manual `/agentbase-deploy`, the user runs **`make set-secret ENV_FILE=src/backend/.env.<env>`** in their own terminal: it creates the git-ignored copy from the `.example` and asks for each `<set-in-deploy>` secret (Enter) or any optional one (`NAME=langfuse`…) with hidden input, checked against the format below — never through the chat (the coding agent cannot type into that prompt; `/agentbase-build` → *Credentials*). Non-secret values stay as committed in the `.example`. Note `env_ignore_empty=True`: an empty value = the default in `config.py`.

**Format — the platform reads the file literally** (`runtime.sh --env-file` and `docker run --env-file`; python-dotenv, used for the local `.env`, is more lenient): one `KEY=VALUE` per line, **no quotes** (`AUTH_AUDIENCE="api://agent"` keeps the quotes ⇒ every JWT is rejected), **no inline comments**, **no `export`**, no `${VAR}`. Lint before every deploy: `make check-env ENV_FILE=src/backend/.env.<env>` (also rejects `GREENNODE_*`, duplicates, leftover `<set-in-deploy>`; prints key names only).

| Variable | dev | prod |
|---|---|---|
| `APP_ENV` | `dev` | `prod` — **required**: `local` is refused on the Runtime |
| `MEMORY_BACKEND` / `MEMORY_ID` / `MEMORY_STRATEGY_ID` | `agentbase` / dev memory | `agentbase` / prod memory (separate) |
| `AUTH_MODE` | `jwt` | `jwt` + `AUTH_JWKS_URL`, `AUTH_ISSUER`, `AUTH_AUDIENCE` (or `AUTH_ALLOW_NO_AUDIENCE=true` + `AUTH_ALLOWED_CLIENT_IDS`) |
| `LLM_*` | dev key, tiers, fallbacks | prod key (own Rate Limit), same tiers as evaluated |
| `LANGFUSE_*` | dev project/env | prod project, `LANGFUSE_SAMPLE_RATE` per load |
| `MCP_GATEWAY_URL`, `HITL_TOOLS`, `MEMORY_MAX_CONCURRENCY` | per environment (separate dev/prod gateways) | `≈ 10 / max replicas` |
| `APP_TIMEZONE`, `LLM_TASK_MAX_TOKENS`, `LTM_MIN_SCORE`, `LTM_FACT_MAX_CHARS`, `REFLECTION_TIMEOUT_S` | as evaluated (empty = app default) | same as the eval config |

**Which secrets may be in the runtime env file** (rendered from the CI secret store, or the user's git-ignored `.env.<env>`): `LLM_API_KEY`, `LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY`, `FEEDBACK_SIGNING_KEY`, static keys the agent presents to its own MCP servers / A2A peers (`${VAR}` in `mcp_servers.json` / `a2a_agents.json`). **Never** there: credentials tools use to call external services (SaaS API keys, OAuth tokens) — those live in AgentBase Identity (`/agentbase-build-identity`); MCP Gateway outbound credentials live in Access Control. Never `GREENNODE_CLIENT_ID/SECRET/AGENT_IDENTITY` (injected) or `GREENNODE_ENDPOINT_URL` (reserved; set `A2A_PUBLIC_URL`). Remind the user to review the env file themselves; **do not** print secret-bearing files into chat.

## Step 3 — Build the image

```bash
make docker-build IMAGE=<registry>/<repo>/<project> TAG=<tag>        # AGENT_VERSION=<tag> baked in
make docker-run IMAGE=... TAG=...   # local smoke test: curl :8080/health
make check-env ENV_FILE=src/backend/.env.prod && make docker-run IMAGE=... TAG=... ENV_FILE=src/backend/.env.prod
#   boots with the DEPLOY config and GREENNODE_AGENT_IDENTITY set (like the Runtime): a missing APP_ENV / AUTH_* fails here
```

Standard Dockerfile (`src/backend/Dockerfile`): multi-stage `python:3.13-slim` — uv only in the builder (`uv sync --locked --no-dev`), the runtime stage gets the venv + code owned by root and runs as non-root `agent`, `EXPOSE 8080`; `.dockerignore` patterns are `**/`-prefixed and exclude tests, evals, scripts and every secret file. Build with `--platform linux/amd64` (mandatory on ARM Macs).

## Step 4 — Push & create/update the runtime

**Manual path — `/agentbase-deploy`.** The image is already built by `make docker-build`, so tell it to **skip its Step 1a (root `Dockerfile` check — never create a root Dockerfile; ours is `src/backend/Dockerfile`) and Step 2 (build)**: only push (Step 3: `cr.sh credentials docker-login`, `docker push`) and create/update (Step 4, prefer `--from-cr` for the AgentBase CR), with `--env-file .env.<env>` (cwd `src/backend`), the user-chosen flavor/autoscaling/wallet, and:

- **Agent identity — one per environment** (`<agent>-dev`, `<agent>-staging`, `<agent>-prod`; each runtime gets its own service account). Never share an identity between environments and never put the prod identity in a developer's `.greennode.json`: any dev or `api_key` caller could then fetch another environment's per-user 3LO tokens / delegated keys. Bind each runtime to its environment's identity, holding that environment's providers (`/agentbase-build-identity`); otherwise `@agent_api_key` / `@user_access_token` return 404 or "can't retrieve credential". The Runtime injects it as `GREENNODE_AGENT_IDENTITY`. Per-user helpers refuse unless `AUTH_MODE=jwt` (`IDENTITY_TRUST_ASSERTED_USER=false`, the default).
  - With per-user credentials, `IDENTITY_CALLBACK_URL` in `.env.<env>` must be this environment's page and must be listed in the identity's `allowedReturnUrls`.
- **Network mode** — `PUBLIC` by default. `VPC` (VPC + Subnet + optional Route CIDRs; needs VPC Peering and a flavor that supports `agent-runtime-vpc`) only when agent code must reach an internal API or a self-hosted service; internal MCP servers go through a Private MCP Gateway instead — see `/agentbase-build` `references/private-networking.md`.
- **Security Settings** ([create-runtime](https://docs.greennode.ai/ai-stack/agent-base/agent-runtime/create-runtime)):
  - **Inbound Auth type** — `JWT` (same IdP as `AUTH_*`; lets the app call directly), `IAM Permissions` (server callers / BFF only), or `No authorization` (public — the agent still verifies tokens itself). Prefer JWT or IAM for production.
  - **IP Access Control** (`allowedCidrs`) — allowed source CIDRs for server-to-server callers with fixed egress (BFF, other agents).
  - With Runtime JWT on `Authorization`, keep the agent's `AUTH_TOKEN_HEADER=Authorization` (same token); with Runtime IAM, the BFF sends the IAM token in `Authorization` and the user JWT in `AUTH_TOKEN_HEADER=X-GreenNode-AgentBase-Custom-User-Token` (`/agentbase-build-auth`).

**Every update is a full replacement — re-send the network and security settings.** Before `runtime.sh update`, read the latest version without printing its env (it holds secrets; > 100 versions ⇒ also read `--page 2…` up to `totalPage`):

```bash
bash <platform-scripts>/runtime.sh versions $RUNTIME_ID --size 100 | jq '.listData | max_by(.version)
  | {version, imageUrl, flavorId, network: .networkConfig, allowedCidrs, inbound: .inboundAuth.mode}'
```

- `networkConfig.mode` = `VPC` ⇒ **re-pass** `--network-mode VPC --vpc-id … --subnet-id … --route-cidrs …`: an update without `networkConfig` makes the server default the new version to **PUBLIC** (endpoint recreated, VPC reachability lost). Switching mode on purpose also recreates the endpoint (brief downtime).
- `allowedCidrs` non-empty or Inbound Auth ≠ none ⇒ `runtime.sh` has no flags for them (nor has `grn`): update in the **Console** (Security Settings are part of the form), or keep this environment out of CI.
- After the update, run the same command and compare with the previous version: same network/security settings, `imageUrl` = the new image. Any drift ⇒ fix it in the Console immediately.

Record per environment in `.agentbase-state.json` → `environments.<env>`: `runtime_id`, `endpoint`, `image`, `network_mode`, `allowed_cidrs`, `inbound_auth`, `identity`, `memory_id`, `memory_strategy_id`, `llm_key_name`, `mcp_gateway`, `policy_group`, `principal` (the flat top-level keys stay for the platform wizard).

**CI path** — see *CI/CD* below. It handles Public runtimes without Inbound Auth / IP Access Control only, and refuses the others.

## Run for real (2026-10, runtime `test-agent`, flavor `runtime-s2-general-2x4`)

Push CR → create runtime → ACTIVE in ~40s; version update ~30s. Checks used: health 200 · no key/wrong key ⇒ 401 · chat · short-term (AgentBaseMemoryEvents) · long-term across sessions · user isolation · stream · MCP via gateway (controlled policy deny). Caught 2 memory SDK bugs that only surface on real memory (see `/agentbase-build-memory`) ⇒ **always smoke test on the runtime after deploy**.

## Step 5 — Verify

1. `curl <endpoint>/health` → 200.
2. Call `/invocations` with a real JWT (or via BFF) → `status: success`.
3. **Which version runs:** `grn agentbase runtime versions <id>` / `runtime.sh versions <id>` (latest `imageUrl` = your tag) and Langfuse traces with `environment=<env>`, `release=<tag>`, standard tree, cost present. (`grn agentbase deploy status -o json` returns `[{}]` — not usable for this.)
4. Network/security settings unchanged versus the previous version (Step 4).
5. `/agentbase-monitor`: no ERROR in logs, CPU/RAM healthy.
6. **MCP through the gateway:** the Policy Group must allow this environment's **runtime** principal (not your developer principal) — `/agentbase-build` Step 9b: read `IAM principal … iam:<sub>` from the runtime logs (`runtime.sh logs <id> --query "IAM principal"`; logged at startup by `log_iam_principal()` — only when an enabled `auth: "iam"` MCP server is configured; locally `make check-creds` shows the developer principal instead), add it with `/agentbase-policy`, re-test a tool call.

`make eval` runs the graph **in-process** (`APP_ENV=local`, in-memory backends) — it does not exercise the deployed runtime. Verify a deployment with real requests + traces; an HTTP mode for `run_eval` against an endpoint does not exist yet.

## Scale (multiple replicas)

- Memory: limit of 10 concurrent requests per IAM account, shared across all replicas ⇒ `MEMORY_MAX_CONCURRENCY ≈ 10 / max replicas`.
- A2A: `InMemoryTaskStore` is not shared across replicas ⇒ use `DatabaseTaskStore` when `max_replicas > 1`.
- In-process caches (MCP tool list, negative cache, JWKS) are per-replica — acceptable.

## Alternative: GreenNode CLI (`grn agentbase deploy`)

[`grn`](https://docs.greennode.ai/ai-stack/agent-base/manage-agentbase-with-the-greennode-cli) composes identity + (memory) + runtime under one **name** from a manifest:
- `grn agentbase deploy generate` prints a template.
- `deploy up --file agent.yaml` is idempotent: it creates what's missing and waits for `ACTIVE`. **It never updates an existing runtime** ("converging, not re-applying" — grn source): a new image or env would silently not ship. Update with `grn agentbase runtime update <id> --file <spec>` (FULL-spec replacement: new version, DEFAULT endpoint rolls forward; `imageAuth` must be explicit — `auto` is create-only) then `grn agentbase runtime wait <id>`.
- **grn v1.13.0 cannot carry** `networkConfig`, `allowedCidrs`, `inboundAuth` or the wallet (`poc`): neither the manifest nor `UpdateAgentRuntimeRequest` has those fields. `deploy up` creates a Public runtime with "No authorization" on the server-default wallet; a `runtime update` drops whatever the server does not keep (omitted `networkConfig` ⇒ PUBLIC). Use grn only for runtimes without those settings.
- `deploy status <name>` shows the state across services.
- `deploy destroy <name>` deletes the runtime and memory; `--purge` also deletes the identity and cannot be undone.

The scaffold ships `deploy/agent.yaml.tpl` (rendered by CI), `deploy/render_runtime_spec.py` (merges `.env.<env>.example` + CI secrets into the manifest and derives the `runtime update` spec — one source of truth; pull credentials from the vCR robot account) and `deploy/env_file.py` (the strict env-file parser behind `make check-env`). Both rendered files contain secrets — temp dir only, never committed (`.gitignore` covers `deploy/agent.yaml`, `deploy/runtime.yaml`).

- **Install from GitHub Releases with a pinned version.** Asset names carry the version (`grn-linux-amd64-v1.13.0`), and the docs' `releases/latest/download/grn-linux-amd64` link returns 404. Verify the asset against `SHA256SUMS`.
- **Auth:**
  - CI: a service account via `GRN_ACCESS_KEY_ID` / `GRN_SECRET_ACCESS_KEY` (machine mode).
  - People: `grn login` (PKCE).
  - One profile per environment (`grn configure --profile staging`). `grn agentbase context current` shows which environment is active.
- **Manifest rules for this standard** (the docs' sample manifest differs; ours follows the other official pages):
  - `name` = identity + runtime name **per environment**: `<AGENT_NAME>-<env>` (3–50 chars, `^[a-zA-Z0-9_-]+$`; CI sets it). This is the identity that must hold that environment's providers — never one identity for several environments.
  - `runtime.command: ["python", "main.py"]` (Dockerfile CMD); `runtime.env` = `APP_ENV` + `AGENT_VERSION` in the template, completed from `.env.<env>.example` and the CI secrets by `render_runtime_spec.py`.
  - Autoscaling thresholds must be within **25–75 %** (the docs' sample uses 80); replicas 1–10.
  - Memory: create it once with `/agentbase-memory` and set `MEMORY_ID`/`MEMORY_STRATEGY_ID` in `.env.<env>.example`. The template omits the `memory:` block. If you do use it, `namespaceTemplate` must be `/strategies/{memoryStrategyId}/actors/{actorId}` (the sample's `/strategies/USER_PREFERENCE/…` would not match `long_term.py`), and `eventExpiryDuration` is documented in days 1–365 on the Memory page vs `3600` in the CLI sample — check with `grn agentbase memory --help` before relying on it.
- **Failure behavior:**
  - `deploy up` does **not** roll back a partial failure; re-run it, or `destroy`.
  - `-o json` prints secrets in clear text (version rows include `environmentVariables`), so never print it in CI logs — project the fields you need with `jq`.
- Not yet run end-to-end from this skill: verify the manifest once with `deploy up` on a dev environment.

## CI/CD (`.github/workflows/ci.yml`, from the scaffold)

| Job | When | Does |
|---|---|---|
| `test` | every push / PR | `uv sync --locked` (fails if `uv.lock` is stale), ruff check + format check, pytest, `env_file.py check` on every `.env.*.example` |
| `eval` | push to main / manual | `run_eval --concurrency 1` gate (MaaS 10 RPM) with **repo-level** `EVAL_LLM_API_KEY`; the LLM tiers/fallbacks/routing/token caps, reflection, LTM and `APP_TIMEZONE` come from `src/backend/.env.<EVAL_CONFIG_ENV>.example` (default `prod`), `EVAL_LLM_MODEL` optionally overrides `LLM_MODEL`; without key/model the gate is skipped with a notice. Runs in-process with `APP_ENV=local`: MCP servers filtered to deployed environments and `user_jwt` servers are not loaded — keep eval items that need them in a separate dataset |
| `deploy` | manual (`workflow_dispatch`: `deploy_env` = dev / staging / prod, optional `image_tag`) | refuses staging/prod if the eval gate didn't run → pinned `grn` + SHA256 check → build `linux/amd64` with `--build-arg AGENT_VERSION=<short SHA>` and push (skipped when `image_tag` is set: redeploy of an existing image) → render manifest + runtime spec (`.env.<env>.example` + secrets) → find the runtime across **all** list pages → **first run** `deploy up`; **later runs**: refuse if the latest version is VPC / has IP Access Control / Runtime Inbound Auth, else `runtime update` + `runtime wait`, then fail on any network/security drift versus the previous version → check the latest version runs the new image → `/health` smoke test → delete the rendered files |

- **Variables:** environment-scoped secrets/vars are only visible to jobs that declare `environment:` — so eval values are repo-level and deploy values per GitHub Environment (`dev` / `staging` / `prod`, listed at the top of `ci.yml`). **The user sets the secrets in their own terminal** with the GitHub CLI, which prompts without echo: `gh secret set LLM_API_KEY --env prod`, repo-level `gh secret set EVAL_LLM_API_KEY`; non-secret vars with `gh variable set`. The multi-line `RUNTIME_ENV_SECRETS`: write its `KEY=VALUE` lines with `make set-secret ENV_FILE=src/backend/.env.<env>.runtime NAME=<KEY>` (a new git-ignored file), then `gh secret set RUNTIME_ENV_SECRETS --env <env> < src/backend/.env.<env>.runtime` and delete the file. Never paste a secret into the chat to have the agent run `gh secret set --body …`. Non-secret config is **committed** (`.env.<env>.example`), not copied into CI variables: the deploy env cannot drift from what the PR reviewed. Extra runtime secrets (static MCP / A2A keys) go in the multi-line secret `RUNTIME_ENV_SECRETS`. A key set both in the file and in a secret fails the render (catches committed secrets). Require reviewers on `prod`.
- **Concurrency:** superseded push/PR runs are cancelled; a manual run has its own workflow group (never cancelled by a push or another dispatch), and deploys to one environment are serialized by the job group `deploy-<env>` (`cancel-in-progress: false`) — note GitHub keeps only the newest *pending* run per group, so a third queued deploy replaces a waiting second one. An interrupted `deploy up` is not rolled back.
- **Shell:** steps run with `bash -eo pipefail` (`defaults.run.shell: bash`) plus `inherit_errexit` in the deploy step, so a failing `grn … runtime list` fails the deploy instead of falling through to `deploy up` (which would succeed without shipping).
- **Credentials:**
  - CI uses a **dedicated service account**, never a person's.
  - Least privilege: the managed policies (`AgentBaseFullAccess`, `vcrFullAccess`, `AiPlatformFullAccess`) are broad; a custom IAM policy with only the actions CI needs can be built in the IAM Console (*Policies* → select actions — platform `/agentbase`, IAM setup). Validate it with one dev deploy. See `/agentbase-build` `references/iam-permissions.md`.
  - Image pull/push uses a vCR robot account.
- **What the smoke test proves:** `/health` only shows the runtime answers — not which version. The deploy step already checks the latest version's `imageUrl`; Langfuse `release` (= tag) confirms what serves traffic.
- **Not run on GitHub from this skill yet:** the deploy logic was exercised against a mock `grn` (pagination, VPC/CIDR/inbound refusal, drift, first create, failing list) and the templates are rendered and linted offline; the `grn` flags and JSON fields come from grn v1.13.0 docs and source. Run the workflow once on a dev Environment before relying on it.

## Operate the runtime

([manage-runtime](https://docs.greennode.ai/ai-stack/agent-base/agent-runtime/manage-runtime))

- **Versions:**
  - Every update creates an immutable **Version**.
  - The **DEFAULT** endpoint follows the latest version; the old one serves until the rollout completes.
  - Extra endpoints can be **pinned** to a version, e.g. `canary`.
- **Rollback = an update with the previous image + config** (new version; DEFAULT follows it):
  - CI: run the workflow with `image_tag=<previous tag>` (no rebuild); dispatch it from the previous release's ref (tag/branch) to roll back `.env.<env>.example` too.
  - Manual: `runtime.sh versions <id>` → previous `imageUrl`/`flavorId` → `/agentbase-deploy` update with that image, the matching `.env.<env>` and the same network flags.
  - Optional check first: a custom endpoint pinned to the old version (`endpoints create --name rollback-test --version N`), then delete it.
  - The docs describe pointing DEFAULT at an older version (`PATCH …/endpoints/{id}?version=N`); the platform skill reports **400** for the DEFAULT endpoint — don't rely on it.
  - Confirm with the latest version's `imageUrl` and Langfuse `release`.
- **Stop / Start:** saves compute. A `STOPPED` runtime answers every request with an error until it's `ACTIVE` again (`STARTING` → `ACTIVE`).
- **Update via API:** the PATCH body must contain **every field except `imageAuth`** (and `networkConfig`, which then defaults to PUBLIC); a partial body is not accepted.
- **Reset service account** (`…/reset-service-account`): regenerates `GREENNODE_CLIENT_ID/SECRET` and **restarts** the runtime. Use it when the SA was revoked or rotated. Policy principals (`iam:<sub>`) may change, so re-read the principal from the logs and update the Policy Group.
- **Delete:** irreversible (versions, endpoints, logs). An identity that still has runtimes can't be deleted.
- **Autoscaling:** replicas 1–10, CPU/RAM thresholds 25–75 %. Remember `MEMORY_MAX_CONCURRENCY ≈ 10 / max replicas` (above).

## Teardown checklist

Runtimes are billed continuously (a `STOPPED` runtime keeps its config without compute). `/agentbase-teardown` (or `grn agentbase deploy destroy <name>`) removes runtime, identity/providers, memory, registry images and API keys — **not** the rest. Go through every item per environment (`.agentbase-state.json` → `environments.<env>` lists the IDs), confirming each deletion with the user:

1. Runtime(s) incl. custom endpoints — `/agentbase-teardown` or `/agentbase-deploy`.
2. Memory store(s), identity + Access Control providers, LLM API keys, vCR images — `/agentbase-teardown`.
3. MCP Gateways created for this agent — `/agentbase-gateway` (each gateway also leaves an `sa-gateway-<id>` service account).
4. Policy Groups — `/agentbase-policy` (detach from the gateway first).
5. MCP Connectors (catalog/custom) — Console.
6. The **Orphaned** runtime service account (`auto-sa-<agent>`) and gateway service accounts — revoke in IAM.
7. Inbound API keys: remove their hashes from `AUTH_API_KEY_SHA256` of the callers' environments, and the A2A keys peers hold for this agent.
8. CI: GitHub Environment secrets, the CI service account / vCR robot account if dedicated to this agent.
9. Langfuse project data (traces, datasets) per your retention policy.
10. Clear the environment's entries in `.agentbase-state.json`.

## Official docs

- [create-runtime](https://docs.greennode.ai/ai-stack/agent-base/agent-runtime/create-runtime) — create a runtime: image, env, autoscaling (1–10 replicas), Security Settings
- [manage-runtime](https://docs.greennode.ai/ai-stack/agent-base/agent-runtime/manage-runtime) — update, stop/start, endpoints/versions, rollback
- [runtime-reference](https://docs.greennode.ai/ai-stack/agent-base/agent-runtime/runtime-reference) — PATCH requires every field except `imageAuth`; reset-service-account
- [container-registry](https://docs.greennode.ai/ai-stack/agent-base/container-registry) — vCR / AgentBase Container Registry
- [logs-and-metrics](https://docs.greennode.ai/ai-stack/agent-base/agent-runtime/logs-and-metrics) — logs and metrics after deploy
- [manage-agentbase-with-the-greennode-cli](https://docs.greennode.ai/ai-stack/agent-base/manage-agentbase-with-the-greennode-cli) — `grn agentbase …` CLI incl. `deploy up`
- [private-networking](https://docs.greennode.ai/ai-stack/agent-base/private-networking) — Private Runtime (VPC Peering, VPC/Subnet/Route CIDRs)
- [access-control](https://docs.greennode.ai/ai-stack/agent-base/access-control) — the identity a runtime is bound to
