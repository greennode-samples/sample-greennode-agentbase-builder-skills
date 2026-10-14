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
    assert output["content"] == "Paris is the capital of France."
    assert output["additional_kwargs"][tracing.REASONING_KEY].startswith(
        "The user asks for a capital"
    )
    assert "hunter2" not in gen["langfuse.observation.output"]  # thinking goes through `mask` too

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
