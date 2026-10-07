"""Capability tiers + task → tier table + fallback + adaptive routing."""

from __future__ import annotations

import httpx
import openai
import pytest
from greennode_agentbase import RequestContext
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from app import llm, service
from app.config import get_settings


class _Model(BaseChatModel):
    name_: str
    error: str | None = None

    @property
    def _llm_type(self) -> str:
        return "fake"

    def _generate(self, messages, stop=None, run_manager=None, **kw):
        req = httpx.Request("POST", "http://x")
        if self.error == "connection":
            raise openai.APIConnectionError(request=req)
        if self.error == "rate":
            raise openai.RateLimitError("429", response=httpx.Response(429, request=req), body=None)
        if self.error == "bad_request":
            raise openai.BadRequestError(
                "400", response=httpx.Response(400, request=req), body=None
            )
        return ChatResult(generations=[ChatGeneration(message=AIMessage(self.name_))])

    def bind_tools(self, tools, **kw):  # type: ignore[override]
        return self


@pytest.fixture
def models(monkeypatch):
    registry: dict[str, _Model] = {}
    monkeypatch.setattr(
        llm, "_chat", lambda model, tier, role: registry.setdefault(model, _Model(name_=model))
    )
    return registry


def _env(monkeypatch, **kv):
    for k, v in kv.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()


def test_capability_tiers_and_defaults(monkeypatch):
    _env(monkeypatch, LLM_MODEL="L", LLM_MODEL_REASONING="R", LLM_MODEL_SMALL="S")
    assert [llm.model_for(t) for t in ("reasoning", "large", "small")] == ["R", "L", "S"]
    _env(monkeypatch, LLM_MODEL_REASONING="", LLM_MODEL_SMALL="")
    assert llm.model_for("reasoning") == llm.model_for("small") == "L"  # empty tier ⇒ large


def test_task_to_tier_mapping_and_override(monkeypatch):
    _env(monkeypatch, LLM_TASK_TIERS='{"agent": "reasoning", "planner": "reasoning"}')
    assert llm.tier_for("agent") == "reasoning"  # override
    assert llm.tier_for("summarize") == "small"  # default
    assert llm.tier_for("eval_judge") == "reasoning"
    assert llm.tier_for("planner") == "reasoning"  # new task added by the project
    assert llm.tier_for("unknown") == "large"
    _env(monkeypatch, LLM_TASK_TIERS='{"agent": "huge"}')
    with pytest.raises(ValueError):
        llm.tier_for("agent")


def test_fallback_chain_per_tier(monkeypatch):
    _env(
        monkeypatch,
        LLM_MODEL="L",
        LLM_FALLBACK_MODELS='["b1","L","b2","b1"]',
        LLM_TIER_FALLBACKS='{"small": ["s-backup"]}',
    )
    assert llm.fallbacks_for("large") == ["b1", "b2"]
    assert llm.fallbacks_for("small") == ["s-backup"]


@pytest.mark.parametrize("err", ["connection", "rate"])
async def test_falls_back_on_infra_errors(monkeypatch, models, err):
    _env(monkeypatch, LLM_MODEL="primary", LLM_FALLBACK_MODELS='["backup"]')
    models["primary"] = _Model(name_="primary", error=err)
    assert (await llm.get_llm("agent", tools=[]).ainvoke("hi")).content == "backup"


async def test_no_fallback_on_bad_request(monkeypatch, models):
    _env(monkeypatch, LLM_MODEL="primary", LLM_FALLBACK_MODELS='["backup"]')
    models["primary"] = _Model(name_="primary", error="bad_request")
    with pytest.raises(openai.BadRequestError):
        await llm.get_llm("agent").ainvoke("hi")


async def test_each_task_uses_its_tier_model(monkeypatch, models):
    _env(
        monkeypatch,
        LLM_MODEL="L",
        LLM_MODEL_REASONING="R",
        LLM_MODEL_SMALL="S",
        LLM_FALLBACK_MODELS="[]",
    )
    assert (await llm.get_llm("summarize").ainvoke("x")).content == "S"
    assert (await llm.get_llm("agent").ainvoke("x")).content == "L"
    assert (await llm.get_llm("eval_judge").ainvoke("x")).content == "R"


