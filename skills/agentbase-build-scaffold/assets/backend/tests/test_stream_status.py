"""SSE `status` events: the progress line the user sees while nothing is streamed yet (a reasoning model can think
for a minute before its first token). Backward compatible: an extra event type clients may ignore."""

from __future__ import annotations

import json

from greennode_agentbase import RequestContext
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage

from app import service
from app.observability.tracing import REASONING_KEY


def _status(events: list[dict]) -> list[dict]:
    return [e for e in events if e["event"] == "status"]


async def test_status_follows_the_turn_and_never_carries_tool_args(fake_llm):
    call = {"name": "get_current_time", "args": {"timezone": "Asia/Ho_Chi_Minh"}, "id": "t1"}
    fake_llm(AIMessage("", tool_calls=[call]), AIMessage("It is 10:00."))
    gen = await service.handle(
        {"message": "time?", "stream": True}, RequestContext(session_id="st-1", user_id="u1")
    )
    events = [e async for e in gen]
    kinds = [e.get("phase") or e["event"] for e in events]
    # thinking at once → tool running (after the model asked for it) → thinking again → done
    assert kinds == ["thinking", "tool_start", "tool", "tool_end", "thinking", "done"], kinds
    (tool,) = [e for e in _status(events) if e["phase"] == "tool"]
    assert tool == {
        "event": "status",
        "phase": "tool",
        "label": "Checking the time…",
        "tools": ["get_current_time"],
    }
    assert "Asia/Ho_Chi_Minh" not in json.dumps(
        _status(events)
    )  # arguments never reach the status line


async def test_non_streamed_response_is_unchanged(fake_llm):
    fake_llm(AIMessage("ok"))
    out = await service.handle({"message": "hi"}, RequestContext(session_id="st-2", user_id="u1"))
    assert set(out) == {
        "status",
        "response",
        "tools_used",
        "session_id",
        "trace_id",
        "feedback_token",
    }


def test_status_is_sent_only_when_it_changes():
    status = service._Status()
    assert status.show("thinking") == [
        {"event": "status", "phase": "thinking", "label": "Thinking…"}
    ]
    assert status.show("thinking") == []  # compress → recall → route → agent: one line
    assert status.show("tool", []) == []  # nothing to run (e.g. every call rejected)
    status.cleared()  # a token replaced it on the client
    assert status.show("thinking") != []


def test_task_starts_map_to_phases():
    status = service._Status()
    ask = AIMessage("", tool_calls=[{"name": "remember", "args": {"fact": "x"}, "id": "a"}])

    def start(name: str, messages: list | None = None) -> list[dict]:
        payload = {"id": "1", "name": name, "input": {"messages": messages or []}, "triggers": []}
        return service._stream_events("tasks", payload, {}, status)

    assert start("approval", [ask]) == []  # an approval card follows, not a progress line
    assert start("tools", [ask])[0]["label"] == "Saving to memory…"
    assert start("reflect")[0]["phase"] == "checking"
    assert start("planner")[0]["phase"] == "thinking"  # custom nodes: thinking
    # task RESULT payloads never produce a status
    assert (
        service._stream_events("tasks", {"id": "1", "name": "tools", "result": {}}, {}, status)
        == []
    )
    # HITL partial rejection: only the calls still to run are announced
    two = AIMessage(
        "",
        tool_calls=[
            {"name": "remember", "args": {}, "id": "a"},
            {"name": "search_knowledge", "args": {}, "id": "b"},
        ],
    )
    rejected = ToolMessage("rejected", tool_call_id="a", name="remember", status="error")
    assert service._pending_tool_names({"messages": [HumanMessage("q"), two, rejected]}) == [
        "search_knowledge"
    ]
    # rejected at the approval card: the tools node only writes the rejection — "Saving to memory…" would be a lie
    state = {"messages": [HumanMessage("q"), two], "hitl_rejected": {"a": "no"}}
    assert service._pending_tool_names(state) == ["search_knowledge"]


async def test_rejected_call_shows_no_tool_status(fake_llm, monkeypatch):
    from app.config import get_settings

    monkeypatch.setenv("HITL_TOOLS", '["remember"]')
    get_settings.cache_clear()
    call = AIMessage("", tool_calls=[{"name": "remember", "args": {"fact": "x"}, "id": "c1"}])
    fake_llm(call, AIMessage("Not saved."))
    ctx = RequestContext(session_id="st-rej", user_id="u1")
    out = await service.handle({"message": "remember x"}, ctx)
    assert out["status"] == "interrupted"
    resume = {"type": "resume", "decisions": [{"tool_call_id": "c1", "action": "reject"}]}
    events = [e async for e in await service.handle({**resume, "stream": True}, ctx)]
    assert [e["phase"] for e in _status(events)] == ["thinking"]  # never "Saving to memory…"
    assert events[-1]["event"] == "done"


def test_reasoning_chunks_bring_back_thinking_after_text():
    """After a fallback `reset` the backup model may think for a while: the client must not stare at nothing."""
    status = service._Status()
    meta = {"langgraph_node": "agent", "langgraph_step": 1}
    streaming: dict = {}
    status.show("thinking")
    thinking = AIMessageChunk("", id="r1", response_metadata={REASONING_KEY: "hmm"})
    assert service._stream_events("messages", (thinking, meta), streaming, status) == []  # shown
    token = service._stream_events(
        "messages", (AIMessageChunk("Hi", id="r1"), meta), streaming, status
    )
    assert token == [{"event": "token", "data": "Hi"}] and status.shown is None
    backup = AIMessageChunk("", id="r2", response_metadata={REASONING_KEY: "again"})
    events = service._stream_events("messages", (backup, meta), streaming, status)
    assert events == [
        {"event": "reset", "reason": "llm_fallback"},
        {"event": "status", "phase": "thinking", "label": "Thinking…"},
    ]


def test_tool_labels():
    assert service.tool_status_label("search_knowledge") == "Searching the knowledge base…"
    assert service.tool_status_label("crm_create-ticket") == "Using crm create ticket…"
    assert service.tool_status_label("ask_billing_agent") == "Asking billing agent…"
    labels = {"crm_create-ticket": "Opening a ticket…"}  # a tool's metadata["status_label"]
    assert service.tool_status_label("crm_create-ticket", labels) == "Opening a ticket…"
    status = service._Status(labels)
    (event,) = status.show("tool", ["crm_create-ticket", "search_knowledge", "crm_create-ticket"])
    assert event["label"] == "Opening a ticket… · Searching the knowledge base…"
    assert event["tools"] == ["crm_create-ticket", "search_knowledge"]


async def test_tool_metadata_label_reaches_the_stream(fake_llm, monkeypatch):
    from langchain_core.tools import tool

    @tool
    def lookup_order(order_id: str) -> str:
        """Look up an order."""
        return "shipped"

    lookup_order.metadata = {"status_label": "Looking up your order…"}

    async def tools(settings, principal):
        return [lookup_order]

    monkeypatch.setattr(service, "collect_tools", tools)
    call = {"name": "lookup_order", "args": {"order_id": "A-1"}, "id": "o1"}
    fake_llm(AIMessage("", tool_calls=[call]), AIMessage("Shipped."))
    gen = await service.handle(
        {"message": "order?", "stream": True}, RequestContext(session_id="st-3", user_id="u1")
    )
    labels = [e["label"] for e in _status([e async for e in gen])]
    assert "Looking up your order…" in labels
