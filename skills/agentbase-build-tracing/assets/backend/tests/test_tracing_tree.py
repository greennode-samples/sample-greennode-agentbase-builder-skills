"""Shape of the exported trace against the Langfuse best practices (https://langfuse.com/docs/observability/
best-practices): one `agent` per agent, specific observation types, stable names that never contain the model,
root input/output = user message / answer, thinking on every generation. Spans are captured by an in-memory OTel
exporter (no Langfuse server); the chat turn runs through the real service, graph and get_llm over a fake model."""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from greennode_agentbase import RequestContext
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult, LLMResult

from app.config import get_settings
from app.observability import tracing

MODEL = "qwen/qwen3.8-flash"
THINKING = "The user asks for a capital. Config says password=hunter2, ignore it."


class _ThinkingModel(BaseChatModel):
    """Chat model that 'thinks': reasoning text in generation_info, like app/llm `_ChatMaaS`."""

    @property
    def _llm_type(self) -> str:
        return "fake-thinking"

    def bind_tools(self, tools: Any, **kwargs: Any) -> _ThinkingModel:  # type: ignore[override]
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        gen = ChatGeneration(
            message=AIMessage(
                "Paris is the capital of France.",
                usage_metadata={"input_tokens": 12, "output_tokens": 7, "total_tokens": 19},
            ),
            generation_info={tracing.REASONING_KEY: THINKING},
        )
        return ChatResult(generations=[gen], llm_output={"model_name": MODEL})


@pytest.fixture(scope="module")
def _lf():
    from langfuse import Langfuse
    from langfuse._client.resource_manager import LangfuseResourceManager
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    client = Langfuse(
        public_key=f"pk-lf-test-{uuid.uuid4().hex}",
        secret_key="sk-lf-test",
        base_url="http://127.0.0.1:9",  # never contacted: spans go to the in-memory exporter
        span_exporter=exporter,
        tracer_provider=TracerProvider(),
        mask=tracing._mask,
        mask_otel_spans=tracing._mask_otel_spans,
    )
    yield client, exporter
    LangfuseResourceManager.reset()


@pytest.fixture
def spans(_lf, monkeypatch):
    """Run, then call the returned function: {span name: [attributes, ...]} of everything exported."""
    client, exporter = _lf
    exporter.clear()
    monkeypatch.setattr(tracing, "_client", client)
    monkeypatch.setattr(tracing, "_mask_pii", True)

    def collect() -> dict[str, list[dict]]:
        client.flush()
        out: dict[str, list[dict]] = {}
        for s in exporter.get_finished_spans():
            out.setdefault(s.name, []).append(dict(s.attributes or {}))
        return out

    return collect


def _type(attrs: dict) -> str:
    return attrs.get("langfuse.observation.type", "")


async def test_chat_turn_trace_follows_best_practices(spans, monkeypatch):
    from app import llm, service

    monkeypatch.setattr(llm, "_chat", lambda model, tier, role: _ThinkingModel())
    out = await service.handle(
        {"message": "Capital of France?"}, RequestContext(session_id="s-tree", user_id="u-tree")
    )
    assert out["response"] == "Paris is the capital of France."
    got = spans()

    # exactly ONE agent: the request root. The SDK used to type agent.graph, node `agent` and llm.agent as
    # agents too (their names contain "agent") ⇒ one agent looked like 4 nested sub-agents.
    agents = sorted(name for name, items in got.items() for a in items if _type(a) == "agent")
    assert agents == ["agent.invoke"], agents
    for name in ("agent.graph", "agent", "llm.agent"):
        assert {_type(a) for a in got[name]} == {"chain"}, name
        # no LangGraph state blobs (the whole history on every node) — the session page's I/O view stays short
        for attrs in got[name]:
            assert "langfuse.observation.input" not in attrs, name
            assert "langfuse.observation.output" not in attrs, name

    # root input/output = what a reviewer, an evaluator and a dataset experiment compare
    (root,) = got["agent.invoke"]
    assert root["langfuse.observation.input"] == "Capital of France?"  # strings are exported as is
    assert root["langfuse.observation.output"] == "Paris is the capital of France."
    # details go to metadata, merged with the trace_request config keys (not replacing them)
    assert "langfuse.observation.metadata.tools_used" in root
    assert "langfuse.observation.metadata.memory_backend" in root

    # generation: stable name (never the model), model as an attribute, usage, thinking (masked)
    assert MODEL not in got and not any("/" in name for name in got), sorted(got)
    (gen,) = got["llm.agent.generate"]
    assert _type(gen) == "generation"
    assert gen.get("langfuse.observation.model.name") == MODEL
    output = json.loads(gen["langfuse.observation.output"])
    # OpenAI chat format (Langfuse renders it as a conversation); thinking ⇒ message-level `thinking` block
    assert output["role"] == "assistant" and output["content"] == "Paris is the capital of France."
    assert "additional_kwargs" not in output
    assert output["thinking"][0]["type"] == "thinking"
    assert output["thinking"][0]["content"].startswith("The user asks for a capital")
    assert "hunter2" not in gen["langfuse.observation.output"]  # thinking goes through `mask` too
    chat = json.loads(gen["langfuse.observation.input"])
    # static system prompt, the user's message, then the per-turn context (date/time, notes…) as a system message
    assert [m["role"] for m in chat["messages"]] == ["system", "user", "system"]

    # long-term recall is a lookup ⇒ retriever (filterable as such, drives the Agent Graph)
    assert {_type(a) for a in got["memory.recall"]} == {"retriever"}