@pytest.mark.parametrize(
    ("verdict", "expected_task"),
    [
        ("simple", "agent_simple"),
        ("standard", "agent"),
        ("complex", "agent_complex"),
        ("junk", "agent"),
    ],  # parse error ⇒ standard
)
async def test_adaptive_routing_picks_task(monkeypatch, verdict, expected_task):
    _env(monkeypatch, LLM_ADAPTIVE_ROUTING="true")
    used: list[str] = []

    class _Fake(BaseChatModel):
        task: str

        @property
        def _llm_type(self):
            return "fake"

        def _generate(self, messages, stop=None, run_manager=None, **kw):
            used.append(self.task)
            text = f'{{"complexity": "{verdict}"}}' if self.task == "router" else "ok"
            return ChatResult(generations=[ChatGeneration(message=AIMessage(text))])

        def bind_tools(self, tools, **kw):  # type: ignore[override]
            return self

    fake = lambda task="agent", tools=None: _Fake(task=task)  # noqa: E731
    monkeypatch.setattr("app.graph.builder.get_llm", fake)
    monkeypatch.setattr("app.llm.routing.get_llm", fake)
    await service.handle(
        {"message": "a question"},
        RequestContext(
            session_id=f"r-{expected_task.replace('_', '-')}-{len(verdict)}", user_id="u"
        ),
    )
    assert used == ["router", expected_task]


async def test_reflection_skipped_for_simple_questions(monkeypatch):
    _env(monkeypatch, LLM_ADAPTIVE_ROUTING="true", REFLECTION_ENABLED="true")
    used: list[str] = []

    class _Fake(BaseChatModel):
        task: str

        @property
        def _llm_type(self):
            return "fake"

        def _generate(self, messages, stop=None, run_manager=None, **kw):
            used.append(self.task)
            text = '{"complexity": "simple"}' if self.task == "router" else "hello there"
            return ChatResult(generations=[ChatGeneration(message=AIMessage(text))])

        def bind_tools(self, tools, **kw):  # type: ignore[override]
            return self

    fake = lambda task="agent", tools=None: _Fake(task=task)  # noqa: E731
    for mod in ("app.graph.builder", "app.llm.routing", "app.reflection"):
        monkeypatch.setattr(f"{mod}.get_llm", fake)
    await service.handle({"message": "hi"}, RequestContext(session_id="skip-refl", user_id="u"))
    assert used == ["router", "agent_simple"]  # no judge


def test_primary_switches_fast_fallbacks_keep_backoff(monkeypatch):
    llm._chat.cache_clear()
    _env(monkeypatch, LLM_MODEL="L", LLM_MAX_RETRIES="2", LLM_FALLBACK_MODELS='["b1"]')
    assert llm._chat("L", "large", "primary").max_retries == 0
    # 429 is account-wide: the fallback must still back off and retry
    assert llm._chat("b1", "large", "fallback").max_retries == 2
    llm._chat.cache_clear()
    _env(monkeypatch, LLM_FALLBACK_MODELS="[]")
    assert llm._chat("L", "large", "primary").max_retries == 2
    llm._chat.cache_clear()


def test_retry_backoff_matches_openai_sdk():
    from openai import _constants

    assert llm.SDK_INITIAL_RETRY_DELAY_S == _constants.INITIAL_RETRY_DELAY
    assert llm.SDK_MAX_RETRY_DELAY_S == _constants.MAX_RETRY_DELAY
    # 0.5 × 2^n capped at 8 s, summed over the retries of one model
    assert [llm.retry_sleep_s(r) for r in range(7)] == [0, 0.5, 1.5, 3.5, 7.5, 15.5, 23.5]


