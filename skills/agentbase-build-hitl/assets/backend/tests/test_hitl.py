from __future__ import annotations

import pytest
from greennode_agentbase import RequestContext
from greennode_agentbase.exceptions import GreenNodeRequestError
from langchain_core.messages import AIMessage, ToolMessage

from app import service
from app.config import get_settings


@pytest.fixture(autouse=True)
def _hitl_env(monkeypatch):
    monkeypatch.setenv("HITL_TOOLS", '["remember"]')
    get_settings.cache_clear()


def ctx(session="hitl-1"):
    return RequestContext(session_id=session, user_id="u1")


def _call(fact="Likes tea"):
    return AIMessage(
        "I will remember that.",
        tool_calls=[{"name": "remember", "args": {"fact": fact}, "id": "c1"}],
    )


async def test_interrupt_then_approve(fake_llm):
    fake_llm(_call(), AIMessage("Remembered."))
    out = await service.handle({"message": "Remember that I like tea"}, ctx())
    assert out["status"] == "interrupted"
    assert out["interrupt"]["tool_calls"][0]["name"] == "remember"

    # Awaiting approval => new chat is blocked
    with pytest.raises(GreenNodeRequestError) as e:
        await service.handle({"message": "hello"}, ctx())
    assert e.value.status_code == 409

    out = await service.handle(
        {"type": "resume", "decisions": [{"tool_call_id": "c1", "action": "approve"}]}, ctx()
    )
    assert out["status"] == "success"
    assert out["tools_used"] == ["remember"]


async def test_edit_args(fake_llm):
    model = fake_llm(_call("wrong"), AIMessage("ok"))
    await service.handle({"message": "remember"}, ctx("hitl-2"))
    await service.handle(
        {
            "type": "resume",
            "decisions": [{"tool_call_id": "c1", "action": "edit", "args": {"fact": "correct"}}],
        },
        ctx("hitl-2"),
    )
    tool_msgs = [m for m in model.calls[-1] if isinstance(m, ToolMessage)]
    assert "correct" in tool_msgs[-1].content


async def test_reject_returns_error_tool_message(fake_llm):
    model = fake_llm(_call(), AIMessage("Understood, I won't save it."))
    await service.handle({"message": "remember"}, ctx("hitl-3"))
    out = await service.handle(
        {
            "type": "resume",
            "decisions": [{"tool_call_id": "c1", "action": "reject", "reason": "not wanted"}],
        },
        ctx("hitl-3"),
    )
    assert out["status"] == "success"
    tool_msgs = [m for m in model.calls[-1] if isinstance(m, ToolMessage)]
    assert tool_msgs[-1].status == "error" and "not wanted" in tool_msgs[-1].content


async def test_resume_without_pending_is_409(fake_llm):
    fake_llm(AIMessage("ok"))
    with pytest.raises(GreenNodeRequestError) as e:
        await service.handle(
            {"type": "resume", "decisions": [{"tool_call_id": "x", "action": "approve"}]},
            ctx("hitl-4"),
        )
    assert e.value.status_code == 409


async def test_stale_interrupt_id_is_409(fake_llm):
    """Double-submit / stale tab: a resume naming an interrupt that is not the pending one is refused."""
    fake_llm(_call(), AIMessage("Remembered."))
    out = await service.handle({"message": "remember"}, ctx("hitl-6"))
    interrupt_id = out["interrupt"]["id"]
    assert interrupt_id
    approve = [{"tool_call_id": "c1", "action": "approve"}]
    with pytest.raises(GreenNodeRequestError) as e:
        await service.handle(
            {"type": "resume", "decisions": approve, "interrupt_id": "stale-id"}, ctx("hitl-6")
        )
    assert e.value.status_code == 409 and "interrupt_id" in e.value.message
    out = await service.handle(
        {"type": "resume", "decisions": approve, "interrupt_id": interrupt_id}, ctx("hitl-6")
    )
    assert out["status"] == "success" and out["tools_used"] == ["remember"]
    with pytest.raises(GreenNodeRequestError) as e:  # the same approval submitted again
        await service.handle(
            {"type": "resume", "decisions": approve, "interrupt_id": interrupt_id}, ctx("hitl-6")
        )
    assert e.value.status_code == 409


def test_placeholder_and_waiting_detection():
    from types import SimpleNamespace

    from app.hitl import is_placeholder_tool_message, is_waiting_approval

    ph = ToolMessage(
        "Tool call 'remember' with id 'c1' was interrupted before completion.",
        tool_call_id="c1",
        status="error",
    )
    assert is_placeholder_tool_message(ph)
    assert not is_placeholder_tool_message(ToolMessage("Remembered", tool_call_id="c1"))
    # AgentBaseMemoryEvents may return interrupts=() but next=('approval',)
    assert is_waiting_approval(SimpleNamespace(interrupts=(), next=("approval",)))
    assert not is_waiting_approval(SimpleNamespace(interrupts=(), next=()))


