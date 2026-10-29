# __PROJECT_NAME__ — instructions for coding agents

This AI agent follows the **AgentBase Build** standard (skills `agentbase-build-*`,
https://github.com/greennode-samples/sample-greennode-agentbase-builder-skills). The skills hold the rules, the
verified code and the known platform pitfalls; the code alone does not. **Before you change, debug or review any part
of this project, load the skill that owns it** — also for small fixes and error reports.

## Which skill owns what

| Part (path) | Skill |
|---|---|
| Overall design, new feature spanning several parts, review against the standard | `agentbase-build` |
| Project layout, `pyproject.toml` / `uv.lock`, dependencies, Makefile, upgrading to a newer template | `agentbase-build-scaffold` |
| `src/backend/app/limits.py`, `app/service.py` — request size, concurrency, per-user / per-tenant rate limits, the request flow | `agentbase-build-scaffold` |
| `src/backend/app/llm/` — models, tiers, fallback, the agent's LLM API key | `agentbase-build-llm` |
| `src/backend/app/memory/` — conversation history, long-term facts, context compression | `agentbase-build-memory` |
| `src/backend/app/observability/`, `app/prompts/` — Langfuse traces, cost, prompt versions | `agentbase-build-tracing` |
| `src/backend/app/tools/`, `mcp_servers.json` — tools, MCP connectors, Gateway, Policy Group | `agentbase-build-mcp` |
| A self-built MCP server under `src/` | `agentbase-build-mcp-server` |
| `src/backend/app/a2a/`, `a2a_agents.json` — calling or exposing other agents | `agentbase-build-a2a` |
| `src/backend/app/auth/` — login, JWT, API keys of callers, organizations (tenants) and roles, Runtime inbound auth | `agentbase-build-auth` |
| `src/backend/app/identity.py` — keys / OAuth tokens that tools use for external services | `agentbase-build-identity` |
| `src/backend/app/hitl.py`, `HITL_TOOLS` — human approval of tool calls | `agentbase-build-hitl` |
| `src/backend/evals/`, `app/reflection.py` — quality datasets, LLM judge, eval gate, feedback | `agentbase-build-eval` |
| `src/frontend/` — the mobile chat app | `agentbase-build-frontend` |
| `src/web/` — the web chat app and its BFF (login, cookies, CSRF, CSP) | `agentbase-build-web` |
| `deploy/`, `.github/workflows/ci.yml`, `.env.<env>.example`, releases, rollback | `agentbase-build-deploy` |
| `scripts/security_scan.py`, security review, threat model | `agentbase-build-security-audit` |

## Symptom → skill

| Symptom | Load first |
|---|---|
| `/invocations` returns 401 / 403, login broken after an IdP change, User-Id header rejected | `agentbase-build-auth` |
| A tool gets 403 / "denied by policy", an MCP server is skipped, tools missing | `agentbase-build-mcp` |
| A tool cannot get its external API key or OAuth token, "authorization required" | `agentbase-build-identity` |
| Set up or replace the agent's own LLM API key | `agentbase-build-llm` |
| Wrong / missing model, LLM 401 / 429 / 5xx, cost or latency went up | `agentbase-build-llm`, then `agentbase-build-tracing` |
| Slow or wrong answers, need to see what happened in a turn | `agentbase-build-tracing` |
| The agent forgot something, context too long, one user sees another user's data | `agentbase-build-memory` (cross-user data: also `agentbase-build-security-audit`) |
| Approval card missing or a write ran without approval, 409 on resume, an approved action ran twice or shows "Outcome unknown" | `agentbase-build-hitl` |
| The agent answers 429 / 413 / 503 "busy", a limit to raise for a heavy user or tenant | `agentbase-build-scaffold` (then `agentbase-build-deploy` for a gateway quota) |
| `make eval` below the threshold, thumbs-down answers to investigate | `agentbase-build-eval` |
| `make security-scan` FAIL / WARN, any secret (LLM key included) pasted into a chat or committed, injection worries | `agentbase-build-security-audit` |
| Deploy failed, health check failing, settings lost after an update, rollback | `agentbase-build-deploy` |
| `make test` broken after editing the template, dependency conflicts | `agentbase-build-scaffold` |

Platform operations (create memory stores, API keys, gateways, runtimes; read runtime logs) go through the platform
skills `agentbase-*` (`/agentbase-memory`, `/agentbase-llm`, `/agentbase-gateway`, `/agentbase-deploy`,
`/agentbase-monitor`…), which ask for confirmation — never hand-roll API calls.

## Rules that always apply

- Config only through `src/backend/app/config.py` (`Settings`) + `.env.example`; every LLM call through `get_llm(task)`.
- The user id comes from the verified token, never from a tool parameter or the request body.
- Side-effect tools are listed in `HITL_TOOLS` and pass `idempotency_key(config, tool_call_id)` to the system they change. Secrets never go in code, git, traces or the chat: the user runs
  `make set-secret` in their own terminal.
- Before you finish a change: `make test`, `make lint`, `make security-scan` pass; a new feature adds a test and an
  eval item.
