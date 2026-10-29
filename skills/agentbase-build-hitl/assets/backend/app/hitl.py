"""Human-in-the-loop (HITL) — a human approves tool calls before they execute.

Mechanism: LangGraph `interrupt()` + checkpointer. The graph stops at the `approval` node, state is stored
in AgentBase Memory (AgentBaseMemoryEvents); the client sends decisions via the payload
`{"type": "resume", "decisions": [...]}` in the same session => the graph continues exactly where it stopped.

    agent --(tool call matches HITL_TOOLS)--> approval --interrupt--> [client approves] --> tools
    agent --(other tool call)----------------------------------------------------------> tools

Interrupt payload sent to the client:
    {"type": "tool_approval", "tool_calls": [{"id", "name", "args"}], "message": "<AI text>"}
Decision sent back by the client (one decision per tool_call_id; missing => reject):
    {"tool_call_id": "...", "action": "approve" | "edit" | "reject", "args": {...}, "reason": "..."}

Rules:
- HITL_TOOLS is a list of globs on tool names (e.g. ["gateway_*_delete", "send_email"]).
  Apply it by default to side-effecting tools (write/delete/send/payment). Read-only tools need no approval.
- The approval node is re-run FROM THE START on resume => put no side effects before `interrupt()` (the
  `hitl.request` event is therefore emitted again on resume).
- While awaiting approval, new chats in the same session are rejected (409, with the pending `interrupt`).
- Approving may lead to ANOTHER approval request (new interrupt id) — clients keep handling `interrupt` until done.
  Decisions naming tool calls that are not pending (a replayed / double-submitted earlier card) are refused (409),
  never applied to the next card.
- At most once is NOT guaranteed by this module alone: two replicas can resume the same card at the same moment, and
  a request can end (timeout, client gone, restart) while an approved tool runs. So:
  - a side-effect tool passes `idempotency_key(config, tool_call_id)` to the system it changes (Idempotency-Key header,
    request id, unique constraint): the same approved call always gets the same key, on any replica, in any retry;
  - a call left unanswered by an ended request is closed with an "Outcome unknown" result (`unfinished_tool_calls`,
    service._prepare) — the model is told it may have run and must not repeat it, instead of a bare "interrupted"
    that it used to retry (a 2nd card, approved again, ran the action twice).
"""

from __future__ import annotations

import hashlib
import json
from fnmatch import fnmatch
from typing import Any

from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.prebuilt import ToolNode
from langgraph.types import interrupt

from app.config import Settings
from app.observability import tracing

VALID_ACTIONS = {"approve", "edit", "reject"}
REJECTED_PREFIX = "Not executed:"
# Decisions come from the client: bounded, because `reason` and edited `args` reach the LLM (and the trace)
MAX_DECISIONS = 64
MAX_TOOL_CALL_ID_CHARS = 128
MAX_REASON_CHARS = 1000
MAX_EDIT_ARGS_CHARS = 16_000
ALREADY_HANDLED = "This approval request has already been handled"
OUTCOME_UNKNOWN_PREFIX = "Outcome unknown:"


def idempotency_key(config: RunnableConfig, tool_call_id: str) -> str:
    """Stable key of ONE tool call of one user's session: a retried / double-resumed / other-replica run of the same
    approved call gets the same key ⇒ pass it to the downstream API so it applies the change once. In a tool:

        @tool
        async def send_invoice(order_id: str, config: RunnableConfig,
                               tool_call_id: Annotated[str, InjectedToolCallId]) -> str:
            key = idempotency_key(config, tool_call_id)  # e.g. headers={"Idempotency-Key": key}
    """
    c = config.get("configurable") or {}
    raw = json.dumps([c.get("actor_id"), c.get("session_id"), tool_call_id])
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def is_rejected_tool_message(m: Any) -> bool:
    """ToolMessage written for a call the user rejected (the tool never ran)."""
    return (
        isinstance(m, ToolMessage)
        and m.status == "error"
        and isinstance(m.content, str)
        and m.content.startswith(REJECTED_PREFIX)
    )


def requires_approval(tool_name: str, settings: Settings) -> bool:
    return any(fnmatch(tool_name, pattern) for pattern in settings.hitl_tools)


def last_ai(messages: list) -> AIMessage | None:
    for m in reversed(messages):
        if isinstance(m, AIMessage):
            return m
    return None


