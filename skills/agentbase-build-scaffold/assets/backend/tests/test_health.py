"""GET /ready (app/health.py), the JWKS outcome it reports, and the structured logs (app/logging_setup.py)."""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import httpx
import openai
import pytest
from greennode_agentbase import RequestContext
from langchain_core.messages import AIMessage

from app import health, service
from app.auth import inbound
from app.config import get_settings
from app.logging_setup import JsonFormatter, TextFormatter, configure_logging


@pytest.fixture(autouse=True)
def _clean_health():
    health.reset()
    yield
    health.reset()


def _settings(monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()
    return get_settings()


async def _get_ready() -> httpx.Response:
    import main

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=main.app), base_url="http://agent"
    ) as client:
        return await client.get("/ready")


# --------------------------------------------------------------------------- readiness
async def test_ready_until_something_fails(monkeypatch):
    r = await _get_ready()
    assert r.status_code == 200 and r.json()["status"] == "ready" and r.json()["failing"] == []
    health.record("llm", False, "HTTP 401")
    r = await _get_ready()
    assert (
        r.status_code == 200 and r.json()["status"] == "degraded" and r.json()["failing"] == ["llm"]
    )
    assert "401" not in r.text  # components only: reasons stay in the log
    health.record("llm", True)  # a turn that worked clears it
    assert (await _get_ready()).json()["status"] == "ready"


async def test_a_failure_nobody_sees_again_is_forgotten(monkeypatch):
    health.record("backing_service", False, "HTTP 503")
    assert health.failing() == {"backing_service": "HTTP 503"}
    assert health.failing(now=health._state["backing_service"][1] + health.STALE_S + 1) == {}


async def test_unusable_jwks_is_not_ready_and_recovers_when_the_probe_works(monkeypatch):
    s = _settings(monkeypatch, AUTH_MODE="jwt", AUTH_JWKS_URL="https://idp.example/jwks")
    health.record("jwks", False, "unreachable")
    probes = []

    async def probe():  # the IdP is back
        probes.append(1)
        health.record("jwks", True)

    assert await health.readiness(s, None) == ("not_ready", ["jwks"])
    assert await health.readiness(s, probe) == (
        "ready",
        [],
    )  # re-checked by /ready itself, no user request needed
    assert probes == [1]


async def test_a_failing_jwks_is_probed_at_most_every_ten_seconds(monkeypatch):
    s = _settings(monkeypatch, AUTH_MODE="jwt", AUTH_JWKS_URL="https://idp.example/jwks")
    health.record("jwks", False)
    probes = []

    async def still_down():
        probes.append(1)
        health.record("jwks", False)

    for _ in range(5):
        assert (await health.readiness(s, still_down))[0] == "not_ready"
    assert probes == [1]


async def test_jwks_outage_does_not_matter_without_jwt_auth(monkeypatch):
    s = _settings(monkeypatch, AUTH_MODE="none")
    health.record("jwks", False)
    assert (await health.readiness(s, None))[
        0
    ] == "degraded"  # nobody needs the JWKS: informational only


async def test_ready_route_answers_503_when_users_cannot_log_in(monkeypatch):
    _settings(monkeypatch, AUTH_MODE="jwt", AUTH_JWKS_URL="https://idp.invalid/jwks")
    import main

    monkeypatch.setattr(main, "settings", get_settings())
    monkeypatch.setattr(main, "prefetch_jwks", lambda s: _noop())
    health.record("jwks", False)
    r = await _get_ready()
    assert r.status_code == 503 and r.json()["status"] == "not_ready"


async def _noop():
    return None


def test_jwks_failures_are_recorded_by_the_auth_code(monkeypatch):
    s = _settings(
        monkeypatch,
        AUTH_MODE="jwt",
        AUTH_JWKS_URL="https://idp.example/jwks",
        AUTH_ISSUER="https://idp.example",
        AUTH_AUDIENCE="agent",
    )
    import jwt

    class Down:
        def get_signing_key_from_jwt(self, _t):
            raise jwt.PyJWKClientError("Fail to fetch data from the url, err: boom")

    monkeypatch.setattr(inbound, "_jwks_client", lambda _url: Down())
    with pytest.raises(Exception) as e:
        inbound.verify_jwt("a.b.c", s)
    assert getattr(e.value, "status_code", None) == 503
    assert "jwks" in health.failing()

    class UnknownKid:
        def get_signing_key_from_jwt(self, _t):
            raise jwt.PyJWKClientError("Unable to find a signing key that matches: 'k'")

    monkeypatch.setattr(inbound, "_jwks_client", lambda _url: UnknownKid())
    with pytest.raises(Exception) as e:
        inbound.verify_jwt("a.b.c", s)
    assert (
        getattr(e.value, "status_code", None) == 401
    )  # the token's fault: the JWKS itself is fine
    assert "jwks" not in health.failing()


async def test_a_rejected_llm_key_shows_up_as_degraded_and_a_good_turn_clears_it(fake_llm):
    request = httpx.Request("POST", "https://llm.example/v1/chat/completions")
    err = openai.AuthenticationError(
        "bad key", response=httpx.Response(401, request=request), body=None
    )
    assert service._map_error(err).status_code == 502
    service._mapped(err)
    assert health.failing().get("llm") == "HTTP 401"
    fake_llm(AIMessage("hello"))
    await service.handle({"message": "hi"}, RequestContext(session_id="ready-1", user_id="alice"))
    assert "llm" not in health.failing()


