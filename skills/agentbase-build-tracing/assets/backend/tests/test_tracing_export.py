"""What actually LEAVES the process: spans captured by an in-memory OTel exporter (no Langfuse server), scores
captured by a fake client. Secrets in exception text must not reach status_message, the OTel span status or an
OTel `exception` event — whichever code path sets them (tracing helpers or the LangChain CallbackHandler)."""

from __future__ import annotations

import json
import uuid

import httpx
import pytest

from app.config import get_settings
from app.observability import tracing

MCP_URL = "https://x/mcp?tavilyApiKey=SECRET-1&sig=abc"
SECRETS = ("SECRET-1", "sig=abc", "hunter2")


def _mcp_http_error() -> httpx.HTTPStatusError:
    """The exact error an MCP tools/call re-raises: its message contains the request URL."""
    request = httpx.Request("POST", MCP_URL)
    try:
        httpx.Response(502, request=request).raise_for_status()
    except httpx.HTTPStatusError as e:
        return e
    raise AssertionError("unreachable")


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
        mask_otel_spans=getattr(tracing, "_mask_otel_spans", None),  # same wiring as init_tracing
    )
    yield client, exporter
    LangfuseResourceManager.reset()


@pytest.fixture
def lf(_lf, monkeypatch):
    client, exporter = _lf
    exporter.clear()
    monkeypatch.setattr(tracing, "_client", client)
    monkeypatch.setattr(tracing, "_mask_pii", True)
    return client, exporter


def _exported(client, exporter) -> tuple[str, dict[str, str]]:
    """(everything exported as one string, {span name: status_message attribute})."""
    client.flush()
    spans = exporter.get_finished_spans()
    dump = [
        {
            "attributes": dict(s.attributes or {}),
            "status": s.status.description,
            "events": [{"name": e.name, **dict(e.attributes or {})} for e in s.events],
        }
        for s in spans
    ]
    status = {
        s.name: s.attributes.get("langfuse.observation.status_message")
        for s in spans
        if s.attributes and s.attributes.get("langfuse.observation.status_message")
    }
    return json.dumps(dump, default=str), status


def _assert_clean(blob: str) -> None:
    for s in SECRETS:
        assert s not in blob, f"{s!r} leaked: {blob[:2000]}"


def test_step_exception_is_masked_everywhere(lf):
    with pytest.raises(httpx.HTTPStatusError):
        with tracing.step("mcp.call_tool", input={"server": "tavily"}):
            raise _mcp_http_error()
    blob, status = _exported(*lf)
    _assert_clean(blob)
    assert "tavilyApiKey=***" in status["mcp.call_tool"]


def test_step_set_status_message_is_masked(lf):
    """Call sites (routing, long-term memory, compression, mcp) pass f"{type(e).__name__}: {e}"."""
    with tracing.step("llm.route") as st:
        e = _mcp_http_error()
        st.set(level="WARNING", status_message=f"{type(e).__name__}: {e}")
    blob, status = _exported(*lf)
    _assert_clean(blob)
    assert status["llm.route"].startswith("HTTPStatusError: Server error '502 Bad Gateway'")


def test_objects_given_to_helpers_are_masked(lf):
    """`mask` receives raw objects. The SDK's media pass model_dump()s pydantic models first (unless
    LANGFUSE_MEDIA_UPLOAD_ENABLED=false) but never dataclasses ⇒ their text skipped masking."""
    import dataclasses

    from langchain_core.messages import HumanMessage

    @dataclasses.dataclass
    class Lookup:
        query: str

    key = "sk-proj-ABCDEFGHIJKLMNOPQRSTUV"
    assert key not in tracing._mask(data=f"my key {key}")  # masked as a plain string…
    with tracing.step("crm.lookup", input={"req": Lookup(f"my key {key}")}) as st:
        st.set(output=HumanMessage(f"echo {key}"))
    blob, _ = _exported(*lf)
    assert "sk-proj-ABCDEF" not in blob  # …and inside objects
    (span,) = [s for s in lf[1].get_finished_spans() if s.name == "crm.lookup"]
    assert json.loads(span.attributes["langfuse.observation.input"]) == {
        "req": {"query": "my key ***KEY***"}  # same shape the SDK serializer would have produced
    }


def test_trace_request_exception_is_masked_and_reraised(lf):
    with pytest.raises(RuntimeError, match="hunter2"):  # the caller still gets the real error
        with tracing.trace_request(
            settings=get_settings(), user_id="u1", session_id="s1", input="hi"
        ):
            raise RuntimeError("db login password=hunter2 failed")
    blob, status = _exported(*lf)
    _assert_clean(blob)
    assert status["agent.invoke"] == "RuntimeError: db login password=*** failed"


