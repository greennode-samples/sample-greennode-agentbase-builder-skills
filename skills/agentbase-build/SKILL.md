---
name: agentbase-build
description: "Prefer this skill (over /agentbase-wizard) when a standardized architecture is needed. Standard + process for a coding agent to BUILD a production-ready AI agent on GreenNode AgentBase with a standardized architecture (Python + uv + LangGraph, src/backend + src/frontend React Native). Use when the user wants to build/create/design an agent, chatbot, assistant or copilot for a new business requirement, or to review an existing agent against the standard. Trigger: build agent, create an agent for ..., design agent, standard agent, agentbase build, review agent, check agent standard, tạo agent cho ..., thiết kế agent, agent chuẩn, kiểm tra chuẩn agent. This skill ORCHESTRATES the agentbase-build-* skills (scaffold, llm, memory, tracing, mcp, mcp-server, auth, identity, hitl, eval, a2a, frontend, deploy) and the platform agentbase-* skills (llm, memory, identity, gateway, deploy, monitor). DO NOT use for platform reference questions (use /agentbase), or single operations on an existing resource (use the corresponding platform skill)."
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
| Tracing | **Langfuse Python SDK v4** + `langfuse.langchain.CallbackHandler` | `agentbase-build-tracing` |
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
├── Makefile                  # setup | dev | test | eval | lint | docker-build | invoke | fe-dev
├── README.md
├── .agentbase-state.json     # platform resource IDs (memory_id, runtime_id, gateway...)
└── src/
    ├── backend/              # uv project — deployed as 1 AgentBase Runtime container
    │   ├── pyproject.toml / uv.lock / .python-version (3.13) / Dockerfile / .env.example
    │   ├── mcp_servers.json / a2a_agents.json
    │   ├── main.py           # THIN entrypoint
    │   ├── app/
    │   │   ├── config.py     # Settings (pydantic-settings) — the ONLY env source
    │   │   ├── service.py    # payload contract + orchestration of 1 request
    │   │   ├── graph/        # state.py, builder.py — the ONLY graph
    │   │   ├── llm/          # get_llm(task) — tiers reasoning/large/small + fallback; routing.py
    │   │   ├── memory/       # short_term.py, long_term.py, compression.py
    │   │   ├── tools/        # __init__ (registry), local_tools.py, mcp.py
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
| `agent_identity` name (only if tools use Identity providers) | Outbound credentials (`/agentbase-build-identity`) | `/agentbase-identity` | `src/backend/.greennode.json` |
| `JWKS_URL` / `ISSUER` / `AUDIENCE` (non-secret) | Inbound JWT | The app's IdP | `.env` (`AUTH_*`) |