async def test_invalid_edit_is_rejected_not_executed(fake_llm):
    """`remember` takes {"fact": str}; an edit with the wrong shape must not reach the tool."""
    model = fake_llm(_call("x"), AIMessage("ok"))
    await service.handle({"message": "remember"}, ctx("hitl-5"))
    await service.handle(
        {
            "type": "resume",
            "decisions": [{"tool_call_id": "c1", "action": "edit", "args": {"wrong": 1}}],
        },
        ctx("hitl-5"),
    )
    tool_msgs = [m for m in model.calls[-1] if isinstance(m, ToolMessage)]
    assert (
        tool_msgs[-1].status == "error" and "Edited arguments are invalid" in tool_msgs[-1].content
    )


def test_edited_args_checked_against_mcp_json_schema():
    """MCP tools carry a JSON-Schema dict the adapter does not enforce — validate it ourselves."""
    from langchain_core.tools import StructuredTool

    from app.hitl import edited_args_error

    async def _noop(**kwargs):
        return "ok"

    schema = {
        "type": "object",
        "properties": {"amount": {"type": "integer"}, "to": {"type": "string"}},
        "required": ["amount", "to"],
        "additionalProperties": False,
    }
    tool = StructuredTool(name="pay", description="pay", args_schema=schema, coroutine=_noop)
    assert edited_args_error(tool, {"amount": 10, "to": "bob"}) is None
    assert edited_args_error(tool, {"amount": "lots", "to": "attacker"})
    assert edited_args_error(tool, {})
    assert edited_args_error(tool, {"amount": 1, "to": "b", "extra": True})


async def test_unmatched_hitl_pattern_is_reported(monkeypatch, caplog):
    import app.tools as tools_mod
    from app.auth.inbound import Principal
    from app.tools import collect_tools

    tools_mod._reported_unmatched.clear()  # warned once per pattern per process
    monkeypatch.setenv("HITL_TOOLS", '["remember", "github_create-issue"]')
    get_settings.cache_clear()
    await collect_tools(get_settings(), Principal(user_id="u1"))
    assert "github_create-issue" in caplog.text and "approval NOT enforced" in caplog.text


# --------------------------------------------------------------------------- pending approval blocks chat (409)
async def test_chat_while_pending_returns_the_pending_interrupt(fake_llm):
    """409 carries details.interrupt = the same object as the interrupted response ⇒ the client re-shows it."""
    fake_llm(_call(), AIMessage("Remembered."))
    out = await service.handle({"message": "remember"}, ctx("hitl-c1"))
    with pytest.raises(GreenNodeRequestError) as e:
        await service.handle({"message": "hello"}, ctx("hitl-c1"))
    assert e.value.status_code == 409
    assert e.value.details["interrupt"] == out["interrupt"]
    events = await _stream({"message": "hello"}, "hitl-c1")
    assert events[-1]["event"] == "error" and events[-1]["status"] == 409
    assert events[-1]["interrupt"] == out["interrupt"]


async def _stream(payload: dict, session: str) -> list[dict]:
    return [e async for e in await service.handle({**payload, "stream": True}, ctx(session))]


def _two_cards(fake_llm):
    """Approving card 1 (remember x) makes the model ask card 2 (remember y)."""
    return fake_llm(
        AIMessage("", tool_calls=[{"name": "remember", "args": {"fact": "x"}, "id": "c1"}]),
        AIMessage("", tool_calls=[{"name": "remember", "args": {"fact": "y"}, "id": "c2"}]),
        AIMessage("Both remembered."),
    )


def _approve(tool_call_id: str, interrupt_id: str | None = None) -> dict:
    body = {"type": "resume", "decisions": [{"tool_call_id": tool_call_id, "action": "approve"}]}
    return body | ({"interrupt_id": interrupt_id} if interrupt_id else {})


