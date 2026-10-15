---
name: agentbase-build
description: "Prefer this skill (over /agentbase-wizard) when a standardized architecture is needed. Standard + process for a coding agent to BUILD a production-ready AI agent on GreenNode AgentBase (Python + uv + LangGraph, src/backend + optional React Native src/frontend). Use when the user wants to build/create/design an agent, chatbot, assistant or copilot for a new business requirement, or to review an existing agent against the standard. Trigger: build agent, create an agent for ..., design agent, standard agent, agentbase build, review agent, check agent standard, upgrade project, tạo agent cho ..., thiết kế agent, agent chuẩn, kiểm tra chuẩn agent, nâng cấp project. ORCHESTRATES the agentbase-build-* skills (scaffold, llm, memory, tracing, mcp, mcp-server, auth, identity, hitl, eval, a2a, frontend, deploy) and the platform agentbase-* skills. DO NOT use for platform reference questions (use /agentbase) or single operations on an existing resource (use the platform skill)."
---

# AgentBase Build — Standardizing how AI agents are built on GreenNode AgentBase

Goal: **every agent, whatever its business requirements, has the same structure, the same way of getting the LLM, remembering, tracing, connecting tools, and authenticating**. What differs between projects is only: the system prompt, tools/MCP servers, business nodes in the graph, and the evaluation dataset.

## Standard stack (DO NOT change on your own)

| Area | Standard | Owning skill |
|---|---|---|
| Language / env | **Python 3.13** (`.python-version`, `requires-python >=3.13,<3.14`, Docker `python:3.13-slim`) + **uv** (`pyproject.toml` + `uv.lock`, every command via `uv run`, no `pip`/`requirements.txt`) | `agentbase-build-scaffold` |
| Runtime | `greennode-agentbase` `GreenNodeAgentBaseApp` — port 8080, `GET /health`, `POST /invocations` | `agentbase-build-scaffold` |
| Framework | LangGraph (`StateGraph`) + LangChain core | `agentbase-build-scaffold` |
| LLM | GreenNode AI Platform MaaS (OpenAI-compatible) via `ChatOpenAI` | `agentbase-build-llm` |
| Short-term memory | `AgentBaseMemoryEvents` (checkpointer) | `agentbase-build-memory` |
| Long-term memory | AgentBase Memory records: auto-recall node + tools `remember`/`recall_memory` | `agentbase-build-memory` |
| Context compression | Rolling summary + hard trim | `agentbase-build-memory` |
| Tracing | **Langfuse Python SDK v4** — only via `app/observability/tracing.py`: `tracing.langchain_callbacks()` (typed `CallbackHandler` subclass), `step()` / `event()`, audited with `lf_trace.py --audit` | `agentbase-build-tracing` |
| External tools | **MCP Connectors** (catalog/custom) on the **MCP Gateway** + **Policy Group** (`langchain-mcp-adapters`, one `connectUrl` per connector) | `agentbase-build-mcp` |
| Inbound auth | JWT (OIDC/JWKS) — user_id = claim `sub` | `agentbase-build-auth` |
| Outbound credentials | AgentBase Identity / Access Control — Static & Delegated API key, OAuth2 M2M & 3LO (`app/identity.py`) | `agentbase-build-identity` |
| Human-in-the-loop | LangGraph `interrupt()` to approve dangerous tools | `agentbase-build-hitl` |
| Evaluation | Langfuse Experiments (offline) + scores (online) + self-eval loop | `agentbase-build-eval` |
| Build MCP server | FastMCP streamable HTTP + auth (API key / OAuth JWT, scopes, user from token) on Runtime | `agentbase-build-mcp-server` |
| Agent ↔ agent | **A2A** (a2a-sdk; server + client, per-user isolation) — **only when decision guide §5 is satisfied** | `agentbase-build-a2a` |
| UI | Expo React Native (TypeScript) in `src/frontend` — **only when a UI is needed** | `agentbase-build-frontend` |
| Deploy | Docker linux/amd64 → AgentBase CR → Agent Runtime | `agentbase-build-deploy` |

## Standard project structure

