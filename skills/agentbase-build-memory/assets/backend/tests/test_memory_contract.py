"""Contract tests against the greennode-agentbase SDK (real bugs hit on deploy):
- insert-directly must use MemoryRecordInsertDirectlyRequest (passing a list => TypeError in the SDK)
- search returns list[dict] {id, memory, score, ...}; search limit within the API's 5–200
- 429/5xx (10 concurrent requests / IAM account) are retried with backoff, other errors are not
"""

from __future__ import annotations

import asyncio

import pytest
from greennode_agentbase.exceptions import GreenNodeRequestError
from greennode_agentbase.memory.models import (
    MemoryRecordInsertDirectlyRequest,
    MemoryRecordSearchRequest,
)

from app.config import get_settings
from app.memory.long_term import AgentBaseLTM


class FakeMemoryClient:
    """`failures`: GreenNodeRequestError status codes raised (in order) before calls succeed."""

    def __init__(self, failures: tuple[int, ...] = ()):
        self.inserted = []
        self.calls = 0
        self.failures = list(failures)
        self.limits: list[int] = []

    def _maybe_fail(self):
        self.calls += 1
        if self.failures:
            code = self.failures.pop(0)
            raise GreenNodeRequestError(f"HTTP {code} error: mock", status_code=code)

    async def insert_memory_records_directly_async(self, *, id, namespace, request):
        assert isinstance(request, MemoryRecordInsertDirectlyRequest)
        self._maybe_fail()
        self.inserted.append((namespace, request.memory_records))
        return {}

    async def search_memory_records_async(self, *, id, namespace, request):
        assert isinstance(request, MemoryRecordSearchRequest)
        assert len(request.query) <= 1000  # Memory API limit (400 if exceeded)
        assert 5 <= request.limit <= 200  # Memory API range
        self.limits.append(request.limit)
        self._maybe_fail()
        return [
            {"id": "1", "memory": "Likes green tea", "score": 0.71},
            {"id": "2", "memory": "Unrelated", "score": 0.1},
        ]


def _ltm(monkeypatch, min_score=None, failures=(), backoff="0.001"):
    monkeypatch.setenv("MEMORY_STRATEGY_ID", "ltms-x")
    monkeypatch.setenv("MEMORY_MAX_RETRIES", "3")
    monkeypatch.setenv("MEMORY_RETRY_BACKOFF_S", backoff)
    if min_score is not None:
        monkeypatch.setenv("LTM_MIN_SCORE", str(min_score))
    get_settings.cache_clear()
    ltm = AgentBaseLTM.__new__(AgentBaseLTM)
    s = get_settings()
    ltm._client, ltm._memory_id = FakeMemoryClient(failures), "mem-1"
    ltm._strategy_id, ltm._min_score = s.memory_strategy_id, s.ltm_min_score
    ltm._max_query = s.memory_query_max_chars
    ltm._max_retries, ltm._backoff_s = s.memory_max_retries, s.memory_retry_backoff_s
    return ltm


async def test_save_uses_request_model_and_namespace(monkeypatch):
    ltm = _ltm(monkeypatch)
    await ltm.save("u1", "Likes green tea")
    assert ltm._client.inserted == [("/strategies/ltms-x/actors/u1", ["Likes green tea"])]


async def test_search_parses_dicts_and_min_score(monkeypatch):
    assert await _ltm(monkeypatch).search("u1", "what to drink", 5) == [
        "Likes green tea",
        "Unrelated",
    ]
    assert await _ltm(monkeypatch, min_score=0.5).search("u1", "what to drink", 5) == [
        "Likes green tea"
    ]


async def test_long_query_truncated_to_api_limit(monkeypatch):
    ltm = _ltm(monkeypatch)
    assert await ltm.search("u1", "x" * 5000 + " final question", 5)  # no 400


async def test_search_limit_clamped_to_api_range(monkeypatch):
    """LTM_RECALL_LIMIT outside the API's 5–200 ⇒ the request stays in range, the result is still cut to it."""
    ltm = _ltm(monkeypatch)
    assert await ltm.search("u1", "what to drink", 1) == ["Likes green tea"]
    await ltm.search("u1", "what to drink", 500)
    assert ltm._client.limits == [5, 200]


