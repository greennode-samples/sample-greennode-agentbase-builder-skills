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

// Resume after a HITL interrupt — ALWAYS echo interrupt_id (an earlier card's id ⇒ 409)
{"type": "resume", "stream": false, "interrupt_id": "<interrupt.id>",
 "decisions": [
  {"tool_call_id": "call_1", "action": "approve"},
  {"tool_call_id": "call_2", "action": "edit", "args": {"amount": 100}},
  {"tool_call_id": "call_3", "action": "reject", "reason": "Wrong recipient"}
]}
// one decision per tool_call_id of THIS card (missing ⇒ reject; an id of another card ⇒ 409);
// tool_call_id string ≤ 128, reason string ≤ 1000 chars, edit args = object (≤ 16 000 chars JSON) — else 400

// Feedback on 1 answer (records a user_feedback score on the Langfuse trace)
{"type": "feedback", "trace_id": "…", "feedback_token": "…", "score": 1, "comment": "spot on"}
// score ∈ {-1, 0, 1}; trace_id string ≤ 128; feedback_token comes from the response (HMAC of user+trace) — used by
// another user ⇒ 403
```

**HITL loop**: a `resume` can be answered by **another** `interrupted` (the model asks for the next action — a new `interrupt.id`). Keep handling `interrupt` (show the card, resume with its `id`) until the turn ends with `success`.

## Responses (non-stream)

```jsonc
{"status": "success", "response": "…", "tools_used": ["get_current_time"],
 "session_id": "…", "trace_id": "…", "feedback_token": "…"}

{"status": "interrupted", "session_id": "…", "trace_id": "…", "feedback_token": "…",
 "interrupt": {"id": "…", "type": "tool_approval", "message": "<AI message>",
               "tool_calls": [{"id": "call_1", "name": "gateway_create_ticket", "args": {…}}]}}
```

Errors: HTTP status + `{"error", "error_type", "details"}` (SDK format). `400` invalid payload · `401` missing/invalid token (only the agent's own auth — never a backing service's 401/403) · `403` spoofed user header / feedback token of another user · `409` invalid HITL state — a second request of the same session on the same replica **waits** for the first (per-session lock), it is not rejected · `422` exceeded `MAX_TOOL_ROUNDS` · `502` LLM provider error after all fallbacks, "LLM configuration error (HTTP 401/402/403/404)" (key rejected / plan expired / model not enabled or wrong id — fix the config, retrying won't help), or a backing service (Memory / Identity) rejected the request (provider/SDK details only in the server log) · `503` MaaS/Memory overloaded, IdP/JWKS unreachable or slow (client retries with backoff) · `504` exceeded `REQUEST_TIMEOUT_S` · `500` other errors.

`409` and `details.interrupt`:

| Situation | `details.interrupt` | Client |
|---|---|---|
| `chat` while an approval is pending | the pending card — same object as the `interrupted` response's `interrupt` (`id` may be `null` when the backend cannot see it yet) | **re-show the approval card** (don't show an error) |
| `resume` for an earlier card (double tap, retry, stale tab — wrong `interrupt_id` or decisions for tool calls that are not pending) while another card is pending | the card pending now | replace the card with this one |
| `resume` with nothing pending (already handled / expired) | absent | drop the approval card (retrying only gets 409 again) |

## SSE events (`stream: true`)

Each frame is `data: {json}\n\n`. Clients must parse per the SSE spec (LF/CRLF/CR line endings, multi-line `data:` joined with "\n", flush the last event at EOF), and **ignore events and fields they don't know** (new progress events are added that way, without a contract break):

| event | Fields | Frontend handling |
|---|---|---|
| `status` | `phase`: `thinking` (before the first LLM call, while a reasoning model thinks, after a tool or a reset) · `tool` (a tool is running; `tools` = their names) · `checking` (self-eval gate: the answer is being checked before it is final) — more phases may come; `label`: short English default text (never tool arguments or results) | show a small status line under the pending reply (localize by `phase` / tool name, else show `label`); **clear it when a `token` arrives**. Sent only when it changes. Not in the non-stream response |
| `token` | `data` | append to the answer bubble |
| `tool_start` | `name` | show "using tool …" |
| `tool_end` | `name`, `status` | |
| `reset` | `reason`: `self_eval_retry` (self-eval asked for a new answer) or `llm_fallback` (the primary model failed mid-answer and a backup restarts it) | **clear the displayed text** |
| `interrupt` | same as the `interrupted` response | show the approval card (sample `ApprovalCard`: Approve / Edit — JSON args, validated — / Reject with a per-call reason); resume with its `id`; another `interrupt` may follow |
| `done` | same as the `success` response | finalize the bubble, store `trace_id` + `feedback_token` for feedback. The turn is over: the client may close the stream — work the backend does afterwards (background self-eval, `REFLECTION_MODE`) does not need the connection |
| `error` | `message`, `status?`, `interrupt?` | `interrupt` present (409) ⇒ show that approval card, as for the non-stream 409 above; `409` without it on a resume ⇒ drop the card; `401` ⇒ log in again; otherwise show the error |

Typical streamed turn with a tool: `status{thinking}` → `tool_start` → `status{tool, tools: ["search_knowledge"], label: "Searching the knowledge base…"}` → `tool_end` → `status{thinking}` → `token`… → `done`. With the self-eval gate: … `token`… → `status{checking}` → `done`, or → `reset{self_eval_retry}` → `status{thinking}` → `token`… → `done`.

## A2A (when `A2A_ENABLED=true`)

| Route | Auth | Content |
|---|---|---|
| `GET /.well-known/agent-card.json` | public | Agent Card (skills, securitySchemes, JSON-RPC interface `<url>/a2a`) |
| `POST /a2a` | same as `/invocations` (api key + User-Id, or JWT) | JSON-RPC A2A v1: `SendMessage`, `GetTask`, `ListTasks`, `CancelTask` |

`contextId` ⇒ session `a2a-<contextId>` (a calling agent built from this template sends its own **session id** — never its internal `thread_id`); tasks are keyed by user; HITL ⇒ `TASK_STATE_INPUT_REQUIRED`, then send `approve` / `reject: <reason>`. A decision word with nothing pending ⇒ `TASK_STATE_FAILED` "Nothing is awaiting confirmation" (no LLM call). Each message creates a new task (no task-id continuation).
