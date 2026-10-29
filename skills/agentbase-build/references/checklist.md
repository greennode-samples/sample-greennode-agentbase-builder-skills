# Definition of Done / Review checklist

Run each item and report `PASS` / `FAIL` / `N/A` with evidence (command + short output). Items marked **[B]** = BLOCKER, do not deploy if FAIL.

## Structure & env
- [B] `src/backend/pyproject.toml` + `uv.lock` + `.python-version` (3.13) exist; `requires-python = ">=3.13,<3.14"`; no `requirements.txt`; `uv sync --locked` works (lock matches `pyproject.toml`); `uv run python --version` prints 3.13.x.
- [B] `app/` structure follows the standard; no second graph/LLM factory (`grep -rn "ChatOpenAI(" app | grep -v app/llm` is empty).
- [B] No `os.environ`/`os.getenv` reads outside `app/config.py` and the allowed exceptions (HARD RULE 2: `app/prompts`, `app/observability`, the `${VAR}` expansion in `app/tools/mcp.py` / `app/a2a/client.py`). Must print nothing: `grep -rn "os.getenv\|os.environ" app | grep -v -e '^app/config.py:' -e '^app/prompts/' -e '^app/observability/' -e '^app/tools/mcp.py:.*environ.get(\(m.group(1)\|v\))' -e '^app/a2a/client.py:.*environ.get(m.group(1))'` (mcp.py also reads the `${VAR}` values to know when to re-parse its cached config).
- [B] `.env`, `.greennode.json` are in `.gitignore` and `.dockerignore`.
- [B] `make check-creds` prints OK for IAM and LLM — the user entered the secrets themselves (`make set-secret` in their own terminal; never pasted into chat or printed); any secret that did reach a chat, commit or log has been rotated (`references/security.md` → *Secret pasted into a chat*).
- Every variable in `Settings` is in `.env.example`.

## Runtime contract
- [B] `GET /health` → 200; listens on `0.0.0.0:8080`.
- [B] `make test` green; `make lint` clean.
- Image built with `--platform linux/amd64`, runs as non-root.

## LLM
- [B] LLMs are created only via `get_llm(task)` (`app/llm`; the `ChatOpenAI(` grep above is empty); `LLM_MODEL` is the model's `path` on AIP. New flows are in `DEFAULT_TASK_TIERS`, new LLM-calling modules in `tests/conftest.py` `LLM_MODULES`.
- Prod: the API key lives in the runtime's env file (rendered from the CI secret store), not in the image.
- Tiers: `summarize`/`router`/`judge` use the small tier (the default when `LLM_MODEL_SMALL` is set); the `large` tier has **at least 1 fallback** from a different model family for prod; every tier's model has been run through `make eval`.

## Memory
- [B] `MEMORY_BACKEND=agentbase` + `MEMORY_ID` in staging/prod (dev may use `inmemory` only with 1 replica — history is lost on restart).
- [B] actor_id comes from `Principal`, not a tool parameter; namespace matches the memory store's `namespaceTemplate`.
- Compression thresholds tuned to the model's context window; long-conversation test passes.

## Tools / MCP
- [B] Side-effect tools are in `HITL_TOOLS` (or the user confirmed in writing that it is not needed).
- [B] Each local side-effect tool passes `idempotency_key(config, tool_call_id)` to the system it changes (or that system dedupes on a business key) — HITL alone does not stop a double run across replicas (`/agentbase-build-hitl`).
- Prod MCP goes through the MCP Gateway, inbound `IAM` or `JWT`; secrets live in Identity (Secret Provider).
- [B] The Gateway has a **Policy Group** attached (none attached ⇒ every tools/call returns 403); there is an ALLOW policy with the exact action `<connector>__<tool>` for each principal that calls it: the developer's `iam:<sub>` (`make check-creds`, local runs) and **each environment's runtime** `iam:<sub>` (runtime logs `IAM principal …`, logged at startup by `log_iam_principal()` — only when an enabled `auth: "iam"` MCP server is configured — `/agentbase-build` Step 9b). Dev and prod use separate gateways/groups (≤ 10 policies per group).
- [B] `mcp_servers.json` points to **each connector's connectUrl** (`<gateway endpoint>/<connector>`), not the gateway root, and every entry declares `auth` explicitly.
- [B] `HITL_TOOLS` patterns all match a tool (`tools.collect.output.hitl_unmatched` is empty).
- MCP servers in a VPC / data center go through a **Private** MCP Gateway (`references/private-networking.md`); they still authenticate every call.
- The main flow produces no `mcp.policy_denied` in traces.
- No duplicate tool names; a failing MCP server does not crash the agent (trace shows `mcp.list_tools` WARNING).