@pytest.mark.parametrize(
    ("env", "tier", "expected"),
    [
        # no fallback: (1 + retries) × timeout + backoff — the template defaults (60 s, 2 retries): 3 × 60 + 1.5
        ({"LLM_FALLBACK_MODELS": "[]"}, "large", 181.5),
        ({"LLM_FALLBACK_MODELS": "[]"}, "reasoning", 361.5),  # reasoning timeout × 2
        # 1 fallback: primary 1 attempt (no retry) + fallback (1 + 2) attempts + its backoff
        ({"LLM_FALLBACK_MODELS": '["b1"]'}, "large", 4 * 60 + 1.5),
        ({"LLM_FALLBACK_MODELS": '["b1", "b2"]'}, "small", 7 * 60 + 2 * 1.5),
        # recommended (SKILL §4): 25 s, 1 retry, 1 fallback
        (
            {"LLM_TIMEOUT_S": "25", "LLM_MAX_RETRIES": "1", "LLM_FALLBACK_MODELS": '["b1"]'},
            "large",
            75.5,
        ),
        (
            {"LLM_TIMEOUT_S": "25", "LLM_MAX_RETRIES": "1", "LLM_FALLBACK_MODELS": '["b1"]'},
            "reasoning",
            150.5,
        ),
    ],
)
def test_worst_case_includes_retry_backoff(monkeypatch, env, tier, expected):
    _env(monkeypatch, **{"LLM_MODEL": "L", "LLM_TIMEOUT_S": "60", "LLM_MAX_RETRIES": "2", **env})
    assert llm.worst_case_s(tier) == pytest.approx(expected)


def test_warns_when_fallback_chain_exceeds_request_timeout(monkeypatch, caplog):
    llm._warn_if_over_budget.cache_clear()
    _env(
        monkeypatch,
        LLM_MODEL="L",
        LLM_TIMEOUT_S="60",
        LLM_MAX_RETRIES="2",
        REQUEST_TIMEOUT_S="180",
        LLM_FALLBACK_MODELS="[]",
    )
    # 3 attempts × 60 s = 180 s fits — but the SDK sleeps 0.5 s + 1 s between them ⇒ 181.5 s > 180 s
    llm._warn_if_over_budget("large")
    assert "tier large" in caplog.text and "181.5" in caplog.text
    llm._warn_if_over_budget.cache_clear()


def test_recommended_settings_fit_every_tier(monkeypatch, caplog):
    """SKILL §4 time-budget table: no warning for any tier, worst case within REQUEST_TIMEOUT_S."""
    one_fallback = {"LLM_FALLBACK_MODELS": '["b1"]'}
    for env in (
        {"LLM_TIMEOUT_S": "25", "LLM_MAX_RETRIES": "1", **one_fallback},  # recommended
        {
            "LLM_TIMEOUT_S": "25",
            "LLM_MAX_RETRIES": "1",
            "LLM_TIER_FALLBACKS": '{"large": ["b1", "b2"], "small": ["s1", "s2"], "reasoning": ["r1"]}',
        },
        {"LLM_TIMEOUT_S": "40", "LLM_MAX_RETRIES": "1"},  # no fallbacks
        {"LLM_TIMEOUT_S": "45", "LLM_MAX_RETRIES": "1", "REQUEST_TIMEOUT_S": "300", **one_fallback},
    ):
        llm._warn_if_over_budget.cache_clear()
        base = {"REQUEST_TIMEOUT_S": "180", "LLM_FALLBACK_MODELS": "[]", "LLM_TIER_FALLBACKS": "{}"}
        _env(monkeypatch, LLM_MODEL="L", **{**base, **env})
        for tier in llm.TIERS:
            llm._warn_if_over_budget(tier)
            assert llm.worst_case_s(tier) <= get_settings().request_timeout_s, (env, tier)
    assert "worst case" not in caplog.text
    llm._warn_if_over_budget.cache_clear()


# --- Real ChatOpenAI against a local OpenAI-compatible mock server (no real LLM) ---------------------------------

_DONE = object()


def _chunk(content: str | None = None, finish: str | None = None) -> dict:
    delta = {"role": "assistant", "content": content} if content is not None else {}
    return {
        "id": "chatcmpl-mock",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "mock",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }


def _answer(text: str) -> list:
    tokens = [w + " " for w in text.split(" ")]
    tokens[-1] = tokens[-1].rstrip()
    return [*(_chunk(t) for t in tokens), _chunk(finish="stop"), _DONE]


# vLLM-style: the provider fails AFTER the 200 response started ⇒ an `{"error": ...}` SSE chunk (no HTTP status)
_SSE_ERROR = {"error": {"message": "engine overloaded", "type": "InternalServerError", "code": 500}}