def validate_decisions(decisions: Any) -> list[dict]:
    """Type-checked, normalized copy (documented keys only): a malformed decision is a 400 (ValueError), never
    a 500 further down (unhashable id, `reason: 123` …)."""
    if not isinstance(decisions, list) or not decisions:
        raise ValueError("decisions must be a non-empty list")
    if len(decisions) > MAX_DECISIONS:
        raise ValueError(f"at most {MAX_DECISIONS} decisions")
    out: list[dict] = []
    for d in decisions:
        if not isinstance(d, dict):
            raise ValueError("each decision must be an object")
        tool_call_id = d.get("tool_call_id")
        if not isinstance(tool_call_id, str) or not 0 < len(tool_call_id) <= MAX_TOOL_CALL_ID_CHARS:
            raise ValueError(
                f"each decision needs a tool_call_id (string, max {MAX_TOOL_CALL_ID_CHARS} chars)"
            )
        if any(o["tool_call_id"] == tool_call_id for o in out):
            raise ValueError("one decision per tool_call_id")
        action = d.get("action")
        if not isinstance(action, str) or action not in VALID_ACTIONS:
            raise ValueError(f"action must be one of {sorted(VALID_ACTIONS)}")
        reason = d.get("reason")
        if reason is not None and (not isinstance(reason, str) or len(reason) > MAX_REASON_CHARS):
            raise ValueError(f"reason must be a string (max {MAX_REASON_CHARS} chars)")
        clean: dict[str, Any] = {"tool_call_id": tool_call_id, "action": action}
        if reason:
            clean["reason"] = reason
        if action == "edit":
            args = d.get("args")
            if not isinstance(args, dict):
                raise ValueError("action=edit requires args (object)")
            if len(json.dumps(args, ensure_ascii=False, default=str)) > MAX_EDIT_ARGS_CHARS:
                raise ValueError(f"edited args exceed {MAX_EDIT_ARGS_CHARS} chars")
            clean["args"] = args
        out.append(clean)
    return out


def rejection_note(decision: dict | None, reason: str) -> str:
    """What the LLM reads for a call that did not run. It says WHO decided: a bare "Not executed: <reason>" reads
    like a tool/system refusal ⇒ models retried with reworded args (a 2nd card for a request the user already
    declined) and told the user "the system refused"."""
    if decision is None:
        why = "no approval decision was received for this call, so it was treated as declined"
    elif decision["action"] == "edit":
        why = f"the user edited the arguments but they are invalid — {reason}"
    elif decision.get("reason"):
        why = f'the user declined this call (their own decision, not a system error). Their reason: "{reason}"'
    else:
        why = "the user declined this call (their own decision, not a system error)"
    return (
        f"{REJECTED_PREFIX} {why}. Do not call this tool again, or a reworded variant of the call, in this turn "
        "unless the user asks; tell the user plainly that it was not done."
    )


def stale_decision_ids(decisions: list[dict], pending_tool_calls: list[dict]) -> set[str]:
    """Decision ids that are not pending. Non-empty ⇒ the decisions were made for another (earlier) card."""
    pending = {tc.get("id") for tc in pending_tool_calls}
    return {d["tool_call_id"] for d in decisions} - pending


def edited_args_error(tool: Any, args: dict) -> str | None:
    """Why `args` don't fit the tool's input schema (None = valid). Uses `tool_call_schema` — the schema the LLM
    sees, without injected args. MCP tools carry a JSON-Schema dict that the adapter does NOT enforce, so an edit
    like {"amount": "lots"} would otherwise reach the MCP server unchecked."""
    schema = getattr(tool, "tool_call_schema", None) if tool is not None else None
    if schema is None:
        return None
    try:
        if isinstance(schema, dict):
            import jsonschema

            jsonschema.validate(args, schema)
        else:
            schema.model_validate(args)
    except Exception as e:  # noqa: BLE001 — jsonschema.ValidationError / pydantic.ValidationError
        return (str(getattr(e, "message", "")) or str(e)).splitlines()[0][:200]
    return None


