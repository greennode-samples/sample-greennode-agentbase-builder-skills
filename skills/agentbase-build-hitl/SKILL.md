---
name: agentbase-build-hitl
description: "Human-in-the-loop (HITL) standard for LangGraph AI agents on GreenNode AgentBase: pause the graph with interrupt() before side-effecting tools, persist state in the AgentBase Memory checkpointer, client approves/edits/rejects each tool call via a resume payload, hitl.request/hitl.decision traces in Langfuse, approval card UI in React Native. Use when the agent performs actions needing human confirmation (send email, create/delete data, payments, approvals), needs parameter edits before running, or when adding an approval step to the graph. Also for debugging a missing approval card, 409 on resume, or a write that ran unapproved. Trigger: human in the loop, HITL, approve before running, confirm action, approve tool, interrupt, approval, duyệt trước khi chạy, xác nhận hành động, phê duyệt. DO NOT use for static policy-based tool permissions (use /agentbase-policy via the Gateway)."
---

# Human-in-the-loop

## When to enable

By default **every side-effecting tool** (write, delete, send, money transfer, permission change, expensive API calls) must go through HITL, unless the user confirms in writing that it's not needed. Read-only tools don't need HITL.

- Side-effecting tools exist **only when the user explicitly asked for them** (Agent Spec) — don't add write/send/delete tools "for completeness" and then gate them with HITL.
- **Approval fatigue**: a card for every high-frequency, low-risk call (e.g. "save a note" on every turn) trains users to tap Approve blindly — which defeats HITL for the calls that matter. Gate the risky ones; make low-risk ones reversible instead, or use a dynamic threshold (see *Extensions*).

HITL ≠ Policy: Policy Group (Gateway) **blocks statically** by who/which tool; HITL **asks a human** for each specific call. Use both for sensitive actions.

## Configuration

`.env`: `HITL_TOOLS=["hr_create_leave_request","*_delete_*","send_email"]` — globs over **tool names inside the agent**: MCP tool = `<server key in mcp_servers.json>_<tool name>`, local tool = function name. Check the real names in the `tools.collect.output` trace.

Note the 2 different naming schemes: HITL/eval use the tool name **inside the agent** (`hr_create_leave_request`), while Policy Group uses the action **`<connector>__<tool>`** (`hr__create_leave_request`). Don't mix them up.

## Flow (asset `app/hitl.py`, already wired into graph + service)

```
user: "Create a leave request for Friday"
agent → AIMessage(tool_calls=[create_leave_request{date:...}])
route → approval → interrupt({type: tool_approval, tool_calls, message})
⇒ response {"status":"interrupted","interrupt":{id, type, tool_calls, message}}  (SSE: event "interrupt")
client shows approval card → user Approve / Edit args / Reject (with reason)
client POST {"type":"resume","interrupt_id":"<interrupt.id>","decisions":[{tool_call_id, action, args?, reason?}]}
approval reruns, receives decisions → tools run approved calls (with edited args),
rejected calls ⇒ error ToolMessage "Not executed: the user declined… <reason>" (`rejection_note`) → agent continues answering
⇒ "success" — OR another "interrupted" (the model asks for the next action: a NEW interrupt id)
```

**Client contract**: keep handling `interrupt` until the turn ends with `success` — each card has its own `id`, always echo it as `interrupt_id`. While a card is pending, `chat` in that session returns **409 with `details.interrupt`** (SSE: `error` event with `interrupt`) = the pending card ⇒ re-show it instead of an error.

