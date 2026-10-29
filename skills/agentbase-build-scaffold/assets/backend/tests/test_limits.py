"""Admission control (app/limits.py), the bounded session-lock wait and SESSION_MAX_TURNS rotation (app/service.py)."""

from __future__ import annotations

import asyncio
import uuid

import httpx
import pytest
from greennode_agentbase import RequestContext
from greennode_agentbase.exceptions import GreenNodeRequestError
from langchain_core.messages import AIMessage, HumanMessage

from app import service
from app.config import get_settings
from app.limits import RequestLimitsMiddleware, SlidingWindowLimiter, rate_limit_wait
from app.memory.short_term import get_checkpointer, thread_config


def ctx(session: str, user: str = "alice") -> RequestContext:
    return RequestContext(session_id=session, user_id=user)


def _limited(app=None, *, max_body_bytes: int = 1024, max_concurrent: int = 0) -> httpx.AsyncClient:
    async def echo(scope, receive, send):
        body = b""
        while True:
            message = await receive()
            body += message.get("body", b"")
            if not message.get("more_body"):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": str(len(body)).encode()})

    mw = RequestLimitsMiddleware(
        app or echo, max_body_bytes=max_body_bytes, max_concurrent=max_concurrent
    )
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=mw), base_url="http://agent")


async def test_body_over_the_limit_is_413_before_the_app_reads_it():
    async with _limited() as client:
        assert (await client.post("/invocations", content=b"x" * 1024)).text == "1024"
        r = await client.post("/invocations", content=b"x" * 1025)
        assert r.status_code == 413 and r.json()["error_type"] == "GreenNodeRequestError"

        async def chunks():  # no Content-Length (chunked): counted while it arrives
            for _ in range(3):
                yield b"x" * 600

        assert (await client.post("/invocations", content=chunks())).status_code == 413


async def test_concurrency_over_the_limit_is_503_at_once_and_frees_its_slot():
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow(scope, receive, send):
        await receive()
        if scope["method"] == "POST":
            entered.set()
            await release.wait()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async with _limited(slow, max_concurrent=1) as client:
        first = asyncio.create_task(client.post("/invocations", json={}))
        await asyncio.wait_for(entered.wait(), 5)
        busy = await asyncio.wait_for(client.post("/invocations", json={}), 5)
        assert busy.status_code == 503 and busy.headers["retry-after"] == "2"
        assert (
            await client.get("/health")
        ).status_code == 200  # only POSTs to the agent paths count
        release.set()
        assert (await first).status_code == 200
        assert (await client.post("/invocations", json={})).status_code == 200


def test_sliding_window_counts_every_key_or_none():
    limiter = SlidingWindowLimiter(window_s=60)
    keys = [("user:a", 2), ("tenant:t", 3)]
    assert limiter.acquire(keys, now=0) == 0 and limiter.acquire(keys, now=1) == 0
    assert limiter.acquire(keys, now=2) == pytest.approx(58)  # user full: nothing recorded
    assert limiter.acquire([("user:b", 2), ("tenant:t", 3)], now=3) == 0  # tenant had room (2 + 1)
    assert limiter.acquire([("user:c", 2), ("tenant:t", 3)], now=4) == pytest.approx(56)
    assert limiter.acquire(keys, now=61) == 0  # window slid
    assert limiter.acquire([("user:z", 0)], now=61) == 0  # 0 ⇒ unlimited


async def test_chat_over_the_user_rate_is_429_with_retry_after(fake_llm, monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_USER_PER_MIN", "2")
    get_settings.cache_clear()
    fake_llm(AIMessage("a"), AIMessage("b"), AIMessage("c"))
    for _ in range(2):
        await service.handle({"message": "hi"}, ctx("rate-1"))
    with pytest.raises(GreenNodeRequestError) as e:
        await service.handle({"message": "hi"}, ctx("rate-1"))
    assert e.value.status_code == 429 and 0 < e.value.details["retry_after_s"] <= 60
    await service.handle({"message": "hi"}, ctx("rate-2", user="bob"))  # per user


def test_tenant_rate_spans_its_users(monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_TENANT_PER_MIN", "2")
    get_settings.cache_clear()
    s = get_settings()
    assert rate_limit_wait("u-a", "acme", s) == 0 and rate_limit_wait("u-b", "acme", s) == 0
    assert rate_limit_wait("u-c", "acme", s) > 0
    assert rate_limit_wait("u-c", "globex", s) == 0


async def test_waiting_for_a_busy_session_is_bounded(fake_llm, monkeypatch):
    monkeypatch.setenv("REQUEST_TIMEOUT_S", "0.2")
    get_settings.cache_clear()
    fake_llm(AIMessage("ok"))
    lock = service._session_lock("alice", "busy-1")
    await lock.acquire()  # another request of this session is running
    try:
        with pytest.raises(GreenNodeRequestError) as e:
            await service.handle({"message": "hi"}, ctx("busy-1"))
        assert e.value.status_code == 409
        events = [
            ev
            async for ev in await service.handle({"message": "hi", "stream": True}, ctx("busy-1"))
        ]
        assert events == [{"event": "error", "message": service._SESSION_BUSY, "status": 409}]
    finally:
        lock.release()
    assert not lock.locked()
    assert (await service.handle({"message": "hi"}, ctx("busy-1")))["status"] == "success"


async def test_long_session_is_rotated_with_its_history(fake_llm, monkeypatch):
    monkeypatch.setenv("SESSION_MAX_TURNS", "2")
    get_settings.cache_clear()
    model = fake_llm(*(AIMessage(f"answer {i}") for i in range(4)))
    for i in range(2):
        out = await service.handle({"message": f"question {i}"}, ctx("rot-1"))
        assert out["session_id"] == "rot-1"
    out = await service.handle({"message": "question 2"}, ctx("rot-1"))
    new_id = out["session_id"]
    assert new_id != "rot-1" and uuid.UUID(new_id)
    seen = [m.content for m in model.calls[-1] if isinstance(m, HumanMessage)]
    assert "question 0" in " ".join(seen)  # carried over: the model still sees the conversation

    s = get_settings()
    old = await _state(s, "rot-1")
    assert old["turns"] == 2  # the old session is no longer written
    out = await service.handle({"message": "question 3"}, ctx(new_id))
    assert out["session_id"] == new_id and (await _state(s, new_id))["turns"] == 2


async def test_rotation_is_off_with_zero_and_for_in_process_callers(fake_llm, monkeypatch):
    monkeypatch.setenv("SESSION_MAX_TURNS", "1")
    get_settings.cache_clear()
    fake_llm(*(AIMessage("ok") for _ in range(4)))
    for _ in range(2):  # A2A / eval keep their own session id (they don't read a new one back)
        out = await service.run_chat("hi", user_id="alice", session_id="rot-a2a")
        assert out["session_id"] == "rot-a2a"
    monkeypatch.setenv("SESSION_MAX_TURNS", "0")
    get_settings.cache_clear()
    for _ in range(2):
        assert (await service.handle({"message": "hi"}, ctx("rot-off")))["session_id"] == "rot-off"


async def _state(s, session_id: str) -> dict:
    cp = get_checkpointer(s)
    saved = await cp.aget_tuple(thread_config(s, session_id=session_id, user_id="alice"))
    return saved.checkpoint["channel_values"] if saved else {}