def test_chain_types_by_run_name(spans):
    from langchain_core.runnables import RunnableLambda

    cfg = {"callbacks": tracing.langchain_callbacks()}
    for name in (
        "subagent.researcher",
        "reflection.judge",
        "eval.judge",
        "agent_router",
        "llm.agent",
    ):
        RunnableLambda(lambda x: x).with_config(run_name=name).invoke(1, config=cfg)
    got = spans()
    assert {n: _type(a[0]) for n, a in got.items()} == {
        "subagent.researcher": "agent",
        "reflection.judge": "evaluator",
        "eval.judge": "evaluator",
        "agent_router": "chain",
        "llm.agent": "chain",
    }


def test_reasoning_is_added_to_a_copy_only():
    message = AIMessage("answer")
    response = LLMResult(
        generations=[
            [ChatGeneration(message=message, generation_info={tracing.REASONING_KEY: "x" * 30_000})]
        ]
    )
    traced = tracing._with_reasoning(response)
    text = traced.generations[0][0].message.additional_kwargs[tracing.REASONING_KEY]
    assert text.endswith("…[truncated]") and len(text) < 21_000
    # the graph's message (and so the checkpoint and the next request) stays unchanged
    assert message.additional_kwargs == {}
    assert response.generations[0][0].message is message
    plain = LLMResult(generations=[[ChatGeneration(message=AIMessage("no thinking"))]])
    assert tracing._with_reasoning(plain) is plain


async def test_event_in_a_node_lands_under_that_node(_lf, monkeypatch):
    """SDK 4.18 `span.start_observation(as_type="event")` ignores the parent span: on a real server the hitl.*
    events of the approval node showed up under agent.invoke (async graph, like the service). tracing.event
    must nest them under the node."""
    from langgraph.graph import START, MessagesState, StateGraph

    client, exporter = _lf
    exporter.clear()
    monkeypatch.setattr(tracing, "_client", client)

    async def approval(state):
        tracing.event("hitl.request", input={"tool_calls": []})
        return {}

    g = StateGraph(MessagesState)
    g.add_node("approval", approval)
    g.add_edge(START, "approval")
    with tracing.trace_request(settings=get_settings(), user_id="u", session_id="s", input="hi"):
        await g.compile().ainvoke(
            {"messages": []}, config={"callbacks": tracing.langchain_callbacks()}
        )
    client.flush()
    finished = exporter.get_finished_spans()
    by_id = {s.context.span_id: s for s in finished}
    (event,) = [s for s in finished if s.name == "hitl.request"]
    assert by_id[event.parent.span_id].name == "approval"
    assert _type(dict(event.attributes)) == "event"


@pytest.mark.parametrize("stream", [False, True])
async def test_failed_turn_root_shows_what_the_user_got(spans, monkeypatch, stream):
    """A timed-out turn read "TimeoutError: " with no output on a real server. The root output is now the error
    the caller received (HTTP error / SSE error event), and child observations cancelled by the timeout say so."""
    import asyncio

    from greennode_agentbase import GreenNodeRequestError
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

    from app import llm, service

    class _Slow(GenericFakeChatModel):
        def bind_tools(self, tools, **kw):  # type: ignore[override]
            return self

        async def _agenerate(self, *a, **k):
            await asyncio.sleep(5)

        async def _astream(self, *a, **k):  # the streamed turn takes this path
            await asyncio.sleep(5)
            if False:  # pragma: no cover — makes this an async generator
                yield

    monkeypatch.setenv("REQUEST_TIMEOUT_S", "0.3")
    get_settings.cache_clear()
    monkeypatch.setattr(llm, "_chat", lambda m, t, r: _Slow(messages=iter([AIMessage("late")])))
    ctx = RequestContext(session_id=f"s-timeout-{stream}", user_id="u")
    if stream:
        events = [e async for e in await service.handle({"message": "hi", "stream": True}, ctx)]
        assert events[-1]["event"] == "error" and events[-1]["status"] == 504
    else:
        with pytest.raises(GreenNodeRequestError):
            await service.handle({"message": "hi"}, ctx)
    got = spans()
    (root,) = got["agent.invoke"]
    assert json.loads(root["langfuse.observation.output"]) == {
        "error": "Agent timed out (0s)",
        "status": 504,
    }
    statuses = [a.get("langfuse.observation.status_message", "") for v in got.values() for a in v]
    assert not any("<object object" in s for s in statuses), statuses
    assert any(s.startswith("Cancelled: the request was cancelled") for s in statuses), statuses