async def test_replayed_first_decision_is_not_applied_to_the_next_card(fake_llm):
    """Double-tap / retried resume of card 1, serialized behind the session lock, used to be applied to card 2
    (c2 absent from the decisions ⇒ silently rejected). It must be a 409 and card 2 must stay pending."""
    _two_cards(fake_llm)
    first = await service.handle({"message": "remember x then y"}, ctx("hitl-r1"))
    second = await service.handle(_approve("c1", first["interrupt"]["id"]), ctx("hitl-r1"))
    assert second["status"] == "interrupted"
    card2 = second["interrupt"]
    assert card2["id"] != first["interrupt"]["id"] and card2["tool_calls"][0]["id"] == "c2"
    # with / without interrupt_id
    for replay in (_approve("c1", first["interrupt"]["id"]), _approve("c1")):
        with pytest.raises(GreenNodeRequestError) as e:
            await service.handle(replay, ctx("hitl-r1"))
        assert e.value.status_code == 409 and "already been handled" in e.value.message
        assert e.value.details["interrupt"]["id"] == card2["id"]  # ⇒ the client shows card 2
    events = await _stream(_approve("c1"), "hitl-r1")
    assert events[-1]["event"] == "error" and events[-1]["status"] == 409
    assert events[-1]["interrupt"]["id"] == card2["id"]
    done = await service.handle(_approve("c2", card2["id"]), ctx("hitl-r1"))
    assert done["status"] == "success" and done["tools_used"] == ["remember", "remember"]


async def test_replay_refused_when_the_snapshot_hides_the_interrupt(fake_llm, monkeypatch):
    """AgentBase Memory may not expose the interrupt (no interrupt_id check possible): the decision ids decide."""
    import app.hitl as hitl

    _two_cards(fake_llm)
    await service.handle({"message": "remember x then y"}, ctx("hitl-r2"))
    await service.handle(_approve("c1"), ctx("hitl-r2"))
    # the pending card is then rebuilt from the AIMessage, with id None
    monkeypatch.setattr(hitl, "interrupt_payload", lambda interrupts: None)
    with pytest.raises(GreenNodeRequestError) as e:
        await service.handle(_approve("c1"), ctx("hitl-r2"))
    assert e.value.status_code == 409
    assert e.value.details["interrupt"]["id"] is None
    assert [tc["id"] for tc in e.value.details["interrupt"]["tool_calls"]] == ["c2"]


async def test_approval_node_reasks_instead_of_applying_unknown_ids(fake_llm, monkeypatch):
    """Backstop when the service check is bypassed (race across replicas): the stale decisions are not applied,
    the card is asked again — and stays answerable (raising would leave the stale value in the checkpoint)."""
    _two_cards(fake_llm)
    await service.handle({"message": "remember x then y"}, ctx("hitl-r3"))
    card2 = (await service.handle(_approve("c1"), ctx("hitl-r3")))["interrupt"]
    check = service.stale_decision_ids
    monkeypatch.setattr(service, "stale_decision_ids", lambda *a: set())
    again = await service.handle(_approve("c1"), ctx("hitl-r3"))
    assert again["status"] == "interrupted" and again["interrupt"]["id"] == card2["id"]
    assert [tc["id"] for tc in again["interrupt"]["tool_calls"]] == ["c2"]
    monkeypatch.setattr(service, "stale_decision_ids", check)
    done = await service.handle(_approve("c2", again["interrupt"]["id"]), ctx("hitl-r3"))
    assert done["status"] == "success" and done["tools_used"] == ["remember", "remember"]


def test_pending_approval_rebuilt_from_the_checkpoint():
    from types import SimpleNamespace

    from app.hitl import pending_approval

    ai = AIMessage(
        "Sending now.",
        tool_calls=[
            {"name": "remember", "args": {"fact": "x"}, "id": "c1"},
            {"name": "get_current_time", "args": {}, "id": "c2"},
        ],
    )
    snap = SimpleNamespace(interrupts=(), next=("approval",), values={"messages": [ai]})
    assert pending_approval(snap, get_settings()) == {
        "id": None,
        "type": "tool_approval",
        "tool_calls": [{"id": "c1", "name": "remember", "args": {"fact": "x"}}],
        "message": "Sending now.",
    }
    idle = SimpleNamespace(interrupts=(), next=(), values={"messages": [ai]})
    assert pending_approval(idle, get_settings()) is None


# --------------------------------------------------------------------------- malformed decisions ⇒ 400
@pytest.mark.parametrize(
    "decision",
    [
        {"tool_call_id": "c1", "action": "reject", "reason": 123},
        {"tool_call_id": "c1", "action": "reject", "reason": {"x": 1}},
        {"tool_call_id": "c1", "action": "reject", "reason": "x" * 1001},
        {"tool_call_id": ["c1"], "action": "approve"},
        {"tool_call_id": "c" * 129, "action": "approve"},
        {"tool_call_id": "c1", "action": ["approve"]},
        {"tool_call_id": "c1", "action": "edit"},
        {"tool_call_id": "c1", "action": "edit", "args": {"fact": "x" * 16_001}},
        "approve",
    ],
)
async def test_malformed_decisions_are_400_not_500(fake_llm, decision):
    fake_llm(_call(), AIMessage("ok"))
    await service.handle({"message": "remember"}, ctx("hitl-bad"))
    with pytest.raises(GreenNodeRequestError) as e:
        await service.handle({"type": "resume", "decisions": [decision]}, ctx("hitl-bad"))
    assert e.value.status_code == 400