def build_approval_node(settings: Settings, tools: list | None = None):
    by_name = {t.name: t for t in tools or []}

    async def approval(state: dict) -> dict:
        ai = last_ai(state["messages"])
        pending = [
            {"id": tc["id"], "name": tc["name"], "args": tc["args"]}
            for tc in ai.tool_calls
            if requires_approval(tc["name"], settings)
        ]
        # Re-emitted on every resume: the node re-runs from the start (see module docstring)
        tracing.event("hitl.request", input={"tool_calls": pending})
        card = {"type": "tool_approval", "tool_calls": pending, "message": ai.text}
        decisions = validate_decisions(interrupt(card))
        # Last line of defense (the service refuses these first, but cannot across replicas): decisions naming calls
        # that are not in THIS message belong to an earlier card ⇒ never applied; the card is asked again. Raising
        # instead would leave the stale resume value in the checkpoint, and every later resume would hit it.
        while stale := stale_decision_ids(decisions, ai.tool_calls):
            tracing.event(
                "hitl.stale_decision", level="WARNING", metadata={"tool_call_ids": sorted(stale)}
            )
            decisions = validate_decisions(interrupt(card))
        by_id = {d["tool_call_id"]: d for d in decisions}

        new_calls, rejected, resolved = [], {}, []
        for tc in ai.tool_calls:
            # A decision for this call is honored FIRST: if HITL_TOOLS changed between the interrupt and the
            # resume, a call the user rejected must still not run.
            d = by_id.get(tc["id"])
            if d is None:
                if not requires_approval(tc["name"], settings):
                    new_calls.append(tc)
                    continue
                d = {"action": "reject", "reason": "no decision"}
            action, reason = d["action"], None
            if action == "edit":
                if err := edited_args_error(by_name.get(tc["name"]), d["args"]):
                    action, reason = "reject", f"Edited arguments are invalid ({err})"
                    tracing.event(
                        "hitl.invalid_edit", level="WARNING", metadata={"tool": tc["name"]}
                    )
                else:
                    tc = {**tc, "args": d["args"]}
            elif action == "reject":
                reason = d.get("reason") or "Rejected by the user"
            if reason is not None:
                rejected[tc["id"]] = rejection_note(by_id.get(tc["id"]), reason)
            resolved.append(
                {"tool_call_id": tc["id"], "tool": tc["name"], "action": action}
                | ({"reason": reason} if reason else {})
            )
            new_calls.append(tc)
        # What actually happens per call (incl. implicit rejects) — not the raw client payload
        tracing.event("hitl.decision", output={"decisions": resolved})
        # Same id => add_messages REPLACES the old AIMessage (keeps the edited args)
        updated = AIMessage(content=ai.content, tool_calls=new_calls, id=ai.id)
        return {"messages": [updated], "hitl_rejected": rejected}

    return approval


def build_tools_node(tools: list):
    """ToolNode wrapped with handling for rejected tool calls (returns an error ToolMessage to the LLM)."""
    tool_node = ToolNode(tools, handle_tool_errors=True)

    async def run_tools(state: dict, config: RunnableConfig) -> dict:
        ai = last_ai(state["messages"])
        rejected: dict[str, str] = state.get("hitl_rejected") or {}
        to_run = [tc for tc in ai.tool_calls if tc["id"] not in rejected]
        out: list = []
        if to_run:
            result = await tool_node.ainvoke(
                {**state, "messages": [AIMessage(content="", tool_calls=to_run)]}, config
            )
            out.extend(result["messages"])
        for tc in ai.tool_calls:
            if tc["id"] in rejected:
                out.append(
                    ToolMessage(
                        content=rejected[tc["id"]],
                        tool_call_id=tc["id"],
                        name=tc["name"],
                        status="error",
                    )
                )
        return {"messages": out, "hitl_rejected": {}}

    return run_tools


def unfinished_tool_calls(snapshot: Any) -> list[dict]:
    """Calls of the last AI message the `tools` node never answered: the graph stopped INSIDE the tools node (request
    timeout, client gone, replica restart) — `next == ("tools",)`. They may or may not have executed."""
    if "tools" not in (getattr(snapshot, "next", None) or ()):
        return []
    messages = (getattr(snapshot, "values", None) or {}).get("messages", [])
    ai = last_ai(messages)
    answered = {m.tool_call_id for m in messages if isinstance(m, ToolMessage)}
    return [tc for tc in (ai.tool_calls if ai else []) if tc["id"] not in answered]


def outcome_unknown_messages(calls: list[dict], rejected: dict[str, str]) -> list[ToolMessage]:
    """Results closing `unfinished_tool_calls`: rejected calls never ran (their rejection note), the others may have."""
    note = (
        f"{OUTCOME_UNKNOWN_PREFIX} the request ended while this call was running, so it may or may not have been "
        "executed. Do not call it again, or a variant of it, in this turn; tell the user plainly that it may already "
        "have been done and ask them to check before trying again."
    )
    return [
        ToolMessage(
            content=rejected.get(tc["id"], note),
            tool_call_id=tc["id"],
            name=tc["name"],
            status="error",
        )
        for tc in calls
    ]


