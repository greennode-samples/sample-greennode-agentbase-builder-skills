from __future__ import annotations

import asyncio

import pytest
from greennode_agentbase import RequestContext
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

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
    # The 2nd agent call receives the critique in the per-turn context message, not in the static system prompt
    assert "missing figures" not in model.calls[2][0].content
    assert any(
        isinstance(m, SystemMessage) and "missing figures" in m.content for m in model.calls[2][1:]
    )
    # The bad answer has been removed from history
    assert not any(getattr(m, "content", "") == "bad answer" for m in model.calls[-1])
    # retries used up (REFLECTION_MAX_RETRIES=1): the retry answer is final whatever the verdict ⇒ no 2nd judge call
    assert len(model.calls) == 3


async def test_judge_sees_this_turns_tool_results(fake_llm, monkeypatch):
    """Criteria say "accurate to the tool data" — the judge used to get only question + answer ⇒ spurious fails."""
    monkeypatch.setenv("REFLECTION_ENABLED", "true")
    get_settings.cache_clear()
    call = {"name": "get_current_time", "args": {"timezone": "Asia/Ho_Chi_Minh"}, "id": "t1"}
    model = fake_llm(
        AIMessage("", tool_calls=[call]),
        AIMessage("It is 10:00."),
        AIMessage('{"pass": true, "score": 0.9, "critique": ""}'),
    )
    out = await service.handle(
        {"message": "time?"}, RequestContext(session_id="r-tools", user_id="u1")
    )
    assert out["response"] == "It is 10:00."
    judge_input = model.calls[2][1].content
    assert "Tool results this turn:" in judge_input and "get_current_time (success):" in judge_input
    assert "Answer:\nIt is 10:00." in judge_input


def test_tool_results_for_the_judge_are_current_turn_and_truncated():
    from app import reflection

    msgs = [
        HumanMessage("old question"),
        ToolMessage("OLD-RESULT", tool_call_id="o1", name="search"),
        HumanMessage("new question"),
        ToolMessage("r" * 10_000, tool_call_id="n1", name="search"),
        ToolMessage("NEXT", tool_call_id="n2", name="other"),
    ]
    text = reflection.tool_results_text(msgs)
    assert "OLD-RESULT" not in text and "NEXT" in text
    assert len(text) < reflection.TOOL_RESULT_MAX_CHARS + 200


async def test_reflection_skipped_when_user_rejected_a_tool(fake_llm, monkeypatch):
    """The answer explains what was NOT done — judging it "against the tool data" fails it spuriously."""
    monkeypatch.setenv("REFLECTION_ENABLED", "true")
    monkeypatch.setenv("HITL_TOOLS", '["remember"]')
    get_settings.cache_clear()
    model = fake_llm(
        AIMessage("", tool_calls=[{"name": "remember", "args": {"fact": "x"}, "id": "c1"}]),
        AIMessage("OK, I did not save it."),
        AIMessage('{"pass": false, "score": 0.1, "critique": "no data"}'),
    )
    ctx = RequestContext(session_id="r-hitl", user_id="u1")
    out = await service.handle({"message": "remember x"}, ctx)
    assert out["status"] == "interrupted"
    tc_id = out["interrupt"]["tool_calls"][0]["id"]
    done = await service.handle(
        {"type": "resume", "decisions": [{"tool_call_id": tc_id, "action": "reject"}]}, ctx
    )
    assert done["response"] == "OK, I did not save it."
    assert len(model.calls) == 2  # agent + agent — no judge


async def test_judge_timeout_keeps_the_answer(fake_llm, monkeypatch):
    from app import reflection

    monkeypatch.setenv("REFLECTION_ENABLED", "true")
    monkeypatch.setenv("REFLECTION_TIMEOUT_S", "0.05")
    get_settings.cache_clear()
    fake_llm(AIMessage("the answer"))
    events: list[tuple[str, str]] = []
    monkeypatch.setattr(
        reflection.tracing,
        "event",
        lambda name, level="DEFAULT", **kw: events.append((name, level)),
    )

    class _SlowJudge:
        async def ainvoke(self, *a, **k):
            await asyncio.sleep(5)

    monkeypatch.setattr(reflection, "get_llm", lambda task="judge", tools=None: _SlowJudge())
    out = await service.handle({"message": "q"}, RequestContext(session_id="r-slow", user_id="u1"))
    assert out["response"] == "the answer"
    assert ("reflection.judge_timeout", "WARNING") in events


