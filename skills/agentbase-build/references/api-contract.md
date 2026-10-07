# API contract frontend ↔ backend (`POST /invocations`)

Source of truth: the docstring at the top of `src/backend/app/service.py`. Contract change ⇒ update `src/frontend/src/api/agentClient.ts`, the tests and this file together.

## Headers

| Header | Required | Notes |
|---|---|---|
| `Content-Type: application/json` | ✔ | |
| `Authorization: Bearer <JWT>` | ✔ when `AUTH_MODE=jwt` | Header name configurable via `AUTH_TOKEN_HEADER` (e.g. when the runtime endpoint uses `Authorization` for IAM ⇒ use `X-GreenNode-AgentBase-Custom-User-Token`) |
| `X-GreenNode-AgentBase-Session-Id` | ✔ for chat/resume | UUID, 1 conversation = 1 session. Format `^[A-Za-z0-9][A-Za-z0-9-]{0,127}$` (blocks `../`, `/`), invalid ⇒ 400 |
| `X-GreenNode-AgentBase-Custom-Api-Key: <key>` | ✔ when `AUTH_MODE=api_key` | Only for trusted callers (server/BFF/other agents) |
| `X-GreenNode-AgentBase-User-Id` | jwt: ✖ · **api_key: ✔** | jwt: if sent it must match `sub` (otherwise ⇒ 403). api_key: required (memory is isolated per user). Format `^[A-Za-z0-9][A-Za-z0-9._@+=-]{0,127}$`, invalid ⇒ 400. A JWT whose `sub` is outside this format ⇒ `user_id = "u-" + sha256(iss|sub)[:40]` |
| `X-GreenNode-AgentBase-Custom-*` | ✖ | Custom data, read via `context.request_headers` |
| `Accept: text/event-stream` | when streaming | |

## Payloads

```jsonc
// Chat
{"type": "chat", "message": "Hello", "stream": false}

// Resume after a HITL interrupt
{"type": "resume", "stream": false, "interrupt_id": "<interrupt.id>",  // recommended: differs from the pending interrupt ⇒ 409
 "decisions": [
  {"tool_call_id": "call_1", "action": "approve"},
  {"tool_call_id": "call_2", "action": "edit", "args": {"amount": 100}},
  {"tool_call_id": "call_3", "action": "reject", "reason": "Wrong recipient"}
]}

// Feedback on 1 answer (records a user_feedback score on the Langfuse trace)
{"type": "feedback", "trace_id": "…", "feedback_token": "…", "score": 1, "comment": "spot on"}
// score ∈ {-1, 0, 1}; feedback_token comes from the response (HMAC of user+trace) — used by another user ⇒ 403
```

## Responses (non-stream)

```jsonc
{"status": "success", "response": "…", "tools_used": ["get_current_time"],
 "session_id": "…", "trace_id": "…", "feedback_token": "…"}

{"status": "interrupted", "session_id": "…", "trace_id": "…",
 "interrupt": {"id": "…", "type": "tool_approval", "message": "<AI message>",
               "tool_calls": [{"id": "call_1", "name": "gateway_create_ticket", "args": {…}}]}}
```

Errors: HTTP status + `{"error", "error_type", "details"}` (SDK format). `400` invalid payload · `401` missing/invalid token · `403` spoofed user header · `409` invalid HITL state / stale `interrupt_id` / another request of the same session is running · `422` exceeded `MAX_TOOL_ROUNDS` · `502` LLM returned an error · `503` MaaS/Memory overloaded or JWKS unreachable (client retries with backoff) · `504` exceeded `REQUEST_TIMEOUT_S` · `500` other errors.

## SSE events (`stream: true`)

Each frame is `data: {json}\n\n`:

| event | Fields | Frontend handling |
|---|---|---|
| `token` | `data` | append to the answer bubble |
| `tool_start` | `name` | show "using tool …" |
| `tool_end` | `name`, `status` | |
| `reset` | `reason` | **clear the displayed text** (self-eval requested a new answer) |
| `interrupt` | same as the `interrupted` response | show the approval card (Approve / Reject; `edit` is supported by the API — the sample `ApprovalCard` has no Edit button yet) |
| `done` | same as the `success` response | finalize the bubble, store `trace_id` for feedback |
| `error` | `message`, `status?` | show the error; `401` ⇒ log in again |

## A2A (when `A2A_ENABLED=true`)

| Route | Auth | Content |
|---|---|---|
| `GET /.well-known/agent-card.json` | public | Agent Card (skills, securitySchemes, JSON-RPC interface `<url>/a2a`) |
| `POST /a2a` | same as `/invocations` (api key + User-Id, or JWT) | JSON-RPC A2A v1: `SendMessage`, `GetTask`, `ListTasks`, `CancelTask` |

`contextId` ⇒ session `a2a-<contextId>`; tasks are keyed by user; HITL ⇒ `TASK_STATE_INPUT_REQUIRED`, then send `approve` / `reject: <reason>`.
