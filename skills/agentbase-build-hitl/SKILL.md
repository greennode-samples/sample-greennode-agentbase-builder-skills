---
name: agentbase-build-hitl
description: "Human-in-the-loop (HITL) standard for LangGraph AI agents on GreenNode AgentBase: pause the graph with interrupt() before side-effecting tools, persist state in the AgentBase Memory checkpointer, client approves/edits/rejects each tool call via a resume payload, hitl.request/hitl.decision traces in Langfuse, approval card UI in React Native. Use when the agent performs actions needing human confirmation (send email, create/delete data, payments, approvals), needs parameter edits before running, or when adding an approval step to the graph. Trigger: human in the loop, HITL, approve before running, confirm action, approve tool, interrupt, approval, duyệt trước khi chạy, xác nhận hành động, phê duyệt. DO NOT use for static policy-based tool permissions (use /agentbase-policy via the Gateway)."
---

# Human-in-the-loop

## When to enable

By default **every side-effecting tool** (write, delete, send, money transfer, permission change, expensive API calls) must go through HITL, unless the user confirms in writing that it's not needed. Read-only tools don't need HITL.

HITL ≠ Policy: Policy Group (Gateway) **blocks statically** by who/which tool; HITL **asks a human** for each specific call. Use both for sensitive actions.

## Configuration

`.env`: `HITL_TOOLS=["hr_create_leave_request","*_delete_*","send_email"]` — globs over **tool names inside the agent**: MCP tool = `<server key in mcp_servers.json>_<tool name>`, local tool = function name. Check the real names in the `tools.collect.output` trace.

Note the 2 different naming schemes: HITL/eval use the tool name **inside the agent** (`hr_create_leave_request`), while Policy Group uses the action **`<connector>__<tool>`** (`hr__create_leave_request`). Don't mix them up.

## Flow (asset `app/hitl.py`, already wired into graph + service)

```
user: "Create a leave request for Friday"
agent → AIMessage(tool_calls=[create_leave_request{date:...}])
route → approval → interrupt({type: tool_approval, tool_calls, message})
⇒ response {"status":"interrupted","interrupt":{...}}  (SSE: event "interrupt")
client shows approval card → user Approve / Edit args / Reject (with reason)
client POST {"type":"resume","decisions":[{tool_call_id, action, args?, reason?}]}
approval reruns, receives decisions → tools run approved calls (with edited args),
rejected calls ⇒ error ToolMessage "Not executed: <reason>" → agent continues answering
```

Rules enforced in code:
- Missing decision for a tool_call ⇒ treated as **reject**.
- While an interrupt is pending ⇒ a new `chat` in the same session returns **409** (must resume first) — avoids history with orphaned tool_calls.
- `resume` with no pending interrupt ⇒ **409**; sending an `interrupt_id` that differs from the pending one (double-submit, stale tab) ⇒ **409** — when the snapshot exposes the interrupt; with AgentBase Memory it may not (see caveats), then double-approve protection relies on the per-session lock (single replica) only.
- A session runs only 1 request at a time (lock per user+session) ⇒ approving twice in parallel doesn't run the tool twice.
- `edit` requires `args` to be an object and is validated against the tool's schema in the approval node (`edited_args_error`: Pydantic for local tools, JSON Schema for MCP tools, whose adapter does NOT validate). Invalid ⇒ the call is rejected with `Edited arguments are invalid (…)`, never executed.
- The `approval` node **reruns from the start on resume** ⇒ don't put side effects before `interrupt()`.
- A `HITL_TOOLS` pattern that matches no tool (typo, renamed MCP tool, connector down) logs a WARNING and appears as `tools.collect.output.hitl_unmatched` — approval is NOT enforced for it, so check that list after adding patterns.

## AgentBase Memory caveats (seen & fixed)

`AgentBaseMemoryEvents`: `aget_state()` right after the graph pauses may return `interrupts=()` even though `next=('approval',)`, and when loading a checkpoint the bridge inserts a fake ToolMessage ("…was interrupted before completion.") for the pending tool_call ⇒ the service used to wrongly report "success". The template handles this: the interrupt is taken from `__interrupt__` in the `ainvoke`/`astream` output; awaiting approval ⇔ `interrupts` or `"approval" in next` (`is_waiting_approval`); fake ToolMessages are skipped when counting `tools_used` (`is_placeholder_tool_message`). Verified on the runtime with real memory.

## Extensions

- **Approver differs from requester** (e.g. a manager approves): store `session_id` + `interrupt.id` in your approval system; the approver calls `resume` **with the same session and the session owner's identity** via a BFF, or design the `resume` payload to include `approver` and check permissions in `service`. Record `approver` in the `hitl.decision` event metadata.
- **Approval timeout**: the interrupt lives until the event expires (memory's `eventExpiryDuration`). To cancel proactively ⇒ resume with `reject` + reason `timeout` from a background job.
- **Reviewing the answer content** (not a tool): add a node before `END` calling `interrupt({"type":"answer_review", ...})` — keep the same `interrupt`/`resume` contract.
- **Dynamic thresholds** (only ask when amount > X): change `requires_approval()` to also take `args`.

## Tracing & metrics

- Events `hitl.request` (tool_calls awaiting approval) and `hitl.decision` (the decision) sit under the `approval` node.
- Score `hitl_requested` on the interrupt trace; both traces (interrupt and resume) share the `session_id` ⇒ view in the Sessions tab.
- Track reject rate per tool ⇒ reveals which tools/prompts often propose wrong actions.

## Frontend

`/agentbase-build-frontend`: `ApprovalCard` shows tool + args, Approve / Reject buttons, (optional) Edit JSON; sends `resume` in the same session.

## Tests

`tests/test_hitl.py`: interrupt → approve; edit args; reject; resume with nothing pending ⇒ 409; chat while pending ⇒ 409; stale / already-used `interrupt_id` ⇒ 409 (in-memory checkpointer, which exposes the interrupt). On the runtime with real AgentBase Memory (already run): interrupt → interleaved chat 409 → other user resume 409 → session owner approves ⇒ tool runs, memory stored for the correct user.

## Official docs

- [memory](https://docs.greennode.ai/ai-stack/agent-base/memory) — LangGraph checkpointer bridge (`AgentBaseMemoryEvents`) that persists interrupted runs