async def test_judge_setup_error_keeps_the_answer(fake_llm, monkeypatch):
    """get_llm("judge") used to run OUTSIDE the try: an invalid judge config ⇒ 500 after the answer was ready."""
    from app import reflection

    monkeypatch.setenv("REFLECTION_ENABLED", "true")
    get_settings.cache_clear()
    fake_llm(AIMessage("the answer"))

    def broken(task="judge", tools=None):
        raise ValueError("LLM_TASK_TIERS[judge]=huge is invalid")

    monkeypatch.setattr(reflection, "get_llm", broken)
    out = await service.handle({"message": "q"}, RequestContext(session_id="r-cfg", user_id="u1"))
    assert out["status"] == "success" and out["response"] == "the answer"


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
    for raw in ('{"score": NaN}', '{"score": Infinity}', '{"score": -Infinity}'):
        # non-finite ⇒ missing (NaN used to reach the self_eval score)
        assert _parse(raw)["score"] == 1.0


async def test_fail_verdict_without_critique_still_retries(fake_llm, monkeypatch):
    """Judge says fail but gives no critique ⇒ must retry, never end the turn with an empty reply."""
    monkeypatch.setenv("REFLECTION_ENABLED", "true")
    monkeypatch.setenv("REFLECTION_MAX_RETRIES", "1")
    get_settings.cache_clear()
    fake_llm(
        AIMessage("bad answer"),
        AIMessage('{"pass": false, "score": 0.4}'),  # no critique
        AIMessage("good answer"),
        AIMessage('{"pass": true, "score": 0.9}'),
    )
    out = await service.handle({"message": "q"}, RequestContext(session_id="r2", user_id="u1"))
    assert out["response"] == "good answer"


# --- eval CI gate: crashing items / judges must count as failures, never vanish
async def test_eval_task_never_raises(monkeypatch):
    from evals import run_eval

    async def boom(*a, **k):
        raise ConnectionError("LLM 502")

    monkeypatch.setattr(run_eval, "run_chat", boom)
    out = await run_eval.make_task("approve")(item={"input": {"message": "hi"}})
    assert out["status"] == "error" and "LLM 502" in out["error"]
    assert no_error(output=out).value == 0.0


async def test_judge_error_scores_zero_instead_of_disappearing(monkeypatch):
    from evals import evaluators

    class _Broken:
        async def ainvoke(self, *a, **k):
            raise TimeoutError("judge down")

    monkeypatch.setattr(evaluators, "get_llm", lambda task: _Broken())
    ev = await evaluators.llm_judge_correctness(
        input={"message": "q"}, output={"response": "a"}, expected_output="a"
    )
    assert ev.name == "correctness" and ev.value == 0.0
    ok = no_error(output={"status": "success", "response": "a"})
    assert not item_passed([ok, ev])


def test_eval_config_error_exit_code(monkeypatch):
    import sys

    from evals import run_eval

    def bad_settings():
        raise ValueError("LLM_MODEL and LLM_API_KEY are required")

    monkeypatch.setattr(run_eval, "get_settings", bad_settings)
    monkeypatch.setattr(run_eval, "load_dotenv", lambda *a, **k: False)
    monkeypatch.setattr(sys, "argv", ["run_eval", "--data", "x.jsonl"])
    assert run_eval.main() == run_eval.EXIT_CONFIG_ERROR


def test_gate_fails_when_langfuse_drops_crashed_items(monkeypatch):
    """Langfuse returns results only for the items that did not raise and computes pass_rate over THOSE
    (here 1/1 = 100%). The CI gate must divide by the dataset size: 1 passed of 4 ⇒ 25% < 50% ⇒ exit 1."""
    import sys
    from types import SimpleNamespace

    from langfuse import Evaluation

    from evals import run_eval

    passed = SimpleNamespace(evaluations=[no_error(output={"status": "success", "response": "x"})])
    result = SimpleNamespace(
        item_results=[passed],
        run_evaluations=[Evaluation(name="pass_rate", value=1.0)],
        format=lambda: "",
    )
    dataset = SimpleNamespace(items=[object()] * 4, run_experiment=lambda **kw: result)
    client = SimpleNamespace(auth_check=lambda: True, get_dataset=lambda name: dataset)
    monkeypatch.setattr(run_eval.tracing, "init_tracing", lambda s: None)
    monkeypatch.setattr(run_eval.tracing, "get_client", lambda: client)
    monkeypatch.setattr(run_eval.tracing, "shutdown_tracing", lambda: None)
    monkeypatch.setattr(run_eval, "load_dotenv", lambda *a, **k: False)
    monkeypatch.setattr(sys, "argv", ["run_eval", "--dataset", "d", "--min-pass-rate", "0.5"])
    assert run_eval.main() == run_eval.EXIT_GATE_FAILED