Rules enforced in code:
- Missing decision for a tool_call ⇒ treated as **reject**. A decision present for a call is honored **first**, even if `HITL_TOOLS` changed between the interrupt and the resume (a rejected call never runs).
- Decisions are validated (400, never 500): `tool_call_id` string ≤ 128, one per call, `action` ∈ approve/edit/reject, `reason` string ≤ 1000 chars (it reaches the LLM), `edit` needs `args` (object, ≤ 16 000 chars JSON); unknown keys are dropped.
- While an interrupt is pending ⇒ a new `chat` in the same session returns **409** with `details.interrupt` (must resume first) — avoids history with orphaned tool_calls.
- `resume` with no pending interrupt ⇒ **409**. A resume for an **earlier card** ⇒ **409 "already been handled"** with `details.interrupt` = the card that is pending now — detected by the `interrupt_id` (when the snapshot exposes the interrupt) **and** by the decision ids: they must name tool calls of the pending card. The id check works without `interrupt_id` (A2A, eval) and with AgentBase Memory hiding the interrupt, so a double-tap/retried "approve card 1" is never applied to card 2 (it used to silently reject card 2's calls). If a stale resume still reaches the `approval` node (race across replicas), the node does not apply it and asks the same card again.
- A session runs only 1 request at a time **per replica** (lock per user+session; a waiter gives up with 409 after `REQUEST_TIMEOUT_S`) ⇒ approving twice in parallel on one replica doesn't run the tool twice.
- **At most once needs the downstream system too.** Replicas share no lock, and a request can end (timeout, client gone, restart) while an approved tool runs. Two guards:
  - **Idempotency key (do this in every side-effect tool):** `idempotency_key(config, tool_call_id)` is the same for the same approved call on any replica and in any retry. Pass it to the system the tool changes (`Idempotency-Key` header, request id, unique constraint) so it applies the change once. Read `tool_call_id` with `Annotated[str, InjectedToolCallId]` and `config: RunnableConfig` (example in `app/hitl.py`). An MCP tool cannot carry it through the adapter: make the MCP server's write itself idempotent (e.g. a unique constraint on the business key, `/agentbase-build-mcp-server`).
  - **Outcome unknown:** when the previous request ended inside the `tools` node (`next == ("tools",)`), the next request of the session closes those calls with an `Outcome unknown:` result (event `hitl.outcome_unknown`): the model is told the action may have run and must not repeat it, and the card is gone (a resume ⇒ 409). Before, the call was left dangling, the bridge's "interrupted" placeholder made the model call it again, and the user approved a 2nd card: the action ran twice (reproduced in `test_request_ending_inside_an_approved_tool_never_reruns_it`).
- `edit` requires `args` to be an object and is validated against the tool's schema in the approval node (`edited_args_error`: Pydantic for local tools, JSON Schema for MCP tools, whose adapter does NOT validate). Invalid ⇒ the call is rejected with `Edited arguments are invalid (…)`, never executed.
- A rejected call's ToolMessage (`rejection_note`) says **the user** declined it (with their reason) and tells the model not to retry it or a reworded variant in that turn. A bare "Not executed: <reason>" made real models open a 2nd card for the same declined request and tell the user "the memory system refused".
- The `approval` node **reruns from the start on resume** ⇒ don't put side effects before `interrupt()` (and `hitl.request` is emitted again on every resume — expected).
- A `HITL_TOOLS` pattern that matches no tool (typo, renamed MCP tool, connector down) logs a WARNING and appears as `tools.collect.output.hitl_unmatched` — approval is NOT enforced for it, so check that list after adding patterns.

## AgentBase Memory caveats (seen & fixed)

`AgentBaseMemoryEvents`: `aget_state()` right after the graph pauses may return `interrupts=()` even though `next=('approval',)`, and when loading a checkpoint the bridge inserts a fake ToolMessage ("…was interrupted before completion.") for the pending tool_call ⇒ the service used to wrongly report "success". The template handles this: the interrupt is taken from `__interrupt__` in the `ainvoke`/`astream` output; awaiting approval ⇔ `interrupts` or `"approval" in next` (`is_waiting_approval`); fake ToolMessages are skipped when counting `tools_used` (`is_placeholder_tool_message`). Verified on the runtime with real memory.

## Extensions

- **Approver differs from requester** (e.g. a manager approves): store `session_id` + `interrupt.id` in your approval system; the approver calls `resume` **with the same session and the session owner's identity** via a BFF, or design the `resume` payload to include `approver` and check permissions in `service`. Record `approver` in the `hitl.decision` event metadata.
- **Approval timeout**: the interrupt lives until the event expires (memory's `eventExpiryDuration`). To cancel proactively ⇒ resume with `reject` + reason `timeout` from a background job.
- **Reviewing the answer content** (not a tool): add a node before `END` calling `interrupt({"type":"answer_review", ...})` — keep the same `interrupt`/`resume` contract.
- **Dynamic thresholds** (only ask when amount > X): change `requires_approval()` to also take `args`.

## Tracing & metrics

- Events `hitl.request` (tool_calls awaiting approval — re-emitted on each resume, the node reruns) and `hitl.decision` (what happens per call: `{tool_call_id, tool, action, reason}`, implicit rejects and invalid edits included as `reject`) sit under the `approval` node; `hitl.stale_decision` (WARNING) when a stale resume reached the node.
- Score `hitl_requested` on the interrupt trace; both traces (interrupt and resume) share the `session_id` ⇒ view in the Sessions tab.
- The root of each trace is the conversation line (session page → Conversation view): the interrupt turn's output is `describe_interrupt` (`[Awaiting approval] - remember({...})`), the resume turn's input is `describe_decisions` (`[Approval decision] - Approved|Rejected|Edited <tool>(<args>) — reason`, tool names read from the checkpoint); raw `interrupt` / `resume` payloads are in root metadata.
- Track reject rate per tool ⇒ reveals which tools/prompts often propose wrong actions.

## Frontend

`/agentbase-build-frontend`: `ApprovalCard` shows tool + args, Approve / Edit (JSON args, validated) / Reject (per-call reason); sends `resume` with `interrupt_id` in the same session, keeps showing cards until `success`, and re-shows the card from a 409's `interrupt`.

## Tests

`tests/test_hitl.py`: interrupt → approve; edit args; reject; resume with nothing pending ⇒ 409; chat while pending ⇒ 409 with the pending `interrupt` (HTTP + SSE); stale / already-used `interrupt_id` ⇒ 409; two consecutive cards with the first decision replayed (with/without `interrupt_id`, HTTP + SSE, snapshot hiding the interrupt, node backstop) ⇒ 409 / re-ask, card 2 still answerable; malformed decisions ⇒ 400; `HITL_TOOLS` changed before resume ⇒ rejected call doesn't run; `hitl.decision` content. A2A replay: `test_concurrent_approvals_never_decide_the_next_card` (`/agentbase-build-a2a`). On the runtime with real AgentBase Memory (already run): interrupt → interleaved chat 409 → other user resume 409 → session owner approves ⇒ tool runs, memory stored for the correct user.

## Official docs

- [memory](https://docs.greennode.ai/ai-stack/agent-base/memory) — LangGraph checkpointer bridge (`AgentBaseMemoryEvents`) that persists interrupted runs
