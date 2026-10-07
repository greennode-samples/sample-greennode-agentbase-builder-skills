---
name: agentbase-build-a2a
description: "A2A standard (Agent2Agent Protocol, a2a-sdk 1.x JSON-RPC) for AI agents on GreenNode AgentBase: expose the agent as an A2A server (Agent Card /.well-known/agent-card.json + POST /a2a, same container as /invocations), reuse inbound auth (api_key/JWT) and isolate tasks + memory per user, HITL via the INPUT_REQUIRED state; and an A2A client — call other agents as a LangGraph tool (a2a_agents.json), propagate user identity to the target agent, trace a2a.call. Use for multi-agent setups across independent agents (different teams/runtimes), agent-to-agent calls, publishing an agent for other systems. Trigger: A2A, agent2agent, agent calls agent, multi-agent, agent card, connect agent to agent, agent gọi agent, kết nối agent với agent. DO NOT use for sub-agents within the same graph (add a node/subgraph in app/graph/builder.py) or tool calls (use MCP)."
---

# A2A — Agent2Agent Protocol

AgentBase has **no** dedicated A2A gateway/registry (docs 2026-10) ⇒ A2A runs **inside the agent container itself** via `a2a-sdk`, sharing the Runtime endpoint. Skill assets: `app/a2a/{server,client}.py`, `a2a_agents.json`, `tests/test_a2a.py` (23 tests — mostly e2e: real uvicorn app + real a2a-sdk client, real local JWKS for JWT mode).

## When to use A2A vs MCP vs sub-agent

| Need | Use |
|---|---|
| Call a **function/service** (deterministic, with schema) | **MCP** tool (`/agentbase-build-mcp`, `/agentbase-build-mcp-server`) |
| Delegate work to **another agent** (reasons on its own, has memory, owned by another team, deployed separately) | **A2A** |
| Split logic within **the same agent** | Node / subgraph in `app/graph/builder.py` |

## A2A server (this agent is called)

Enable: `A2A_ENABLED=true`, `A2A_PUBLIC_URL` (**set it explicitly** to the runtime endpoint; the empty fallback `GREENNODE_ENDPOINT_URL` is not in the official list of injected variables, so it may be absent ⇒ the Agent Card would advertise localhost), `A2A_DESCRIPTION`, `A2A_SKILLS` (JSON list `{id,name,description,tags,examples}` — written per the Agent Spec).

| Route | Auth | Notes |
|---|---|---|
| `GET /.well-known/agent-card.json` | public | name, version, interface `JSONRPC` `<url>/a2a`, `securitySchemes` (apiKey header or bearer JWT per `AUTH_MODE`), skills |
| `POST /a2a` | **required** (same `authenticate()` as `/invocations`, same HTTP status: 401 bad credentials, 403 User-Id ≠ token subject, 400 missing User-Id in api_key mode, 503 IdP/JWKS down) | SendMessage, GetTask, ListTasks, CancelTask... |

Standard mapping (verified by tests):