@pytest.mark.parametrize("failures", [(429,), (429, 503), (500, 502, 504)])
async def test_recall_retries_429_and_5xx(monkeypatch, failures):
    """Bursts hit the 10-concurrent-requests limit (429): recall used to give up at once ⇒ silently no facts."""
    from app.memory.long_term import auto_recall

    ltm = _ltm(monkeypatch, failures=failures)
    assert await auto_recall(ltm, "u1", "what to drink", 5) == ["Likes green tea", "Unrelated"]
    assert ltm._client.calls == len(failures) + 1


async def test_remember_retries_429(monkeypatch):
    ltm = _ltm(monkeypatch, failures=(429, 429))
    await ltm.save("u1", "Likes green tea")
    assert ltm._client.inserted == [("/strategies/ltms-x/actors/u1", ["Likes green tea"])]
    assert ltm._client.calls == 3


@pytest.mark.parametrize("code", [400, 401, 403, 404, None])
async def test_no_retry_on_request_errors(monkeypatch, code):
    ltm = _ltm(monkeypatch, failures=(code,))
    with pytest.raises(GreenNodeRequestError):
        await ltm.save("u1", "x")
    assert ltm._client.calls == 1


async def test_retries_are_bounded(monkeypatch):
    ltm = _ltm(monkeypatch, failures=(429,) * 10)
    with pytest.raises(GreenNodeRequestError):
        await ltm.search("u1", "q", 5)
    assert ltm._client.calls == 4  # 1 + MEMORY_MAX_RETRIES


async def test_retry_backoff_is_cancellable(monkeypatch):
    """REQUEST_TIMEOUT_S cancels the turn ⇒ the backoff sleep must stop at once, with no further call."""
    ltm = _ltm(monkeypatch, failures=(429,) * 10, backoff="30")
    task = asyncio.create_task(ltm.search("u1", "q", 5))
    await asyncio.sleep(0.05)  # first call failed, now in backoff (≥ 15 s)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    assert ltm._client.calls == 1


async def test_memory_concurrency_limited():
    """Never exceed MEMORY_MAX_CONCURRENCY concurrent Memory calls (sync + async)."""
    import asyncio
    import threading
    import time

    from app.memory.short_term import _ConcurrencyLimitedClient

    state = {"now": 0, "peak": 0}
    lock = threading.Lock()

    def track(delta):
        with lock:
            state["now"] += delta
            state["peak"] = max(state["peak"], state["now"])

    class _Raw:
        def list_events(self):
            track(1)
            time.sleep(0.05)
            track(-1)

        async def search_memory_records_async(self):
            track(1)
            await asyncio.sleep(0.05)
            track(-1)

    c = _ConcurrencyLimitedClient(_Raw(), limit=3)
    loop = asyncio.get_running_loop()
    await asyncio.gather(
        *(loop.run_in_executor(None, c.list_events) for _ in range(6)),
        *(c.search_memory_records_async() for _ in range(6)),
    )
    assert state["peak"] <= 3


async def test_cancelled_waiter_does_not_leak_a_permit():
    """REQUEST_TIMEOUT_S cancels requests: a coroutine cancelled while WAITING for a permit must not take
    one later (a leak per timeout ⇒ after MEMORY_MAX_CONCURRENCY timeouts every Memory call hangs)."""
    import asyncio

    from app.memory.short_term import _ConcurrencyLimitedClient

    release = asyncio.Event()

    class _Raw:
        async def search_memory_records_async(self):
            await release.wait()

    c = _ConcurrencyLimitedClient(_Raw(), limit=2)
    holders = [asyncio.create_task(c.search_memory_records_async()) for _ in range(2)]
    await asyncio.sleep(0.02)  # both permits taken
    waiter = asyncio.create_task(c.search_memory_records_async())
    await asyncio.sleep(0.02)
    waiter.cancel()  # e.g. request timeout while waiting
    await asyncio.gather(waiter, return_exceptions=True)
    release.set()
    await asyncio.gather(*holders)
    await asyncio.sleep(0.1)  # let any stray acquirer run
    # Both permits are free again
    assert c._sem.acquire(blocking=False) and c._sem.acquire(blocking=False)
    c._sem.release()
    c._sem.release()