@pytest.fixture
def mock_llm_server(monkeypatch):
    """OpenAI-compatible /v1/chat/completions over real HTTP. `scripts[model]` = list of SSE payloads (dicts,
    `_DONE`) or an int HTTP status (error JSON body). `calls` records the model of each request."""
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    scripts: dict[str, list | int] = {}
    calls: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # keep pytest output clean
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append(body["model"])
            script = scripts[body["model"]]
            if isinstance(script, int):
                payload = json.dumps(
                    {"error": {"message": f"mock {script}", "type": "invalid_request_error"}}
                ).encode()
                self.send_response(script)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            assert body.get("stream"), "template streams by default (LLM_STREAMING=true)"
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for item in script:
                data = "[DONE]" if item is _DONE else json.dumps(item)
                self.wfile.write(f"data: {data}\n\n".encode())
                self.wfile.flush()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    llm._chat.cache_clear()
    llm._warn_if_over_budget.cache_clear()
    _env(
        monkeypatch,
        LLM_BASE_URL=f"http://127.0.0.1:{server.server_port}/v1",
        LLM_MODEL="primary",
        LLM_FALLBACK_MODELS='["backup"]',
        LLM_MAX_RETRIES="0",
    )
    yield scripts, calls
    server.shutdown()
    llm._chat.cache_clear()
    llm._warn_if_over_budget.cache_clear()


async def test_sse_error_chunk_mid_stream_falls_back(mock_llm_server):
    """vLLM/MaaS report a failure inside a 200 stream as an SSE error chunk ⇒ the openai SDK raises a BARE
    openai.APIError (no status). It is an infrastructure failure ⇒ must fall back (it didn't: not in FALLBACK_ERRORS)."""
    scripts, calls = mock_llm_server
    scripts["primary"] = [_chunk("Partial"), _SSE_ERROR]
    scripts["backup"] = _answer("backup model answer")
    out = await llm.get_llm("agent").ainvoke("hi")
    assert out.content == "backup model answer"
    assert calls == ["primary", "backup"]


async def test_sse_error_before_first_token_falls_back(mock_llm_server):
    scripts, calls = mock_llm_server
    scripts["primary"] = [_SSE_ERROR]
    scripts["backup"] = _answer("ok")
    assert (await llm.get_llm("agent").ainvoke("hi")).content == "ok"
    assert calls == ["primary", "backup"]


async def test_sse_error_falls_back_in_batch_and_sync_paths(mock_llm_server):
    scripts, calls = mock_llm_server
    scripts["primary"] = [_chunk("Partial"), _SSE_ERROR]
    scripts["backup"] = _answer("ok")
    chain = llm.get_llm("agent")
    assert [m.content for m in await chain.abatch(["hi"])] == ["ok"]
    assert chain.invoke("hi").content == "ok"
    assert calls == ["primary", "backup"] * 2


@pytest.mark.parametrize("status", [400, 401, 422])
async def test_http_request_errors_do_not_fall_back(mock_llm_server, status):
    """Genuine request errors (bad request / wrong key / unprocessable) — another model can't fix them."""
    scripts, calls = mock_llm_server
    scripts["primary"] = status
    scripts["backup"] = _answer("should not be used")
    with pytest.raises(openai.APIStatusError) as exc:
        await llm.get_llm("agent").ainvoke("hi")
    assert exc.value.status_code == status
    assert calls == ["primary"]


async def test_sse_error_mid_stream_resets_client_and_backup_answers(mock_llm_server):
    """End to end through the service: tokens of the failed primary were already streamed ⇒ `reset`, then the
    backup's answer only."""
    scripts, calls = mock_llm_server
    scripts["primary"] = [_chunk("Xin "), _chunk("chào"), _SSE_ERROR]
    scripts["backup"] = _answer("Hello there")
    gen = await service.handle(
        {"message": "hi", "stream": True}, RequestContext(session_id="sse-err", user_id="u")
    )
    events = [e async for e in gen]
    kinds = [e["event"] for e in events]
    assert calls == ["primary", "backup"]
    assert kinds.count("reset") == 1
    after = events[kinds.index("reset") + 1 :]
    assert "".join(e["data"] for e in after if e["event"] == "token") == "Hello there"
    assert events[-1]["event"] == "done" and events[-1]["response"] == "Hello there"