# --- REFLECTION_MODE: gate (the user waits) vs background (after the response, score only)
def _reflection_env(monkeypatch, **kv):
    monkeypatch.setenv("REFLECTION_ENABLED", "true")
    for k, v in kv.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()


@pytest.mark.parametrize(
    ("env", "state", "expected"),
    [
        ({}, {"llm_task": "agent"}, ("gate", "short_answer")),
        ({}, {"llm_task": "agent_complex"}, ("background", "reasoning_tier")),
        ({"LLM_TASK_TIERS": '{"agent": "reasoning"}'}, {}, ("background", "reasoning_tier")),
        ({}, {"answer": "x" * 1201}, ("background", "long_answer")),
        ({"REFLECTION_GATE_MAX_CHARS": "5000"}, {"answer": "x" * 1201}, ("gate", "short_answer")),
        ({"REFLECTION_MAX_RETRIES": "0"}, {}, ("background", "no_retries")),
        ({"REFLECTION_MODE": "gate"}, {"llm_task": "agent_complex"}, ("gate", "configured")),
        ({"REFLECTION_MODE": "background"}, {}, ("background", "configured")),
        ({}, {"llm_task": "agent_simple"}, None),  # REFLECTION_SKIP_SIMPLE
        ({}, {"reflection_round": 1}, None),  # retries used up
        ({"REFLECTION_ENABLED": "false"}, {}, None),
    ],
)
def test_reflection_plan(monkeypatch, env, state, expected):
    from app import reflection

    _reflection_env(monkeypatch, **env)
    answer = state.pop("answer", "short answer")
    s = {"messages": [HumanMessage("q"), AIMessage(answer)], **state}
    assert reflection.reflection_plan(s, get_settings()) == expected
    assert reflection.should_reflect(s, get_settings()) == (
        expected is not None and expected[0] == "gate"
    )


class _GatedJudge:
    """Judge that answers only once `release` is set — shows what happens while it is still running."""

    def __init__(self, verdict: str = '{"pass": false, "score": 0.3, "critique": "wrong"}'):
        self.release = asyncio.Event()
        self.started = asyncio.Event()
        self.verdict = verdict
        self.configs: list[dict] = []

    async def ainvoke(self, messages, config=None, **k):
        self.configs.append(config or {})
        self.started.set()
        await self.release.wait()
        return AIMessage(self.verdict)


def _record_scores(monkeypatch) -> list[tuple[str, float]]:
    from app import reflection

    scores: list[tuple[str, float]] = []
    monkeypatch.setattr(
        reflection.tracing, "score_trace", lambda name, value, **kw: scores.append((name, value))
    )
    return scores


async def test_background_judge_scores_after_the_response_without_retry(fake_llm, monkeypatch):
    """Long / reasoning answers: the response must not wait for the judge — and a fail can't retry any more."""
    from app import reflection

    _reflection_env(monkeypatch, REFLECTION_MODE="background")
    model = fake_llm(AIMessage("the answer"))
    judge = _GatedJudge()
    monkeypatch.setattr(reflection, "get_llm", lambda task="judge", tools=None: judge)
    scores = _record_scores(monkeypatch)
    ctx = RequestContext(session_id="bg-1", user_id="u1")

    out = await service.handle({"message": "q"}, ctx)
    assert out["status"] == "success" and out["response"] == "the answer"
    assert reflection.background_pending() == 1 and scores == []  # the response did not wait
    await judge.started.wait()
    meta = judge.configs[0]["metadata"]
    assert meta == {"reflection_mode": "background", "reflection_reason": "configured"}
    judge.release.set()
    assert await reflection.drain_background() == 0
    assert scores == [("self_eval", 0.3)]
    assert len(model.calls) == 1  # failed verdict, but no retry: the user already has the answer
    # the answer stays in history (a gate fail would have removed it)
    model = fake_llm(AIMessage("next"))
    await service.handle({"message": "q2"}, ctx)
    assert any(getattr(m, "content", "") == "the answer" for m in model.calls[0])
    await reflection.drain_background()