def test_validate_decisions_normalizes_and_refuses_duplicates():
    from app.hitl import validate_decisions

    raw = [{"tool_call_id": "c1", "action": "approve", "reason": None, "args": {"x": 1}, "y": 2}]
    assert validate_decisions(raw) == [{"tool_call_id": "c1", "action": "approve"}]
    with pytest.raises(ValueError, match="one decision per tool_call_id"):
        validate_decisions([{"tool_call_id": "c1", "action": "approve"}] * 2)


def test_describe_decisions_formats_and_tolerates_odd_values():
    from app.hitl import describe_decisions

    calls = [{"id": "c1", "name": "remember", "args": {"fact": "y"}}]
    edit = [{"tool_call_id": "c1", "action": "edit", "args": {"fact": "x"}}]
    edited = describe_decisions(edit, calls)
    assert edited == '[Approval decision]\n- Edited remember → {"fact": "x"}'
    reject = [{"tool_call_id": "c1", "action": "reject", "reason": " too risky "}]
    assert describe_decisions(reject, calls) == (
        '[Approval decision]\n- Rejected remember({"fact": "y"}) — too risky'
    )
    odd = [{"tool_call_id": ["c1"], "action": "reject", "reason": 123}, {"action": "edit"}]
    assert describe_decisions(odd).startswith("[Approval decision]\n- Rejected ['c1'] — 123")


# --------------------------------------------------------------------------- decisions honored first
async def test_rejected_call_never_runs_when_hitl_tools_changed_before_resume(
    fake_llm, monkeypatch
):
    model = fake_llm(_call("secret plan"), AIMessage("Not saved."))
    await service.handle({"message": "remember"}, ctx("hitl-cfg"))
    monkeypatch.setenv("HITL_TOOLS", "[]")  # redeploy / config change while the card is open
    get_settings.cache_clear()
    reject = {"tool_call_id": "c1", "action": "reject", "reason": "no"}
    out = await service.handle({"type": "resume", "decisions": [reject]}, ctx("hitl-cfg"))
    assert out["status"] == "success" and out["tools_used"] == []
    [tool_msg] = [m for m in model.calls[-1] if isinstance(m, ToolMessage)]
    assert tool_msg.status == "error" and tool_msg.content.startswith("Not executed:")


async def test_rejected_call_message_names_the_user_and_forbids_a_retry(fake_llm):
    """Real run: "Not executed: <reason>" read like a system refusal ⇒ the model re-asked with reworded args."""
    model = fake_llm(_call("Fridays remote"), AIMessage("OK, not saved."))
    await service.handle({"message": "remember"}, ctx("hitl-note"))
    reject = {"tool_call_id": "c1", "action": "reject", "reason": "schedule changes often"}
    await service.handle({"type": "resume", "decisions": [reject]}, ctx("hitl-note"))
    [tool_msg] = [m for m in model.calls[-1] if isinstance(m, ToolMessage)]
    assert tool_msg.content.startswith("Not executed: the user declined this call")
    assert '"schedule changes often"' in tool_msg.content and "Do not call this tool again" in tool_msg.content


async def test_decision_event_lists_every_call_with_its_tool_and_implicit_rejects(
    fake_llm, monkeypatch
):
    import app.hitl as hitl

    events = []
    monkeypatch.setattr(hitl.tracing, "event", lambda name, **kw: events.append((name, kw)))
    fake_llm(
        AIMessage(
            "",
            tool_calls=[
                {"name": "remember", "args": {"fact": "a"}, "id": "c1"},
                {"name": "remember", "args": {"fact": "b"}, "id": "c2"},
            ],
        ),
        AIMessage("ok"),
    )
    await service.handle({"message": "remember a and b"}, ctx("hitl-ev"))
    await service.handle(_approve("c1"), ctx("hitl-ev"))
    [decision] = [kw["output"]["decisions"] for name, kw in events if name == "hitl.decision"]
    assert decision == [
        {"tool_call_id": "c1", "tool": "remember", "action": "approve"},
        {"tool_call_id": "c2", "tool": "remember", "action": "reject", "reason": "no decision"},
    ]