```
<project>/
├── Makefile                  # setup | check-creds | check-env | dev | test | eval | lint | docker-build | docker-run | invoke | fe-dev
├── README.md
├── .agentbase-state.json     # standard/scaffold version + platform resource IDs per environment (environments.<env>)
├── .agentbase-template.json  # owner class + template hash per generated file (upgrades: /agentbase-build-scaffold)
├── .github/workflows/ci.yml  # lint + test, eval gate, manual deploy / redeploy (grn)
├── deploy/                   # agent.yaml.tpl, render_runtime_spec.py, env_file.py (make check-env)
├── docs/agent-spec.md        # the confirmed Agent Spec (Step 1)
└── src/
    ├── backend/              # uv project — deployed as 1 AgentBase Runtime container
    │   ├── pyproject.toml / uv.lock / .python-version (3.13) / Dockerfile / .env.example
    │   ├── .env.{dev,staging,prod}.example   # committed non-secret runtime config per environment
    │   ├── mcp_servers.json / a2a_agents.json
    │   ├── main.py           # THIN entrypoint
    │   ├── scripts/check_creds.py   # make check-creds (OK/FAIL only)
    │   ├── app/
    │   │   ├── config.py     # Settings (pydantic-settings) — the ONLY env source
    │   │   ├── service.py    # payload contract + orchestration of 1 request
    │   │   ├── graph/        # state.py, builder.py — the ONLY graph
    │   │   ├── llm/          # get_llm(task) — tiers reasoning/large/small + fallback; routing.py
    │   │   ├── memory/       # short_term.py, long_term.py, compression.py
    │   │   ├── tools/        # __init__ (registry), local_tools.py, mcp.py
    │   │   ├── knowledge/    # .md/.txt for search_knowledge (tool auto-enabled when files exist)
    │   │   ├── auth/         # inbound.py (jwt | api_key | none-local), validate_user_id
    │   │   ├── a2a/          # server.py (Agent Card + /a2a), client.py (tool ask_<agent>) — off by default
    │   │   ├── identity.py   # outbound credentials (AgentBase Identity: API key / OAuth2, M2M + per-user)
    │   │   ├── hitl.py       # approval node
    │   │   ├── reflection.py # self-eval loop (optional)
    │   │   ├── observability/tracing.py   # Langfuse v4
    │   │   └── prompts/      # system.md + loader (Langfuse Prompt Management)
    │   ├── evals/            # datasets/*.jsonl, evaluators.py, run_eval.py
    │   └── tests/
    ├── frontend/             # (optional) Expo React Native
    └── mcp_servers/<name>/   # (optional) self-built MCP server — /agentbase-build-mcp-server
```

Per-module responsibilities and the request flow: read **`references/architecture.md`**.

## Credentials the user provides

| Value | Used for | How to get it | Where it goes (user fills it in their editor) |
|---|---|---|---|
| IAM service account `client_id` + `client_secret` | Memory, Identity, MCP Gateway (agent → platform) | `/agentbase` (IAM setup) | `src/backend/.greennode.json` (from `.greennode.json.example`) |
| `LLM_API_KEY` + `LLM_MODEL` (model `path`) | MaaS LLM | `/agentbase-llm` | `src/backend/.env` |
| `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` (optional) | Tracing, eval | Langfuse project settings | `src/backend/.env` |
| `agent_identity` name (only if tools use Identity providers) — the **dev** identity: one identity per environment (`<agent>-dev/-staging/-prod`), never the prod one locally | Outbound credentials (`/agentbase-build-identity`) | `/agentbase-identity` | `src/backend/.greennode.json` |
| `JWKS_URL` / `ISSUER` / `AUDIENCE` (non-secret) | Inbound JWT | The app's IdP | `.env` (`AUTH_*`) |