# --------------------------------------------------------------------------- logs
def _record(msg="m", **fields) -> logging.LogRecord:
    r = logging.LogRecord("app.test", logging.INFO, __file__, 1, msg, None, None)
    if fields:
        r.fields = fields
    return r


def test_json_line_has_ids_fields_and_masked_text(monkeypatch):
    import app.logging_setup as ls

    monkeypatch.setattr(ls, "_ids", lambda: {"request_id": "req-1", "trace_id": "tr-1"})
    line = JsonFormatter().format(
        _record("call with Bearer abc.def.ghi and user@example.com", status=200, duration_ms=12)
    )
    out = json.loads(line)
    assert out["level"] == "INFO" and out["logger"] == "app.test" and out["ts"].endswith("Z")
    assert out["request_id"] == "req-1" and out["trace_id"] == "tr-1"
    assert out["status"] == 200 and out["duration_ms"] == 12
    assert "abc.def.ghi" not in out["msg"] and "user@example.com" not in out["msg"]


def test_exception_text_is_masked(monkeypatch):
    try:
        raise RuntimeError("failed GET https://svc.example/x?api_key=sk-secret-value-123456")
    except RuntimeError:
        import sys

        rec = _record()
        rec.exc_info = sys.exc_info()
    out = json.loads(JsonFormatter().format(rec))
    assert "sk-secret-value-123456" not in out["exc"] and "RuntimeError" in out["exc"]


def test_text_format_appends_the_fields():
    line = TextFormatter("%(levelname)s %(message)s").format(_record("request", status=200))
    assert line.endswith("request status=200")


@pytest.mark.parametrize(
    ("log_format", "is_local", "json_expected"),
    [("auto", True, False), ("auto", False, True), ("json", True, True), ("text", False, False)],
)
def test_log_format_selection(log_format, is_local, json_expected):
    root = logging.getLogger()
    saved = root.handlers[:], root.level
    stub = SimpleNamespace(log_format=log_format, is_local=is_local, log_level="INFO")
    try:
        configure_logging(stub)
        configure_logging(stub)  # a second call replaces our handler, it does not add another
        ours = [h for h in root.handlers if type(h) is logging.StreamHandler]
        assert len(ours) == 1 and isinstance(ours[0].formatter, JsonFormatter) == json_expected
    finally:
        root.handlers[:], root.level = saved


async def test_every_request_writes_one_structured_line_without_content(fake_llm, caplog):
    fake_llm(AIMessage("The secret answer"))
    with caplog.at_level(logging.INFO, logger="app.service"):
        await service.handle(
            {"message": "my secret question"}, RequestContext(session_id="log-1", user_id="alice")
        )
    lines = [r for r in caplog.records if r.getMessage() == "request"]
    assert len(lines) == 1
    f = lines[0].fields
    assert f["type"] == "chat" and f["status"] == 200 and f["outcome"] == "success"
    assert f["session_id"] == "log-1" and len(f["user"]) == 12 and "alice" not in str(f)
    assert isinstance(f["duration_ms"], int) and f["stream"] is False
    assert "secret" not in caplog.text


async def test_failed_requests_log_their_status(fake_llm, caplog):
    fake_llm(AIMessage("x"))
    with caplog.at_level(logging.INFO, logger="app.service"), pytest.raises(Exception) as e:  # noqa: PT011
        await service.handle(
            {"type": "resume", "decisions": [{"tool_call_id": "x", "action": "approve"}]},
            RequestContext(session_id="log-2", user_id="alice"),
        )
    assert getattr(e.value, "status_code", None) == 409  # nothing was pending
    [line] = [r for r in caplog.records if r.getMessage() == "request"]
    assert line.fields["type"] == "resume" and line.fields["status"] == 409
    assert "outcome" not in line.fields  # no result: the run did not finish


async def test_a_streamed_request_logs_once_when_it_ends(fake_llm, caplog):
    fake_llm(AIMessage("x"))
    with caplog.at_level(logging.INFO, logger="app.service"):
        stream = await service.handle(
            {"message": "hi", "stream": True}, RequestContext(session_id="log-3", user_id="alice")
        )
        [_ async for _ in stream]
    [line] = [r for r in caplog.records if r.getMessage() == "request"]
    assert line.fields["stream"] is True and line.fields["status"] == 200


async def test_lifespan_gives_blocking_calls_their_own_sized_thread_pool(monkeypatch):
    """Python's default executor has CPUs + 4 threads (5 on 1 vCPU) — fewer than MEMORY_MAX_CONCURRENCY — so a slow
    Memory used to queue JWT verification behind it. The startup hook sizes it from EXECUTOR_THREADS."""
    import asyncio

    import main

    monkeypatch.setattr(main.settings, "executor_threads", 17)
    async with main.lifespan(main.app):
        pool = asyncio.get_running_loop()._default_executor
        assert pool is not None and pool._max_workers == 17
        assert await asyncio.to_thread(lambda: 21 * 2) == 42