def interrupt_payload(interrupts: Any) -> dict | None:
    """Normalize an Interrupt (from ainvoke/astream `__interrupt__`, or a snapshot) for the client."""
    if not interrupts:
        return None
    first = interrupts[0]
    return {"id": getattr(first, "id", None), **(getattr(first, "value", None) or {})}


def pending_approval(snapshot: Any, settings: Settings) -> dict | None:
    """The approval request pending in this checkpoint — same shape as the interrupted response's `interrupt`
    ({id, type, tool_calls, message}) — or None. AgentBase Memory may not expose the interrupt right after the
    pause (see is_waiting_approval) ⇒ rebuilt from the last AIMessage then, with `id` None."""
    if not is_waiting_approval(snapshot):
        return None
    if payload := interrupt_payload(getattr(snapshot, "interrupts", None)):
        return payload
    ai = last_ai((getattr(snapshot, "values", None) or {}).get("messages", []))
    calls = ai.tool_calls if ai else []
    # `or calls`: HITL_TOOLS changed since the interrupt — the card must still list what awaits a decision
    awaiting = [tc for tc in calls if requires_approval(tc["name"], settings)] or calls
    return {
        "id": None,
        "type": "tool_approval",
        "tool_calls": [{"id": tc["id"], "name": tc["name"], "args": tc["args"]} for tc in awaiting],
        "message": ai.text if ai else "",
    }


def _call_text(tool_call: dict) -> str:
    args = json.dumps(tool_call.get("args") or {}, ensure_ascii=False, default=str)
    return f"{tool_call.get('name')}({args})"


def describe_interrupt(interrupt: dict) -> str:
    """Root trace OUTPUT of a turn that stops for approval: readable in the Langfuse session view (the raw
    payload goes to metadata), e.g. "[Awaiting approval]\n- remember({"fact": "…"})"."""
    calls = "\n".join(f"- {_call_text(tc)}" for tc in interrupt.get("tool_calls") or [])
    text = (interrupt.get("message") or "").strip()
    return (f"{text}\n\n" if text else "") + f"[Awaiting approval]\n{calls}"


def describe_decisions(decisions: list[dict], tool_calls: list | None = None) -> str:
    """Root trace INPUT of a resume turn — what the user decided, per tool call (names from the pending
    AIMessage when known), e.g. "[Approval decision]\n- Rejected remember({…}) — reason". Tolerates any value
    types (coerced with str()) — it only renders text."""
    by_id = {str(tc["id"]): tc for tc in tool_calls or [] if isinstance(tc, dict) and tc.get("id")}
    lines = []
    for d in decisions:
        tool_call_id = str(d.get("tool_call_id"))
        tc = by_id.get(tool_call_id)
        call = _call_text(tc) if tc else tool_call_id
        if d.get("action") == "approve":
            lines.append(f"Approved {call}")
        elif d.get("action") == "edit":
            name = tc.get("name") if tc else tool_call_id
            args = d.get("args") if isinstance(d.get("args"), dict) else {}
            lines.append(f"Edited {name} → {json.dumps(args, ensure_ascii=False, default=str)}")
        else:
            reason = str(d.get("reason") or "").strip()
            lines.append(f"Rejected {call}" + (f" — {reason}" if reason else ""))
    return "[Approval decision]\n" + "\n".join(f"- {line}" for line in lines)


def is_waiting_approval(snapshot: Any) -> bool:
    """Whether the session is awaiting approval.

    Seen in practice with AgentBaseMemoryEvents: reading state RIGHT after the graph stops may not show the
    `__interrupt__` write yet (interrupts=()) even though `next=('approval',)`. So rely mainly on `next`.
    """
    return bool(getattr(snapshot, "interrupts", None)) or "approval" in (
        getattr(snapshot, "next", None) or ()
    )


def is_placeholder_tool_message(message: Any) -> bool:
    """AgentBaseMemoryEvents inserts a placeholder ToolMessage for an "orphaned" tool_call when loading a checkpoint
    without an interrupt ("Tool call 'x' with id 'y' was interrupted before completion.")."""
    content = getattr(message, "content", "")
    return (
        getattr(message, "status", None) == "error"
        and isinstance(content, str)
        and content.startswith("Tool call '")
        and content.endswith("was interrupted before completion.")
    )