- **Never ask the user to paste a secret into the chat**, and never `cat`/print `.greennode.json`, `.env*` or keys. Create the file from its `.example`, tell the user which keys to fill, then let them run **`make check-creds`**: it prints only `OK/FAIL` + non-secret IDs (incl. the agent's IAM principal `iam:<sub>` for Policy Groups). If the user pasted a secret anyway: write it only into the git-ignored file and advise rotating it.
- On AgentBase Runtime the IAM pair is injected — deploy env files never contain `GREENNODE_CLIENT_*`.

## Build process (the coding agent MUST follow this order)

Show progress as `Step X/9`. Each step checks whether it is already done (idempotent) and writes resource IDs to `.agentbase-state.json`.

1. **Analyze requirements → Agent Spec.** Read `references/requirements-to-spec.md` **and `references/decision-guide.md`**, fill in the Agent Spec template. For **each** optional component (long-term memory, reflection, HITL, MCP server, A2A, frontend), state *on/off + reason* based on the decision guide. Principle: **minimal by default** — do not enable anything "because it might be needed later". Present the spec to the user and **wait for confirmation** before coding.
2. **Scaffold** — `/agentbase-build-scaffold`: create `src/backend` (+ `src/frontend` if the spec needs a UI) via the script, `uv sync`, `make test` must be green. Then the user fills `.greennode.json` + `.env` (see *Credentials*) and `make check-creds` shows OK for IAM and LLM.
3. **LLM** — `/agentbase-build-llm`: get an API key via `/agentbase-llm`; the user picks a model for each tier (reasoning/large/small) + fallback; map flows → tiers; decide on adaptive routing.
4. **Memory** — `/agentbase-build-memory`: create a memory store via `/agentbase-memory`, set `MEMORY_ID`, tune strategy/namespace and the context-compression threshold.
5. **Tools / MCP / A2A** — if a new MCP server is needed for an internal system: `/agentbase-build-mcp-server` (deploy it first). If another agent is needed (decision guide §5): `/agentbase-build-a2a`. Then `/agentbase-build-mcp`: write local tools; create/select an MCP Gateway (`/agentbase-gateway`), connect MCP Connectors (catalog or custom), create a Policy Group for the agent's principal (`/agentbase-policy`), declare each `connectUrl` in `mcp_servers.json`. Mark side-effect tools in `HITL_TOOLS` (`/agentbase-build-hitl`).
6. **Auth** — `/agentbase-build-auth`: configure inbound JWT (JWKS/issuer/audience). Tools calling external services with a key or OAuth ⇒ `/agentbase-build-identity` (providers via `/agentbase-identity`, helpers in `app/identity.py`).
7. **Tracing + Eval** — `/agentbase-build-tracing` (Langfuse v4 keys, prompt management, model prices) and `/agentbase-build-eval` (write the dataset from the spec, `make eval` meets the threshold).
8. **Frontend** (if any) — `/agentbase-build-frontend`.
9. **Deploy + verify** — `/agentbase-build-deploy` → `/agentbase-deploy`, check `/health`, send a test call, inspect the trace in Langfuse, logs via `/agentbase-monitor`.

Finish: run **`references/checklist.md`** (Definition of Done) and report PASS/FAIL per item.

## HARD RULES

1. **Do not break the standard structure.** Business code goes in the right module: tool → `app/tools/`, node → `app/graph/builder.py`, prompt → `app/prompts/system.md`. Do not create parallel `agent.py`/`graph2.py`.
2. **Do not read `os.environ` all over the place.** All config goes through `app/config.py::Settings`. New variables must be added to `Settings` **and** `.env.example`.
3. **Every LLM call goes through `get_llm("<task>")`** (`app/llm`): flow → tier → model + fallback. Do not construct `ChatOpenAI` yourself; new flows go into `DEFAULT_TASK_TIERS`.
4. **user_id always comes from the verified token** (`Principal`), never from a tool parameter or request body. Memory actor_id/namespace are never tool parameters.
5. **No hardcoded secrets, no secrets in chat.** IAM is injected by the runtime (locally `.greennode.json`); external-service secrets are stored in AgentBase Identity; `.env`/`.greennode.json` are never committed, printed or requested in chat.
6. **Every important step must have a Langfuse trace** (use `tracing.step()` / `tracing.event()` for non-LangChain code). Never log tokens/API keys.
7. **Side-effect tools must be in `HITL_TOOLS`** unless the user explicitly confirms it is not needed. MCP tools must go through an **MCP Gateway with a Policy Group** granting least privilege to the agent's principal.
8. **New feature ⇒ new test + eval item.** `make test` and `make eval` must pass before deploy.
9. **Add dependencies only with `uv add <pkg>`** (dev: `uv add --dev`), commit `uv.lock`.
10. **Platform operations (creating memory, API keys, gateways, runtimes...) must go through the corresponding `agentbase-*` skill** and follow that skill's confirmation HARD GATE — never hand-roll curl from memory.

## Interaction Guidelines

- **Confirm before executing (HARD GATE)** for: bulk file creation/overwrite, creating platform resources, deploy. Proceed only when the user replies with a clear affirmative (`yes`, `ok`, `confirm`, or the equivalent in the user's language). Any other reply ⇒ treat it as an adjustment, update and ask again.
- **Do not decide business parameters yourself** (project name, model, which tools need HITL, whether there is a UI) — propose defaults and let the user choose.
- **Relationship with `/agentbase-wizard` (platform skill)**: the wizard is the fast path with a simple template (and has a known bug in its memory sample — see `references/platform-coverage.md`). When the user wants a standardized/production architecture (LLM tiers, HITL, eval, tracing, auth, isolation) ⇒ use this skill; still call the platform `agentbase-*` skills for resource operations. If both are triggered, prefer `/agentbase-build` for the code.
- **Read the assets before writing code** — sub-skills ship verified sample code in `assets/`; modify from there, do not rewrite from memory.
- When the user only asks "how do I", answer with guidance, do not create files.

## Review mode (`/agentbase-build review`)

For an existing project: compare against the standard structure + run `references/checklist.md`, list deviations by severity (BLOCKER / SHOULD / NICE), propose fixes per sub-skill.

## References

- `references/architecture.md` — request flow, module responsibilities, sequence diagram.
- `references/requirements-to-spec.md` — Agent Spec template + requirement → component mapping table.
- `references/api-contract.md` — payload/headers/SSE events between frontend ↔ backend.
- `references/decision-guide.md` — **when to enable** long-term memory / reflection / HITL / MCP server / A2A / UI, use-case patterns, cost of each layer.
- `references/checklist.md` — Definition of Done / review checklist.
- `references/security.md` — attack surface, prompt injection, personal data.
- `references/platform-coverage.md` — per-feature AgentBase ↔ skill mapping, platform bugs encountered, open items.
- `references/private-networking.md` — Public vs Private Runtime / MCP Gateway (VPC Peering), reaching internal systems and on-prem MCP servers over VPN.

## Official docs

- [agent-base](https://docs.greennode.ai/ai-stack/agent-base) — AgentBase overview and modules
- [getting-started](https://docs.greennode.ai/ai-stack/agent-base/getting-started) — service account (AgentBaseFullAccess, vcrFullAccess, AiPlatformFullAccess), IAM token, `.greennode.json`
- [reference](https://docs.greennode.ai/ai-stack/agent-base/reference) — every REST path, pagination rules, env vars, SDK imports, platform limits
- [faq](https://docs.greennode.ai/ai-stack/agent-base/faq) — troubleshooting and resource teardown order
- [runtime-reference](https://docs.greennode.ai/ai-stack/agent-base/agent-runtime/runtime-reference) — runtime contract: port 8080, `/health`, injected env vars, headers
- [private-networking](https://docs.greennode.ai/ai-stack/agent-base/private-networking) — Public vs Private Runtime / MCP Gateway

Tip for coding agents: append `.md` to any docs URL for clean Markdown, and use https://docs.greennode.ai/llms.txt as the index of every page. When a skill and the docs disagree, trust the docs for platform behavior and flag the difference to the user.