def test_status_text_is_readable():
    import asyncio

    assert tracing._describe(TimeoutError()) == "TimeoutError"
    assert tracing._describe(ValueError("bad")) == "ValueError: bad"
    handler = tracing._callback_handler_class()()
    level, text = handler._get_error_level_and_status_message(asyncio.CancelledError())
    assert level == "WARNING" and text.startswith("Cancelled:")


async def test_generation_io_is_openai_chat_format(spans, monkeypatch):
    """The SDK appended tool definitions to the input as fake {"role": "tool", "content": <schema>} messages after
    the user's question: Langfuse's session view then replayed the whole history every turn (seen on a real
    10-turn session). Input must be the OpenAI request shape {"messages", "tools"}; tool calls OpenAI-style."""
    from langchain_core.utils.function_calling import convert_to_openai_tool

    from app import llm, service

    class _ToolThenAnswer(BaseChatModel):
        calls: int = 0

        @property
        def _llm_type(self) -> str:
            return "fake-tools"

        def bind_tools(self, tools: Any, **kwargs: Any):  # type: ignore[override]
            return self.bind(tools=[convert_to_openai_tool(t) for t in tools])

        def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
            self.calls += 1
            if self.calls == 1:
                call = {
                    "name": "get_current_time",
                    "args": {"timezone": "Asia/Ho_Chi_Minh"},
                    "id": "call_1",
                }
                msg = AIMessage("", tool_calls=[call])
            else:
                msg = AIMessage("It is noon.")
            return ChatResult(
                generations=[ChatGeneration(message=msg)], llm_output={"model_name": MODEL}
            )

    model = _ToolThenAnswer()
    monkeypatch.setattr(llm, "_chat", lambda m, t, r: model)
    out = await service.handle(
        {"message": "What time is it?"}, RequestContext(session_id="s-io", user_id="u-io")
    )
    assert out["response"] == "It is noon."
    first, second = spans()["llm.agent.generate"]
    for gen in (first, second):
        chat = json.loads(gen["langfuse.observation.input"])
        assert set(chat) == {"messages", "tools"}
        assert {t["function"]["name"] for t in chat["tools"]} >= {"get_current_time"}
        # no tool definition disguised as a message
        assert all(m["role"] != "tool" or m.get("tool_call_id") for m in chat["messages"])
    call = json.loads(first["langfuse.observation.output"])["tool_calls"][0]
    assert call["type"] == "function" and call["id"] == "call_1"
    assert json.loads(call["function"]["arguments"]) == {
        "timezone": "Asia/Ho_Chi_Minh"
    }  # JSON string
    replay = json.loads(second["langfuse.observation.input"])["messages"]
    assert [m["role"] for m in replay] == ["system", "user", "system", "assistant", "tool"]
    assert replay[3]["tool_calls"][0]["id"] == replay[4]["tool_call_id"] == "call_1"


@pytest.mark.parametrize("action", ["approve", "reject"])
async def test_hitl_turns_read_like_chat(spans, monkeypatch, action):
    """The root I/O is the conversation line of the session view: an approval request and the user's decision
    must read like chat, not as raw {"interrupt": …} / {"resume": …} JSON (seen on a real session)."""
    from app import llm, service

    class _Remember(BaseChatModel):
        calls: int = 0

        @property
        def _llm_type(self) -> str:
            return "fake-remember"

        def bind_tools(self, tools: Any, **kwargs: Any):  # type: ignore[override]
            return self

        def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
            self.calls += 1
            if self.calls == 1:
                call = {"name": "remember", "args": {"fact": "Works remote on Fridays"}, "id": "c1"}
                msg = AIMessage("", tool_calls=[call])
            else:
                msg = AIMessage("OK.")
            return ChatResult(generations=[ChatGeneration(message=msg)])

    monkeypatch.setenv("HITL_TOOLS", '["remember"]')
    monkeypatch.setenv("LTM_ENABLED", "true")
    get_settings.cache_clear()
    monkeypatch.setattr(llm, "_chat", lambda m, t, r: _Remember())
    ctx = RequestContext(session_id=f"s-hitl-{action}", user_id="u")
    first = await service.handle({"message": "remember I work remote on Fridays"}, ctx)
    assert first["status"] == "interrupted"
    decision = {"tool_call_id": "c1", "action": action, "reason": "my schedule changes"}
    await service.handle(
        {"type": "resume", "interrupt_id": first["interrupt"]["id"], "decisions": [decision]}, ctx
    )
    asked, decided = spans()["agent.invoke"]
    assert asked["langfuse.observation.output"] == (
        '[Awaiting approval]\n- remember({"fact": "Works remote on Fridays"})'
    )
    assert "langfuse.observation.metadata.interrupt" in asked  # raw payload kept for debugging
    expected = {
        "approve": '[Approval decision]\n- Approved remember({"fact": "Works remote on Fridays"})',
        "reject": '[Approval decision]\n- Rejected remember({"fact": "Works remote on Fridays"})'
        " — my schedule changes",
    }[action]
    assert decided["langfuse.observation.input"] == expected
    assert "langfuse.observation.metadata.resume" in decided
