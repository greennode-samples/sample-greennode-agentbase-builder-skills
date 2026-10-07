"""Short-term memory = LangGraph checkpointer (per-session conversation history).

- agentbase: `AgentBaseMemoryEvents` stores checkpoints as events in AgentBase Memory,
  keyed by (thread_id = session_id, actor_id = user_id). Survives restarts/scale-out.
- inmemory: `InMemorySaver` for local/test — lost on restart, do NOT use in prod.

Required config when invoking the graph:
    {"configurable": {"thread_id": <session_id>, "actor_id": <user_id>}}
"""

from __future__ import annotations

import asyncio
import inspect
import threading
from collections import deque
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver

from app.config import Settings

_checkpointer: BaseCheckpointSaver | None = None


class _FairLimiter:
    """FIFO permits shared by SYNC callers (the bridge's create_event/list_events, run in executor threads) and
    ASYNC callers (LTM's *_async) — a plain semaphore + async polling starved the async side: blocked threads won
    every release, so recall waited out the whole contention window (measured 2.96 s vs 0.02 s idle).

    A permit is HANDED OVER on release to the oldest waiter (thread: Event; coroutine: Future on its loop), so
    nobody can barge in. Cancellation-safe (REQUEST_TIMEOUT_S cancels requests): a coroutine cancelled while waiting
    leaves the queue; if the permit was handed to it at that very moment, it passes the permit on — never leaked.
    """

    class _Waiter:
        __slots__ = ("event", "future", "loop", "granted")

        def __init__(self, event=None, future=None, loop=None):
            self.event: threading.Event | None = event
            self.future: asyncio.Future | None = future
            self.loop: asyncio.AbstractEventLoop | None = loop
            self.granted = False

    def __init__(self, limit: int):
        self._limit = max(1, limit)
        self._free = self._limit
        self._lock = threading.Lock()
        self._waiters: deque[_FairLimiter._Waiter] = deque()

    def acquire(self, blocking: bool = True) -> bool:
        """Sync acquire (blocks the calling THREAD — never call it on the event loop)."""
        with self._lock:
            if self._free and not self._waiters:
                self._free -= 1
                return True
            if not blocking:
                return False
            waiter = self._Waiter(event=threading.Event())
            self._waiters.append(waiter)
        waiter.event.wait()  # type: ignore[union-attr]  # the permit was handed over before the event was set
        return True

    async def acquire_async(self) -> None:
        loop = asyncio.get_running_loop()
        with self._lock:
            if self._free and not self._waiters:
                self._free -= 1
                return
            waiter = self._Waiter(future=loop.create_future(), loop=loop)
            self._waiters.append(waiter)
        try:
            await waiter.future  # type: ignore[misc]
        except BaseException:  # cancelled (request timeout) while waiting
            with self._lock:
                handed_over = waiter.granted
                if not handed_over:
                    self._waiters.remove(waiter)
            if handed_over:
                self.release()  # got the permit at the moment of cancellation ⇒ pass it on
            raise

    def release(self) -> None:
        with self._lock:
            while self._waiters:
                waiter = self._waiters.popleft()
                waiter.granted = True
                if waiter.event is not None:
                    waiter.event.set()
                    return
                try:
                    waiter.loop.call_soon_threadsafe(_wake, waiter.future)  # type: ignore[union-attr]
                    return
                except (
                    RuntimeError
                ):  # its event loop is closed — nobody will use the permit, try the next
                    continue
            if self._free >= self._limit:
                raise ValueError("_FairLimiter released too many times")
            self._free += 1

    def __enter__(self) -> _FairLimiter:
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


def _wake(future: asyncio.Future) -> None:
    if not future.done():  # already cancelled ⇒ its waiter passes the permit on (acquire_async)
        future.set_result(None)


class _ConcurrencyLimitedClient:
    """MemoryClient proxy: limits concurrent Memory calls within the process.

    Seen in practice: 8 parallel requests ⇒ Memory API returns 429 "Too many concurrent streaming requests
    for this user. Limit: 10" (limit per IAM account, shared across all replicas). The bridge calls SYNC
    functions (create_event, list_events...) in a thread executor, LTM calls ASYNC functions (*_async) ⇒ both share
    one FIFO `_FairLimiter` (fair between the two paths, cancellation-safe, never blocks the event loop).
    """

    def __init__(self, client: Any, limit: int):
        self._client = client
        self._sem = _FairLimiter(limit)

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._client, name)
        if not callable(attr) or name.startswith("_"):
            return attr
        if inspect.iscoroutinefunction(attr):

            async def async_limited(*args: Any, **kwargs: Any) -> Any:
                await self._sem.acquire_async()
                try:
                    return await attr(*args, **kwargs)
                finally:
                    self._sem.release()

            return async_limited

        def limited(*args: Any, **kwargs: Any) -> Any:
            with self._sem:
                return attr(*args, **kwargs)

        return limited


_memory_client: Any = None


def memory_client(settings: Settings) -> Any:
    """Shared MemoryClient (1 FIFO limiter/process) with a short timeout + concurrency limit.

    SDK 1.0.x doesn't accept timeout in the constructor; the internal HttpxClient reads `timeout` when it first
    connects ⇒ set it before the first request."""
    global _memory_client
    if _memory_client is None:
        from greennode_agentbase.memory import MemoryClient

        client = MemoryClient()
        http = getattr(client, "_http_client", None)
        if http is not None and hasattr(http, "timeout"):
            http.timeout = settings.memory_timeout_s
        _memory_client = _ConcurrencyLimitedClient(client, settings.memory_max_concurrency)
    return _memory_client


def get_checkpointer(settings: Settings) -> BaseCheckpointSaver:
    global _checkpointer
    if _checkpointer is None:
        if settings.memory_backend == "agentbase":
            from greennode_agent_bridge import AgentBaseMemoryEvents

            _checkpointer = AgentBaseMemoryEvents(
                memory_id=settings.memory_id,
                memory_client=memory_client(settings),
                max_retries=settings.memory_max_retries,  # bridge default 5
                initial_backoff=settings.memory_retry_backoff_s,  # bridge default 0.1s — too short for 429
            )
        else:
            from langgraph.checkpoint.memory import InMemorySaver

            _checkpointer = InMemorySaver()
    return _checkpointer


def thread_config(settings: Settings, *, session_id: str, user_id: str, **extra) -> dict:
    """Standard config for every graph invocation.

    AgentBaseMemoryEvents isolates by (thread_id, actor_id) itself; InMemorySaver keys only by
    thread_id, so the user is prepended so another user reusing the session_id can't read the history.
    """
    from app.auth.inbound import validate_session_id, validate_user_id

    # Last line of defense for EVERY graph call: both ids go into Memory API paths
    validate_user_id(user_id)
    validate_session_id(session_id)
    thread_id = session_id if settings.memory_backend == "agentbase" else f"{user_id}::{session_id}"
    return {"configurable": {"thread_id": thread_id, "actor_id": user_id, **extra}}