async def test_compression_never_deletes_unsummarized_or_splits_tool_pairs(fake_llm, monkeypatch):
    """Summarizer input over budget ⇒ only the summarized prefix may be removed, and neither the summarizer
    input nor the kept history may start with an orphan ToolMessage. Swept over many message sizes."""
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    from app.config import get_settings
    from app.memory.compression import summarize_if_needed

    monkeypatch.setenv("CONTEXT_MAX_TOKENS", "1000")
    monkeypatch.setenv("CONTEXT_HARD_LIMIT_TOKENS", "2000")
    monkeypatch.setenv("CONTEXT_KEEP_LAST", "2")
    get_settings.cache_clear()

    def call(i):
        return AIMessage("", tool_calls=[{"name": "t", "args": {}, "id": f"t{i}"}], id=f"a{i}")

    checked = 0
    for size in range(1000, 9000, 250):
        msgs = [
            HumanMessage("q1 " * 50, id="h1"),
            call(1),
            ToolMessage("r" * 3000, tool_call_id="t1", id="tm1"),
            AIMessage("x" * size, id="ans1"),
            HumanMessage("q2 " * 300, id="h2"),
            call(2),
            ToolMessage("r" * size, tool_call_id="t2", id="tm2"),
            AIMessage("y" * 1500, id="ans2"),
            HumanMessage("q3", id="h3"),
            AIMessage("ok", id="ans3"),
        ]
        model = fake_llm(AIMessage("- summary"))
        update = await summarize_if_needed(msgs, "", get_settings())
        if update is None:
            continue
        checked += 1
        seen = model.calls[0][
            1:-1
        ]  # minus system prompt and the final "Return the updated summary."
        removed = {m.id for m in update["messages"]}
        assert removed <= {m.id for m in seen}, f"size={size}: removed but never summarized"
        kept = [m for m in msgs if m.id not in removed]
        for part in (seen, kept):
            assert not isinstance(part[0], ToolMessage), f"size={size}: orphan ToolMessage"
            calls = {tc["id"] for m in part if isinstance(m, AIMessage) for tc in m.tool_calls}
            assert all(m.tool_call_id in calls for m in part if isinstance(m, ToolMessage))
    assert checked >= 10  # the sweep really exercised compression


def test_ltm_on_agentbase_requires_real_strategy_id(monkeypatch):
    import pytest

    from app.config import get_settings

    for k, v in {
        "MEMORY_BACKEND": "agentbase",
        "MEMORY_ID": "mem-1",
        "LTM_ENABLED": "true",
    }.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("MEMORY_STRATEGY_ID", "default")
    get_settings.cache_clear()
    with pytest.raises(ValueError, match="MEMORY_STRATEGY_ID"):
        get_settings()
    monkeypatch.setenv("MEMORY_STRATEGY_ID", "strat-123")
    get_settings.cache_clear()
    assert get_settings().memory_strategy_id == "strat-123"


def test_huge_user_message_is_truncated_to_budget(monkeypatch):
    from langchain_core.messages import HumanMessage, SystemMessage
    from langchain_core.messages.utils import count_tokens_approximately

    from app.config import get_settings
    from app.memory.compression import fit_to_budget

    monkeypatch.setenv("CONTEXT_HARD_LIMIT_TOKENS", "2000")
    get_settings.cache_clear()
    msgs = [SystemMessage("sys"), HumanMessage("START " + "x" * 400_000 + " END")]
    out = fit_to_budget(msgs, get_settings())
    assert count_tokens_approximately(out) <= 2000
    text = out[-1].content
    assert text.startswith("START") and text.endswith("END") and "truncated" in text


def test_user_message_kept_when_ai_part_is_the_problem(monkeypatch):
    """Over budget because of AI text / tool args in the current turn ⇒ do not mangle the user's question."""
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

    from app.config import get_settings
    from app.memory.compression import fit_to_budget

    monkeypatch.setenv("CONTEXT_HARD_LIMIT_TOKENS", "2000")
    get_settings.cache_clear()
    question = "q" * 1550
    msgs = [
        SystemMessage("sys"),
        HumanMessage(question),
        AIMessage("", tool_calls=[{"name": "t", "args": {"blob": "a" * 20000}, "id": "t1"}]),
        AIMessage("b" * 6000),
    ]
    out = fit_to_budget(msgs, get_settings())
    assert next(m for m in out if isinstance(m, HumanMessage)).content == question