- **Never ask the user to paste a secret into the chat**, and never `cat`/print `.greennode.json`, `.env*` or keys. Create the file from its `.example`, tell the user which keys to fill, then let them run **`make check-creds`**: it prints only `OK/FAIL` + non-secret IDs (incl. the **developer's** IAM principal `iam:<sub>` — the runtime has its own, Step 9b). If the user pasted a secret anyway: write it only into the git-ignored file and advise rotating it.
- **Run the platform `agentbase-*` scripts with cwd = `src/backend`** (call them by absolute path): `check_credentials.sh`, `get_token.sh`, `save_env_var.sh`, `aip.sh api-keys create` / `--save-env` read and write `./.greennode.json` and `./.env` of the current directory, and this standard keeps both in `src/backend`. If a platform skill reports `MISSING` credentials, **never take its "paste client_id/secret in the chat" option** — ask the user to fill `src/backend/.greennode.json` (or point it at a credentials file path), then re-check.
- On AgentBase Runtime the IAM pair is injected — deploy env files never contain `GREENNODE_CLIENT_*`.
- **Team setup:** one developer service account per person (audit and revocation per person; Memory allows 10 concurrent requests per account), a dedicated CI account with a custom least-privilege policy, separate dev and prod MCP Gateways — `references/iam-permissions.md`.

## Build process (the coding agent MUST follow this order)

Show progress as `Step X/9`. Each step checks whether it is already done (idempotent) and writes resource IDs to `.agentbase-state.json`.

1. **Analyze requirements → Agent Spec.** Read `references/requirements-to-spec.md` **and `references/decision-guide.md`**, fill in the Agent Spec template. For **each** optional component (long-term memory, reflection, HITL, MCP server, A2A, frontend), state *on/off + reason* based on the decision guide. Principle: **minimal by default** — do not enable anything "because it might be needed later". Present the spec to the user and **wait for confirmation** before coding; save it to `docs/agent-spec.md`.
2. **Scaffold** — `/agentbase-build-scaffold`: create `src/backend` (+ `src/frontend` if the spec needs a UI) via the script, `uv sync`, `make test` must be green. "Off" components stay in the code and are **disabled via env** — never delete their modules (they import each other; only `app/identity.py` may go). Then the user fills `.greennode.json` (see *Credentials*) and `make check-creds` shows OK for IAM.
3. **LLM** — `/agentbase-build-llm`: get an API key via `/agentbase-llm` run from `src/backend` (its `api-keys create` / `--save-env` writes `LLM_API_KEY` into `./.env` itself, never shown; otherwise the user puts it there; `make check-creds` must now show OK for LLM); the user picks a model for each tier (reasoning/large/small) + fallback; map flows → tiers; decide on adaptive routing.
4. **Memory** — `/agentbase-build-memory`: create a memory store via `/agentbase-memory`, set `MEMORY_ID`, tune strategy/namespace and the context-compression threshold.
5. **Tools / MCP / A2A** — if a new MCP server is needed for an internal system: `/agentbase-build-mcp-server` (deploy it first). If another agent is needed (decision guide §5): `/agentbase-build-a2a`. Then `/agentbase-build-mcp`: write local tools; create/select an MCP Gateway (`/agentbase-gateway` — separate dev and prod gateways), connect MCP Connectors (catalog or custom), create a Policy Group (`/agentbase-policy`) with an ALLOW for the **developer principal** (`make check-creds`) for local runs — each environment's **runtime principal** is added after its first deploy (Step 9b) — and declare each `connectUrl` in `mcp_servers.json`. Plan for **10 policies per group** (`/agentbase-policy`; the MCP docs say 20) and one group per gateway. Mark side-effect tools in `HITL_TOOLS` (`/agentbase-build-hitl`).
6. **Auth** — `/agentbase-build-auth`: configure inbound JWT (JWKS/issuer/audience). Tools calling external services with a key or OAuth ⇒ `/agentbase-build-identity` (providers via `/agentbase-identity`, helpers in `app/identity.py`).
7. **Tracing + Eval** — install the official Langfuse skill (`npx skills add langfuse/skills --skill langfuse`) next to this set, then `/agentbase-build-tracing` (Langfuse v4 keys, prompt management, model prices; run a few requests and `lf_trace.py --latest 3 --audit` until it reports no FAIL) and `/agentbase-build-eval` (write the dataset from the spec, `make eval` meets the threshold).
8. **Frontend** (if any) — `/agentbase-build-frontend`.
9. **Deploy + verify** — `/agentbase-build-deploy` (CI or `/agentbase-deploy`), check `/health`, send a test call, inspect the trace in Langfuse (`release` = image tag), logs via `/agentbase-monitor`.
   - **9b. Grant the runtime principal** (when the agent uses an MCP Gateway with IAM inbound): the runtime has its own service account (`auto-sa-<agent>`), so a fresh deploy gets 403 on every tool. Read its principal from the runtime logs (`IAM principal … iam:<sub>`, logged at startup by `log_iam_principal()` — only when an enabled `auth: "iam"` MCP server is configured: `runtime.sh logs <id> --query "IAM principal"`), add it to the environment's Policy Group with `/agentbase-policy`, record it in `.agentbase-state.json` → `environments.<env>.principal`, re-test a tool call. Repeat per environment and after a service-account reset.

Finish: run **`references/checklist.md`** (Definition of Done) and report PASS/FAIL per item.

## HARD RULES

1. **Do not break the standard structure.** Business code goes in the right module: tool → `app/tools/`, node → `app/graph/builder.py`, prompt → `app/prompts/system.md`. Do not create parallel `agent.py`/`graph2.py`.
2. **Do not read `os.environ` all over the place.** All config goes through `app/config.py::Settings`. New variables must be added to `Settings` **and** `.env.example` (+ the `.env.<env>.example` files when they differ per environment). Allowed exceptions: `app/observability` and `app/prompts` (`LANGFUSE_*`, read by the Langfuse SDK), and the `${VAR}` expansion of `mcp_servers.json` / `a2a_agents.json` in `app/tools/mcp.py` and `app/a2a/client.py`.
3. **Every LLM call goes through `get_llm("<task>")`** (`app/llm`): flow → tier → model + fallback. Do not construct `ChatOpenAI` yourself; new flows go into `DEFAULT_TASK_TIERS`, new user-facing flows also into `CONVERSATION_TASKS` (trace/session layout), and every new module that calls `get_llm` into `LLM_MODULES` in `tests/conftest.py` (so the offline `fake_llm` patches it). Structured output also goes through it — `get_llm("<task>", schema=MyModel)` (structured output on every model of the fallback chain); never call methods such as `.with_structured_output()` on its result (`references/architecture.md` §5).
4. **user_id always comes from the verified token** (`Principal`), never from a tool parameter or request body. Memory actor_id/namespace are never tool parameters.
5. **No hardcoded secrets, no secrets in chat.** IAM is injected by the runtime (locally `.greennode.json`). Credentials that **tools** use to call external services live in AgentBase Identity (`/agentbase-build-identity`). The runtime env file (rendered from the CI secret store, or the user's git-ignored `.env.<env>`) may hold only the agent's own secrets: `LLM_API_KEY`, Langfuse keys, `FEEDBACK_SIGNING_KEY`, static keys for its own MCP servers / A2A peers. `.env*`/`.greennode.json` are never committed (except the non-secret `*.example`), printed or requested in chat.
6. **Every important step must have a Langfuse trace** (use `tracing.step()` / `tracing.event()` for non-LangChain code). Never log tokens/API keys.
7. **Side-effect tools must be in `HITL_TOOLS`** unless the user explicitly confirms it is not needed. MCP tools must go through an **MCP Gateway with a Policy Group** granting least privilege to the agent's principals (the developer's locally, each environment's runtime service account after deploy).
8. **New feature ⇒ new test + eval item.** `make test` and `make eval` must pass before deploy.
9. **Add dependencies only with `uv add <pkg>`** (dev: `uv add --dev`), commit `uv.lock`.
10. **Platform operations (creating memory, API keys, gateways, runtimes...) must go through the corresponding `agentbase-*` skill** and follow that skill's confirmation HARD GATE — never hand-roll curl from memory.

## Interaction Guidelines

- **Confirm before executing (HARD GATE)** for: bulk file creation/overwrite, creating platform resources, deploy. Proceed only when the user replies with a clear affirmative (`yes`, `ok`, `confirm`, or the equivalent in the user's language). Any other reply ⇒ treat it as an adjustment, update and ask again.
- **Do not decide business parameters yourself** (project name, model, which tools need HITL, whether there is a UI) — propose defaults and let the user choose.
- **Relationship with `/agentbase-wizard` (platform skill)**: the wizard is the fast path with a simple template (and has a known bug in its memory sample — see `references/platform-coverage.md`). Its description claims every build request, so teams should invoke `/agentbase-build` explicitly (or add a line to the project's `AGENTS.md` / `CLAUDE.md`, see the repo README). When the user wants a standardized/production architecture (LLM tiers, HITL, eval, tracing, auth, isolation) ⇒ use this skill; still call the platform `agentbase-*` skills for resource operations. If both are triggered, prefer `/agentbase-build` for the code.
- **Skill names when installed as a plugin** are namespaced: `/agentbase-build-llm` appears as `agentbase-build:agentbase-build-llm` (Claude Code plugin). Use whichever name the agent lists.
- **Read the assets before writing code** — sub-skills ship verified sample code in `assets/`; modify from there, do not rewrite from memory.
- When the user only asks "how do I", answer with guidance, do not create files.

## Review mode (`/agentbase-build review`)

For an existing project: compare against the standard structure + run `references/checklist.md`, list deviations by severity (BLOCKER / SHOULD / NICE), propose fixes per sub-skill.

## Upgrade mode (`/agentbase-build upgrade`)

A project generated by an older version of these skills ("upgrade / update the template / nâng cấp project", or `standard_version` in `.agentbase-state.json` older than this set's) ⇒ `/agentbase-build-scaffold` → *Upgrade an existing project*: dry run, show the plan, `--apply` only after the user confirms, resolve conflicts, `make test`. Never re-scaffold over a project or copy template files over it.

## References

- `references/architecture.md` — request flow, module responsibilities, sequence diagram.
- `references/requirements-to-spec.md` — Agent Spec template + requirement → component mapping table.
- `references/api-contract.md` — payload/headers/SSE events between frontend ↔ backend.
- `references/decision-guide.md` — **when to enable** long-term memory / reflection / HITL / MCP server / A2A / UI, use-case patterns, cost of each layer.
- `references/checklist.md` — Definition of Done / review checklist.
- `references/security.md` — attack surface, prompt injection, personal data.
- `references/platform-coverage.md` — per-feature AgentBase ↔ skill mapping, platform bugs encountered, open items.
- `references/private-networking.md` — Public vs Private Runtime / MCP Gateway (VPC Peering), reaching internal systems and on-prem MCP servers over VPN.
- `references/iam-permissions.md` — who needs which permissions: team roles, developer / CI / runtime / gateway service accounts, API keys, secret rotation.

## Official docs

- [agent-base](https://docs.greennode.ai/ai-stack/agent-base) — AgentBase overview and modules
- [getting-started](https://docs.greennode.ai/ai-stack/agent-base/getting-started) — service account (AgentBaseFullAccess, vcrFullAccess, AiPlatformFullAccess), IAM token, `.greennode.json`
- [reference](https://docs.greennode.ai/ai-stack/agent-base/reference) — every REST path, pagination rules, env vars, SDK imports, platform limits
- [faq](https://docs.greennode.ai/ai-stack/agent-base/faq) — troubleshooting and resource teardown order
- [runtime-reference](https://docs.greennode.ai/ai-stack/agent-base/agent-runtime/runtime-reference) — runtime contract: port 8080, `/health`, injected env vars, headers
- [private-networking](https://docs.greennode.ai/ai-stack/agent-base/private-networking) — Public vs Private Runtime / MCP Gateway
- [roles-and-permissions](https://docs.greennode.ai/ai-stack/agent-base/team-permissions/roles-and-permissions) — team roles and permission matrix

Tip for coding agents: append `.md` to any docs URL for clean Markdown, and use https://docs.greennode.ai/llms.txt as the index of every page. When a skill and the docs disagree, trust the docs for platform behavior and flag the difference to the user.