async def test_background_judge_survives_the_client_closing_the_stream(fake_llm, monkeypatch):
    """The frontend cancels the reader at `done`: the judge must not depend on the connection."""
    from app import reflection

    _reflection_env(monkeypatch, LLM_TASK_TIERS='{"agent": "reasoning"}')  # auto ⇒ background
    fake_llm(AIMessage("reasoned answer"))
    judge = _GatedJudge('{"pass": true, "score": 0.8}')
    monkeypatch.setattr(reflection, "get_llm", lambda task="judge", tools=None: judge)
    scores = _record_scores(monkeypatch)

    gen = await service.handle(
        {"message": "q", "stream": True}, RequestContext(session_id="bg-2", user_id="u1")
    )
    events = []
    async for ev in gen:
        events.append(ev)
        if ev["event"] == "done":
            break
    await gen.aclose()  # client gone
    assert events[-1]["response"] == "reasoned answer"
    assert not any(e.get("phase") == "checking" for e in events)  # nobody waited for a check
    await judge.started.wait()
    judge.release.set()
    await reflection.drain_background()
    assert scores == [("self_eval", 0.8)]


async def test_gate_streams_checking_status_then_reset(fake_llm, monkeypatch):
    _reflection_env(monkeypatch, REFLECTION_MODE="gate")
    fake_llm(
        AIMessage("bad answer"),
        AIMessage('{"pass": false, "score": 0.2, "critique": "missing figures"}'),
        AIMessage("good answer"),
    )
    gen = await service.handle(
        {"message": "q", "stream": True}, RequestContext(session_id="gate-s", user_id="u1")
    )
    events = [e async for e in gen]
    seq = [e.get("phase") or e.get("reason") or e["event"] for e in events if e["event"] != "token"]
    # the user sees "checking" while the judge runs; a fail clears the answer and the agent thinks again
    assert seq == ["thinking", "checking", "self_eval_retry", "thinking", "done"], seq
    assert events[-1]["response"] == "good answer"


async def test_background_judges_are_bounded(fake_llm, monkeypatch):
    from app import reflection

    _reflection_env(
        monkeypatch, REFLECTION_MODE="background", REFLECTION_BACKGROUND_MAX_CONCURRENCY="1"
    )
    fake_llm(AIMessage("answer"))
    judge = _GatedJudge()
    monkeypatch.setattr(reflection, "get_llm", lambda task="judge", tools=None: judge)
    events: list[tuple[str, str]] = []
    monkeypatch.setattr(
        reflection.tracing,
        "event",
        lambda name, level=None, **kw: (
            name.startswith("reflection.") and events.append((name, level))
        ),
    )
    await service.handle({"message": "q"}, RequestContext(session_id="bound-1", user_id="u1"))
    out = await service.handle({"message": "q"}, RequestContext(session_id="bound-2", user_id="u2"))
    assert out["status"] == "success"  # the second answer is simply not scored
    assert events == [("reflection.background_skipped", "WARNING")]
    assert reflection.background_pending() == 1
    judge.release.set()
    await reflection.drain_background()


async def test_background_judge_timeout_and_errors_never_raise(fake_llm, monkeypatch):
    from app import reflection

    _reflection_env(
        monkeypatch, REFLECTION_MODE="background", REFLECTION_BACKGROUND_TIMEOUT_S="0.05"
    )
    fake_llm(AIMessage("answer"))
    events: list[str] = []
    monkeypatch.setattr(
        reflection.tracing,
        "event",
        lambda name, **kw: name.startswith("reflection.") and events.append(name),
    )
    monkeypatch.setattr(reflection, "get_llm", lambda task="judge", tools=None: _GatedJudge())
    await service.handle({"message": "q"}, RequestContext(session_id="bg-t", user_id="u1"))
    await reflection.drain_background()
    assert events == ["reflection.judge_timeout"]

    def broken(task="judge", tools=None):
        raise ValueError("bad judge config")

    monkeypatch.setattr(reflection, "get_llm", broken)
    out = await service.handle({"message": "q"}, RequestContext(session_id="bg-e", user_id="u1"))
    await reflection.drain_background()
    assert out["status"] == "success" and events[-1] == "reflection.judge_failed"


async def test_drain_background_waits_then_cancels(fake_llm, monkeypatch):
    """Shutdown (main.py lifespan): finish what can finish, cancel the rest — never hang the process."""
    from app import reflection

    _reflection_env(monkeypatch, REFLECTION_MODE="background")
    fake_llm(AIMessage("answer"))
    judge = _GatedJudge()
    monkeypatch.setattr(reflection, "get_llm", lambda task="judge", tools=None: judge)
    await service.handle({"message": "q"}, RequestContext(session_id="drain", user_id="u1"))
    await judge.started.wait()
    assert await reflection.drain_background(0.01, cancel=False) == 1  # eval runner: just wait
    assert reflection.background_pending() == 1
    assert await reflection.drain_background(0.01) == 1  # shutdown: cancelled
    assert reflection.background_pending() == 0