def test_callback_handler_errors_are_masked(lf):
    """Tool/chain errors traced by the LangChain CallbackHandler (status_message set by the SDK)."""
    from langchain_core.messages import AIMessage
    from langchain_core.runnables import RunnableLambda
    from langchain_core.tools import tool
    from langgraph.graph import START, MessagesState, StateGraph
    from langgraph.prebuilt import ToolNode

    @tool
    def web_search(query: str) -> str:
        """MCP tool whose HTTP call fails (on_tool_error path)."""
        raise _mcp_http_error()

    @tool
    def lookup(query: str) -> str:
        """Tool that handles its own error ⇒ ToolMessage(status="error") (on_tool_end path)."""
        from langchain_core.tools import ToolException

        raise ToolException(f"upstream {MCP_URL} said password=hunter2")

    lookup.handle_tool_error = True  # like langchain-mcp-adapters tools

    def chain_fails(_):
        raise ValueError(f"cannot reach {MCP_URL}")

    g = StateGraph(MessagesState)
    g.add_node("tools", ToolNode([web_search, lookup], handle_tool_errors=True))
    g.add_edge(START, "tools")
    calls = [
        {"name": "web_search", "args": {"query": "q"}, "id": "c1"},
        {"name": "lookup", "args": {"query": "q"}, "id": "c2"},
    ]
    cfg = {"callbacks": tracing.langchain_callbacks()}
    out = g.compile().invoke({"messages": [AIMessage("", tool_calls=calls)]}, config=cfg)
    with pytest.raises(ValueError):
        RunnableLambda(chain_fails).invoke(1, config=cfg)

    assert "SECRET-1" in out["messages"][1].content  # the graph itself still sees the real error
    blob, status = _exported(*lf)
    _assert_clean(blob)
    assert {"web_search", "lookup", "chain_fails"} <= set(status)


def test_otel_hook_masks_status_of_any_span(lf):
    """mask_otel_spans re-masks status_message on spans created outside our helpers (plain SDK
    CallbackHandler here). The OTel status description of such spans cannot be patched by the hook — that is
    why langchain_callbacks() returns the masking subclass."""
    from langchain_core.runnables import RunnableLambda
    from langfuse.langchain import CallbackHandler

    def boom(_):
        raise ValueError(f"cannot reach {MCP_URL}")

    with pytest.raises(ValueError):
        RunnableLambda(boom).invoke(1, config={"callbacks": [CallbackHandler()]})
    _, status = _exported(*lf)
    assert "SECRET-1" not in status["boom"] and "tavilyApiKey=***" in status["boom"]


def test_init_tracing_wires_both_mask_hooks(monkeypatch):
    import langfuse

    seen: dict = {}

    class FakeLangfuse:
        def __init__(self, *, mask=None, mask_otel_spans=None, **kwargs):
            seen.update(mask=mask, mask_otel_spans=mask_otel_spans)

    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-x")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-x")
    monkeypatch.setattr(langfuse, "Langfuse", FakeLangfuse)
    monkeypatch.setattr(tracing, "_client", None)
    get_settings.cache_clear()
    tracing.init_tracing(get_settings())
    assert seen["mask"] is tracing._mask
    assert seen["mask_otel_spans"] is getattr(tracing, "_mask_otel_spans", object())


# --------------------------------------------------------------------------- scores
class _FakeScores:
    def __init__(self):
        self.calls: list[dict] = []

    def create_score(self, **kwargs):
        self.calls.append(kwargs)

    def get_current_trace_id(self):
        return "a" * 32


def test_feedback_retaps_upsert_one_score(monkeypatch):
    fake = _FakeScores()
    monkeypatch.setattr(tracing, "_client", fake)
    trace = "0123456789abcdef0123456789abcdef"
    assert tracing.record_feedback(trace_id=trace, value=1.0)
    assert tracing.record_feedback(trace_id=trace, value=-1.0)  # user changes 👍 → 👎
    assert tracing.record_feedback(trace_id="f" * 32, value=1.0)
    first, second, other = (c.get("score_id") for c in fake.calls)
    assert first is not None and first == second  # same id ⇒ Langfuse overwrites, no duplicate
    assert other != first
    assert len(first) == 32 and int(first, 16) >= 0  # 32 hex, like Langfuse.create_trace_id(seed=…)


def test_feedback_score_id_scoped_per_user():
    a = tracing.feedback_score_id(trace_id="t", user_id="alice")
    assert a == tracing.feedback_score_id(trace_id="t", user_id="alice")
    assert a != tracing.feedback_score_id(trace_id="t", user_id="bob")


def test_score_comments_masked(monkeypatch):
    fake = _FakeScores()
    monkeypatch.setattr(tracing, "_client", fake)
    monkeypatch.setattr(tracing, "_mask_pii", True)
    tracing.record_feedback(trace_id="t" * 32, value=-1.0, comment="wrong, call me 0912 345 678")
    tracing.score_trace("self_eval", 0.2, comment="answer leaked password=hunter2")
    assert [c["comment"] for c in fake.calls] == [
        "wrong, call me ***PHONE***",
        "answer leaked password=***",
    ]