## Outbound credentials (if tools call external services) — `/agentbase-build-identity`
- [B] Keys/tokens for external services come from Access Control via `app/identity.py` helpers on an **inner** function; none of them in env files, code, tool parameters or traces (env files hold only the agent's own secrets — HARD RULE 5).
- [B] Per-user credentials (3LO / delegated): `IDENTITY_CALLBACK_URL` is set per environment and listed in the identity's `allowedReturnUrls`; the tool returns the consent link (`AUTHORIZATION_REQUIRED`) instead of blocking.
- [B] `make check-creds` shows `OK  Agent identity` locally; the runtime is bound to the identity that holds the providers.

## Auth
- [B] Outside local: `AUTH_MODE=jwt` (issuer/audience or client allowlist configured; expired/wrong-audience token ⇒ 401), or `api_key` for trusted server callers only (BFF, jobs, A2A) with the reason written in the spec.
- [B] Spoofed `X-GreenNode-AgentBase-User-Id` header ⇒ 403.

## Tracing (Langfuse v4)
- [B] One request creates exactly 1 `agent.invoke` trace with user_id, session_id, tags, version.
- Complete trace tree: `auth`, `tools.collect`, `mcp.list_tools`, `compress/context.compress`, `recall/memory.recall` (retriever), generations `llm.<task>.generate` (model attribute, usage, cost, thinking when the model reasons), tool observations, `hitl.*` when present.
- Root `agent.invoke` input = user message, output = answer text; the only `agent` observations are the root and `a2a.<agent>`/`subagent.*`; no observation is named after a model or a per-request value.
- [B] `lf_trace.py --latest 3 --audit` (skill `agentbase-build-tracing`) reports no FAIL on real requests; WARNs are fixed or explained.
- Generations have cost (GreenNode model prices declared in Langfuse) and `input_cache_read` if the provider returns it.
- No tokens/API keys in traces (check 1 real trace).

## HITL
- Interrupt → approve/edit/reject works; chatting while approval is pending ⇒ 409.

## Evaluation
- [B] `evals/datasets/*.jsonl` covers the spec's main flows (≥10 items for prod).
- [B] `make eval` meets the agreed `--min-pass-rate`.
- 👍/👎 feedback from the frontend records a `user_feedback` score.

## Self-built MCP server (if any)
- `/health` is public; `/mcp` without a token ⇒ 401 (`WWW-Authenticate` includes `resource_metadata`).
- Per-user data tools use `current_user()` from the token; there is a 2-user isolation test.
- Root keys / OAuth clients live in Identity; env only holds hashes / public info.

## A2A (if enabled — with a reason per decision guide §5)
- Agent Card has the correct URL, securitySchemes match `AUTH_MODE`; `POST /a2a` without auth ⇒ 401.
- User B cannot GetTask user A's task (test `test_tasks_isolated_per_user`).
- Client: a separate key per target agent; `ask_*` tools pass the user; tools that can take actions are in `HITL_TOOLS`.
- Timeouts are nested (no startup WARNING about them): this agent's `A2A_TIMEOUT_S` ≥ the target's `REQUEST_TIMEOUT_S` + margin, and its own `REQUEST_TIMEOUT_S` > `A2A_TIMEOUT_S` + the LLM budget of the turn.

## Frontend (if any)
- No secrets in `EXPO_PUBLIC_*`; OIDC uses PKCE; tokens in SecureStore.
- All SSE events handled (`token`, `reset`, `interrupt`, `done`, `error`); the client adopts the `session_id` returned in `done` / `interrupt` / the response (session rotation).

## Deploy
- [B] `make check-env ENV_FILE=src/backend/.env.<env>` (or the `.example` CI uses) passes: strict KEY=VALUE, no quotes / inline comments / `export`, no `GREENNODE_*` (injected / reserved), no `AGENT_VERSION` (= image tag).
- [B] The deploy env file sets `APP_ENV` (dev/staging/prod) — `local` is refused on the Runtime.
- [B] `GET <endpoint>/ready` answers 200 (the CI smoke test does this; 503 names `jwks`: the IdP's JWKS cannot be fetched). Logs are JSON lines with `request_id` / `trace_id` and one `request` line per request.
- After deploy: health OK, the latest version's `imageUrl` is the new tag, 1 real request has a trace in Langfuse with `release` = that tag, clean logs (`/agentbase-monitor`).
- [B] After every update the new version keeps the previous network mode (VPC: same VPC/subnet/route CIDRs), IP Access Control and Inbound Auth (`/agentbase-build-deploy` Step 4).
- [B] `.agentbase-state.json` → `environments.<env>` records `runtime_id`, `endpoint`, `image`, `network_mode`, `allowed_cidrs`, `inbound_auth`, `identity`, `memory_id`, `memory_strategy_id`, `llm_key_name`, `mcp_gateway`, `policy_group`, `principal`.
- The agent's LLM API key is dedicated to this agent + environment and has a Protect & Govern **Rate Limit** (requests and tokens per day).
- Request limits sized for the spec's load and replica count (`RATE_LIMIT_USER_PER_MIN`, `RATE_LIMIT_TENANT_PER_MIN`, `MAX_CONCURRENT_REQUESTS`, per replica); a strict global quota (paid tiers, per-tenant contracts) is enforced in front of the runtime.
- [B] Staging/prod run an image built once and promoted: the CI job summary shows the same digest per tag in every environment; rollbacks use `image_tag` from a release tag.
- CI deploys with a dedicated service account (never a person's credentials); secrets live in the CI secret store; the rendered manifest is not committed (`references/iam-permissions.md`).
- Teardown follows the checklist in `/agentbase-build-deploy` (*Teardown checklist*): also gateways, Policy Groups, connectors, the **Orphaned** service account and inbound API key hashes — `/agentbase-teardown` alone does not remove them.

## Security — `make security-scan` (`/agentbase-build-security-audit`; the checks live in the scan, not here)
- [B] `make security-scan` exits 0: no blocking check FAILs (CI runs it too); each WARN fixed or explained; each MANUAL line done with the skill it names and reported.
- Prod: a full audit (`security-audit` skill, seeded with the AgentBase threat model) has run on the deployed `source_ref`, or a scoped run covers every change since; its result is recorded in `.agentbase-state.json` → `security_audit` (S13 blocks while findings are open).