async def test_async_caller_not_starved_by_sync_bridge_calls():
    """The bridge's sync calls (executor threads, blocking acquire) must not win every release against LTM's async
    calls: an async caller waits its FIFO turn, not the whole contention window (seen: 2.96 s vs 0.02 s idle)."""
    import asyncio
    import threading
    import time

    from app.memory.short_term import _ConcurrencyLimitedClient

    entered: list[float] = []
    stop = threading.Event()

    class _Raw:
        def list_events(self):
            time.sleep(0.01)  # a checkpoint read

        async def search_memory_records_async(self):
            entered.append(time.monotonic())

    c = _ConcurrencyLimitedClient(_Raw(), limit=2)

    def bridge_worker():
        while not stop.is_set():
            c.list_events()

    workers = [threading.Thread(target=bridge_worker, daemon=True) for _ in range(6)]
    for w in workers:
        w.start()
    threading.Timer(2.0, stop.set).start()  # contention window
    try:
        await asyncio.sleep(0.1)  # saturated
        start = time.monotonic()
        await c.search_memory_records_async()
        waited = entered[0] - start
    finally:
        stop.set()
        for w in workers:
            w.join()
    assert waited < 0.5, f"async caller starved for {waited:.2f}s under sync contention"


async def test_permit_handed_to_a_cancelled_waiter_is_passed_on():
    """Race: release() hands the permit to waiter A, then A is cancelled before it resumes ⇒ A must pass it on
    (to B, or back to the pool) — otherwise every such timeout leaks a permit."""
    import asyncio

    from app.memory.short_term import _FairLimiter

    lim = _FairLimiter(1)
    assert lim.acquire(blocking=False)
    a = asyncio.create_task(lim.acquire_async())
    b = asyncio.create_task(lim.acquire_async())
    await asyncio.sleep(0)  # both queued, A first
    lim.release()  # handed to A (wake-up scheduled)...
    a.cancel()  # ...but A is cancelled before it runs
    await asyncio.gather(a, return_exceptions=True)
    await asyncio.wait_for(b, 1)  # B got the permit A passed on
    c = asyncio.create_task(lim.acquire_async())
    await asyncio.sleep(0)
    lim.release()  # B done ⇒ handed to C
    c.cancel()  # nobody else waiting ⇒ the permit goes back to the pool
    await asyncio.gather(c, return_exceptions=True)
    assert lim.acquire(blocking=False)
    lim.release()


async def test_limiter_mixed_load_with_cancellations_never_leaks_or_exceeds():
    import asyncio
    import random
    import threading
    import time

    from app.memory.short_term import _ConcurrencyLimitedClient

    state = {"now": 0, "peak": 0}
    lock = threading.Lock()

    def track(delta):
        with lock:
            state["now"] += delta
            state["peak"] = max(state["peak"], state["now"])

    class _Raw:
        def list_events(self):
            track(1)
            time.sleep(random.uniform(0, 0.005))
            track(-1)

        async def search_memory_records_async(self):
            track(1)
            try:
                await asyncio.sleep(random.uniform(0, 0.005))
            finally:
                track(-1)

    c = _ConcurrencyLimitedClient(_Raw(), limit=3)
    threads = [
        threading.Thread(target=lambda: [c.list_events() for _ in range(30)]) for _ in range(4)
    ]
    for t in threads:
        t.start()
    tasks = [asyncio.create_task(c.search_memory_records_async()) for _ in range(120)]
    for t in random.sample(tasks, 60):
        await asyncio.sleep(random.uniform(0, 0.002))
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await asyncio.get_running_loop().run_in_executor(None, lambda: [t.join() for t in threads])
    assert state["peak"] <= 3 and state["now"] == 0
    assert c._sem._free == 3 and not c._sem._waiters  # every permit came back
