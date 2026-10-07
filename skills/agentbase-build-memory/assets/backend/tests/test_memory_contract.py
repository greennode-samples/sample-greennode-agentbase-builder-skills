"""Contract tests against the greennode-agentbase SDK (real bugs hit on deploy):
- insert-directly must use MemoryRecordInsertDirectlyRequest (passing a list => TypeError in the SDK)
- search returns list[dict] {id, memory, score, ...}
"""

from __future__ import annotations

from greennode_agentbase.memory.models import (
    MemoryRecordInsertDirectlyRequest,
    MemoryRecordSearchRequest,
)

from app.config import get_settings
from app.memory.long_term import AgentBaseLTM


class FakeMemoryClient:
    def __init__(self):
        self.inserted = []

    async def insert_memory_records_directly_async(self, *, id, namespace, request):
        assert isinstance(request, MemoryRecordInsertDirectlyRequest)
        self.inserted.append((namespace, request.memory_records))
        return {}

    async def search_memory_records_async(self, *, id, namespace, request):
        assert isinstance(request, MemoryRecordSearchRequest)
        assert len(request.query) <= 1000  # Memory API limit (400 if exceeded)
        return [
            {"id": "1", "memory": "Likes green tea", "score": 0.71},
            {"id": "2", "memory": "Unrelated", "score": 0.1},
        ]


def _ltm(monkeypatch, min_score=None):
    monkeypatch.setenv("MEMORY_STRATEGY_ID", "ltms-x")
    if min_score is not None:
        monkeypatch.setenv("LTM_MIN_SCORE", str(min_score))
    get_settings.cache_clear()
    ltm = AgentBaseLTM.__new__(AgentBaseLTM)
    s = get_settings()
    ltm._client, ltm._memory_id = FakeMemoryClient(), "mem-1"
    ltm._strategy_id, ltm._min_score = s.memory_strategy_id, s.ltm_min_score
    ltm._max_query = s.memory_query_max_chars
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