| A2A | Agent |
|---|---|
| authenticated user (`X-GreenNode-AgentBase-User-Id` + api key, or JWT `sub`) | `user_id` ⇒ `request.user` ⇒ **task store keyed by owner** (user B GetTask on A's task ⇒ `TaskNotFoundError`) |
| verified Principal (claims + token) | `request.auth` ⇒ `run_chat`/`run_resume(principal=…)` ⇒ tools get the same `AUTH_FORWARD_CLAIMS` (`user_claims`) and user token as on `/invocations` — RBAC in tools applies to A2A too (`test_a2a_runs_with_verified_claims_and_token`) |
| `contextId` | session `a2a-<contextId>` (memory per (session, user)) |
| message text | `service.run_chat()` |
| graph interrupt (HITL) | task `TASK_STATE_INPUT_REQUIRED` + description of the tool awaiting approval; next message in the same contextId: `approve` or `reject: <reason>` ⇒ `run_resume()` |
| decision when nothing is pending any more (duplicate / concurrent `approve`) | `TASK_STATE_FAILED` "…already handled" — never a second INPUT_REQUIRED; chat while an approval is pending ⇒ `INPUT_REQUIRED` naming the pending call |
| answer | artifact `response` + `TASK_STATE_COMPLETED` |
| validation error inside the task | `TASK_STATE_FAILED` |

Note: the executor must **enqueue the Task before** any status update (A2A v1 — otherwise `InvalidAgentResponseError`). The default task store is `InMemoryTaskStore` (lost on restart, not shared across replicas, and it **never evicts** — every message without a taskId adds a task, so memory grows for the container's lifetime; fine for demos only) ⇒ multi-replica prod uses a2a-sdk's `DatabaseTaskStore` (Postgres) — conversation history stays durable in AgentBase Memory.

## A2A client (this agent calls other agents)

`a2a_agents.json` (`${VAR}` — braces only — is expanded inside string values **after** JSON parsing, so a `"` or `\` in a key can't break the file or inject keys like `auth`; an unset/empty variable skips that agent with an error log naming it, never sends the literal; `enabled: false` entries are not expanded. Secrets go in the runtime env file / Identity, not committed):

```json
{"agents": {"stock-agent": {"url": "${A2A_STOCK_AGENT_URL}", "description": "Vietnam stock agent",
                            "auth": "api_key", "api_key": "${A2A_STOCK_AGENT_API_KEY}"}}}
```

- Each agent ⇒ tool `ask_<name>(message)` (already included in `collect_tools`, trace `tools.collect.output.a2a`).
- **Per-user isolation across agents**: the tool takes `actor_id`/`thread_id` from `RunnableConfig` (not from the LLM) ⇒ sends `X-GreenNode-AgentBase-User-Id` + `contextId=session` ⇒ the target agent keeps memory for the correct user (tested by `test_client_tool_propagates_user`).
- `auth`: `api_key` (target agent built from this template) · `user_jwt` (forward the end-user JWT, same IdP) · `none` (local). Any other value is skipped with an error log. Outside `APP_ENV=local` the `url` must be `https://` (entries with http are skipped with an error log).
- **No IAM mode**: a target whose Runtime **Inbound Auth is `IAM Permissions`** can't be called with this client (it would need a GreenNode IAM bearer in `Authorization` plus the app-level key/JWT in a custom header). Use Runtime `JWT` or `No authorization` + IP Access Control on the target, or extend `_headers` (e.g. with `IAMBearerAuth` from `app/tools/mcp.py`).
- **Credentials only go to the configured origin**: the RPC URL comes from the target's Agent Card; interfaces whose scheme + host + port differ from `url` are dropped, none left ⇒ the call is refused before any credential is sent (`test_card_pointing_elsewhere_gets_no_credentials`). The target must advertise its real URL (`A2A_PUBLIC_URL`).
- `contextId` = the caller's session id **unchanged** (already validated `[A-Za-z0-9-]`) ⇒ distinct sessions stay distinct on the target.
- `user_jwt` replays the user's token at the target, so the target must accept this agent's `AUTH_AUDIENCE`. Only use it for agents you trust with that token; otherwise use `api_key` (identity still propagates via `X-GreenNode-AgentBase-User-Id`).
- Target agent returns `INPUT_REQUIRED` ⇒ the tool returns `[<agent> needs confirmation] ...` so the current agent asks the user.
- **Only a human may confirm another agent's action.** `ask_<agent>` refuses to relay a decision (`approve`, `yes`, `đồng ý`, `reject: …`) unless `ask_<agent>` is in `HITL_TOOLS` — then the user approves that exact call in the approval card. ⇒ For target agents that take actions, **add `ask_<agent>` to `HITL_TOOLS`** (otherwise their confirmations cannot be completed). Tested by `test_llm_cannot_relay_decision_without_hitl`.
- Trace: span `a2a.call` {agent, url, message, state, text}.

## Workflow

1. Write `A2A_SKILLS` + `A2A_DESCRIPTION` that accurately reflect capabilities (other agents rely on the card to decide what to delegate).
2. `A2A_ENABLED=true` in `.env.<env>` ⇒ deploy (`/agentbase-build-deploy`). Check `curl <endpoint>/.well-known/agent-card.json`; `POST /a2a` without key ⇒ 401.
3. Issue credentials to calling agents: a separate api key per client agent (add its hash to `AUTH_API_KEY_SHA256`) — for independent **rotation/revocation and audit, not isolation**: tasks and memory are owned by `user_id` only, so any key holder can act as any `user_id` it asserts (read that user's A2A tasks, continue their sessions). Only give keys to agents you trust with every user; otherwise use `jwt`.
4. In the calling agent: declare `a2a_agents.json`, URL + key via env ⇒ `make test` ⇒ deploy.
5. Check cross-agent isolation: 2 different users calling through agent A ⇒ agent B doesn't mix memory.

## Current limitations (state these clearly to the user)

- Streaming (`capabilities.streaming=false`) and push notifications not enabled yet.
- No platform registry/discovery ⇒ the agent list is managed in `a2a_agents.json`.
- api_key mode trusts the caller's User-Id header and does not bind tasks to the key ⇒ every key holder can act for every user; only issue keys to trusted agents/systems; public agents use JWT.
- No IAM mode in the client (see above).

## Official docs

- [runtime-reference](https://docs.greennode.ai/ai-stack/agent-base/agent-runtime/runtime-reference) — endpoints and injected env vars (set `A2A_PUBLIC_URL` explicitly)
- [create-runtime](https://docs.greennode.ai/ai-stack/agent-base/agent-runtime/create-runtime) — Inbound Auth / IP Access Control for agent-to-agent callers
