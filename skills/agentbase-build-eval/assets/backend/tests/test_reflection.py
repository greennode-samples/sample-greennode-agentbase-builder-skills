from __future__ import annotations

from greennode_agentbase import RequestContext
from langchain_core.messages import AIMessage

from app import service
from app.config import get_settings
from evals.evaluators import expected_tools, item_passed, no_error


async def test_reflection_retries_and_removes_bad_answer(fake_llm, monkeypatch):
    monkeypatch.setenv("REFLECTION_ENABLED", "true")
    monkeypatch.setenv("REFLECTION_MAX_RETRIES", "1")
    get_settings.cache_clear()
    model = fake_llm(
        AIMessage("bad answer"),
        AIMessage('{"pass": false, "score": 0.2, "critique": "missing figures"}'),
        AIMessage("good answer"),
        AIMessage('{"pass": true, "score": 0.9, "critique": ""}'),
    )
    out = await service.handle(
        {"message": "question"}, RequestContext(session_id="r1", user_id="u1")
    )
    assert out["response"] == "good answer"
    # The 2nd agent call receives the critique in the system prompt
    assert "missing figures" in model.calls[2][0].content
    # The bad answer has been removed from history
    assert not any(getattr(m, "content", "") == "bad answer" for m in model.calls[-1])


def test_evaluators():
    out = {"status": "success", "response": "x", "tools_used": ["get_current_time"]}
    evals = [
        no_error(output=out),
        expected_tools(output=out, metadata={"expected_tools": ["get_current_time"]}),
    ]
    assert item_passed(evals)
    bad = expected_tools(output=out, metadata={"expected_tools": ["remember"]})
    assert bad.value == 0.0


def test_judge_output_parsed_defensively():
    from app.reflection import _parse

    assert _parse('{"pass": "false", "score": 0.2, "critique": "missing"}')["pass"] is False
    assert _parse('{"pass": false, "score": null}')["score"] == 1.0  # null ⇒ default, no crash
    assert _parse("not json")["pass"] is True
    assert _parse('{"score": "abc"}')["score"] == 1.0
    assert _parse('{"score": 7}')["score"] == 1.0  # clamped to [0, 1]
