---
name: agentbase-build-a2a
description: "A2A standard (Agent2Agent Protocol, a2a-sdk 1.x JSON-RPC) for AI agents on GreenNode AgentBase: expose the agent as an A2A server (Agent Card /.well-known/agent-card.json + POST /a2a, same container as /invocations), reuse inbound auth (api_key/JWT) and isolate tasks + memory per user, HITL via the INPUT_REQUIRED state; and an A2A client — call other agents as a LangGraph tool (a2a_agents.json), propagate user identity to the target agent, trace a2a.call. Use for multi-agent setups across independent agents (different teams/runtimes), agent-to-agent calls, publishing an agent for other systems. Trigger: A2A, agent2agent, agent calls agent, multi-agent, agent card, connect agent to agent, agent gọi agent, kết nối agent với agent. DO NOT use for sub-agents within the same graph (add a node/subgraph in app/graph/builder.py) or tool calls (use MCP)."
---

# A2A — Agent2Agent Protocol

AgentBase has **no** dedicated A2A gateway/registry (docs 2026-10) ⇒ A2A runs **inside the agent container itself** via `a2a-sdk`, sharing the Runtime endpoint. Skill assets: `app/a2a/{server,client}.py`, `a2a_agents.json`, `tests/test_a2a.py` (5 e2e tests: real uvicorn + real a2a client).

## When to use A2A vs MCP vs sub-agent

| Need | Use |
|---|---|
| Call a **function/service** (deterministic, with schema) | **MCP** tool (`/agentbase-build-mcp`, `/agentbase-build-mcp-server`) |
| Delegate work to **another agent** (reasons on its own, has memory, owned by another team, deployed separately) | **A2A** |
| Split logic within **the same agent** | Node / subgraph in `app/graph/builder.py` |

## A2A server (this agent is called)

Enable: `A2A_ENABLED=true`, `A2A_PUBLIC_URL` (empty ⇒ runtime-injected `GREENNODE_ENDPOINT_URL`), `A2A_DESCRIPTION`, `A2A_SKILLS` (JSON list `{id,name,description,tags,examples}` — written per the Agent Spec).

| Route | Auth | Notes |
|---|---|---|
| `GET /.well-known/agent-card.json` | public | name, version, interface `JSONRPC` `<url>/a2a`, `securitySchemes` (apiKey header or bearer JWT per `AUTH_MODE`), skills |
| `POST /a2a` | **required** (same `authenticate()` as `/invocations`) | SendMessage, GetTask, ListTasks, CancelTask... |

Standard mapping (verified by tests):

| A2A | Agent |
|---|---|
| authenticated user (`X-GreenNode-AgentBase-User-Id` + api key, or JWT `sub`) | `user_id` ⇒ `request.user` ⇒ **task store keyed by owner** (user B GetTask on A's task ⇒ `TaskNotFoundError`) |
| `contextId` | session `a2a-<contextId>` (memory per (session, user)) |
| message text | `service.run_chat()` |
| graph interrupt (HITL) | task `TASK_STATE_INPUT_REQUIRED` + description of the tool awaiting approval; next message in the same contextId: `approve` or `reject: <reason>` ⇒ `run_resume()` |
| answer | artifact `response` + `TASK_STATE_COMPLETED` |
| auth/validation error | `TASK_STATE_FAILED` |

Note: the executor must **enqueue the Task before** any status update (A2A v1 — otherwise `InvalidAgentResponseError`). The default task store is `InMemoryTaskStore` (lost on restart, not shared across replicas) ⇒ multi-replica prod uses a2a-sdk's `DatabaseTaskStore` (Postgres) — conversation history stays durable in AgentBase Memory.

## A2A client (this agent calls other agents)

`a2a_agents.json` (supports `${ENV}`; secrets go in the runtime env file / Identity, not committed):

```json
{"agents": {"stock-agent": {"url": "${A2A_STOCK_AGENT_URL}", "description": "Vietnam stock agent",
                            "auth": "api_key", "api_key": "${A2A_STOCK_AGENT_API_KEY}"}}}
```

- Each agent ⇒ tool `ask_<name>(message)` (already included in `collect_tools`, trace `tools.collect.output.a2a`).
- **Per-user isolation across agents**: the tool takes `actor_id`/`thread_id` from `RunnableConfig` (not from the LLM) ⇒ sends `X-GreenNode-AgentBase-User-Id` + `contextId=session` ⇒ the target agent keeps memory for the correct user (tested by `test_client_tool_propagates_user`).
- `auth`: `api_key` (target agent built from this template) · `user_jwt` (forward the end-user JWT, same IdP) · `none` (local).
- Target agent returns `INPUT_REQUIRED` ⇒ the tool returns `[<agent> needs confirmation] ...` so the current agent asks the user.
- **Only a human may confirm another agent's action.** `ask_<agent>` refuses to relay a decision (`approve`, `yes`, `đồng ý`, `reject: …`) unless `ask_<agent>` is in `HITL_TOOLS` — then the user approves that exact call in the approval card. ⇒ For target agents that take actions, **add `ask_<agent>` to `HITL_TOOLS`** (otherwise their confirmations cannot be completed). Tested by `test_llm_cannot_relay_decision_without_hitl`.
- Trace: span `a2a.call` {agent, url, message, state, text}.

## Workflow

1. Write `A2A_SKILLS` + `A2A_DESCRIPTION` that accurately reflect capabilities (other agents rely on the card to decide what to delegate).
2. `A2A_ENABLED=true` in `.env.<env>` ⇒ deploy (`/agentbase-build-deploy`). Check `curl <endpoint>/.well-known/agent-card.json`; `POST /a2a` without key ⇒ 401.
3. Issue credentials to calling agents: a separate api key per client agent (add its hash to `AUTH_API_KEY_SHA256`), for independent rotation/revocation.
4. In the calling agent: declare `a2a_agents.json`, URL + key via env ⇒ `make test` ⇒ deploy.
5. Check cross-agent isolation: 2 different users calling through agent A ⇒ agent B doesn't mix memory.

## Current limitations (state these clearly to the user)

- Streaming (`capabilities.streaming=false`) and push notifications not enabled yet.
- No platform registry/discovery ⇒ the agent list is managed in `a2a_agents.json`.
- api_key mode trusts the caller's User-Id header ⇒ only issue keys to trusted agents/systems; public agents use JWT.
