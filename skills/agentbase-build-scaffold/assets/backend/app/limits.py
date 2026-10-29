"""Request limits — admission control in front of the agent, so one caller or one burst can't take it down.

- Body size (middleware): a POST body over MAX_REQUEST_BYTES ⇒ 413 before anything parses it (Content-Length first,
  then counted while it arrives — a chunked body without Content-Length is cut too). Without it the SDK parsed a
  50 MB body (≈ 200 MB of RAM) before the message-length check could refuse it.
- Concurrency (middleware): more than MAX_CONCURRENT_REQUESTS agent requests (/invocations, /a2a — an open SSE stream
  counts until it ends) ⇒ 503 + Retry-After at once, instead of queueing until every request times out.
- Rate (service.handle, AFTER auth — the keys are the VERIFIED user and tenant): chat / resume per user and per tenant
  over a sliding minute ⇒ 429 + details.retry_after_s. The LLM (MaaS: 10 requests/min per account) and Memory
  (10 concurrent per account) quotas are shared by every user: without this one user can starve all the others.

Every limit is per replica (in process) ⇒ the effective limit is × the replica count. A strict global quota belongs in
front of the runtime (API gateway) — skill agentbase-build-deploy. Eval / jobs / A2A call service.run_chat in process:
no rate limit there (A2A requests still pass the middleware).
"""

from __future__ import annotations

import json
import math
import time
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable
from typing import Any

from app.config import Settings

AGENT_PATHS = ("/invocations", "/a2a")
# error body = the SDK's format ({"error", "error_type", "details"}) — clients parse one shape
_ERROR_TYPE = "GreenNodeRequestError"

Scope = dict[str, Any]
Receive = Callable[[], Awaitable[dict]]
Send = Callable[[dict], Awaitable[None]]


async def _reject(send: Send, status: int, message: str, retry_after_s: int | None = None) -> None:
    details = {"retry_after_s": retry_after_s} if retry_after_s else {}
    body = json.dumps({"error": message, "error_type": _ERROR_TYPE, "details": details}).encode()
    headers = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]
    if retry_after_s:
        headers.append((b"retry-after", str(retry_after_s).encode()))
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


class RequestLimitsMiddleware:
    """Pure ASGI (works for SSE): body size on every POST, concurrency on the agent paths."""

    def __init__(self, app: Any, *, max_body_bytes: int, max_concurrent: int):
        self.app = app
        self.max_body_bytes = max_body_bytes
        self.max_concurrent = max_concurrent
        self.in_flight = 0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method") != "POST":
            await self.app(scope, receive, send)
            return
        length = dict(scope.get("headers") or []).get(b"content-length")
        if length is not None and (not length.isdigit() or int(length) > self.max_body_bytes):
            await _reject(send, 413, f"Request body exceeds {self.max_body_bytes} bytes")
            return
        agent_path = scope.get("path") in AGENT_PATHS
        if agent_path and self.max_concurrent and self.in_flight >= self.max_concurrent:
            await _reject(send, 503, "The agent is busy, try again shortly", retry_after_s=2)
            return
        # Read the (bounded) body here, then replay it: the app never sees more than the limit. Later receive() calls
        # go to the server again — an SSE stream needs them to notice the client leaving (http.disconnect).
        chunks, size = [], 0
        while True:
            message = await receive()
            if message["type"] != "http.request":
                await self.app(
                    scope, _replay([message], receive), send
                )  # disconnected while sending
                return
            size += len(message.get("body", b""))
            if size > self.max_body_bytes:
                await _reject(send, 413, f"Request body exceeds {self.max_body_bytes} bytes")
                return
            chunks.append(message.get("body", b""))
            if not message.get("more_body"):
                break
        body = {"type": "http.request", "body": b"".join(chunks), "more_body": False}
        if not agent_path:
            await self.app(scope, _replay([body], receive), send)
            return
        self.in_flight += 1
        try:
            await self.app(scope, _replay([body], receive), send)
        finally:
            self.in_flight -= 1


def _replay(messages: list[dict], receive: Receive) -> Receive:
    pending = list(messages)

    async def replayed() -> dict:
        return pending.pop(0) if pending else await receive()

    return replayed


class SlidingWindowLimiter:
    """Hits per key over the last `window_s` seconds, in process. Bounded: at most `max_keys` keys are tracked (the
    least recently used are dropped) and each key keeps at most `limit` timestamps."""

    def __init__(self, window_s: float = 60.0, max_keys: int = 100_000):
        self.window_s = window_s
        self.max_keys = max_keys
        self._hits: OrderedDict[str, deque[float]] = OrderedDict()

    def _queue(self, key: str, now: float) -> deque[float]:
        q = self._hits.get(key)
        if q is None:
            q = self._hits[key] = deque()
            while len(self._hits) > self.max_keys:
                self._hits.popitem(last=False)
        else:
            self._hits.move_to_end(key)
        while q and q[0] <= now - self.window_s:
            q.popleft()
        return q

    def acquire(self, limits: list[tuple[str, int]], now: float | None = None) -> float:
        """Record one hit on every key if ALL are under their limit (limit ≤ 0 ⇒ unlimited). Returns 0 when recorded,
        else the seconds until the most constrained key frees a slot (nothing recorded)."""
        now = time.monotonic() if now is None else now
        active = [(self._queue(key, now), limit) for key, limit in limits if limit > 0]
        wait = max(
            (q[0] + self.window_s - now for q, limit in active if len(q) >= limit), default=0.0
        )
        if wait > 0:
            return wait
        for q, _ in active:
            q.append(now)
        return 0.0


_limiter = SlidingWindowLimiter()


def rate_limit_wait(user_id: str, tenant_id: str | None, settings: Settings) -> int:
    """Seconds the caller must wait (0 ⇒ allowed, and counted) — chat / resume per verified user and tenant."""
    limits = [(f"user:{user_id}", settings.rate_limit_user_per_min)]
    if tenant_id:
        limits.append((f"tenant:{tenant_id}", settings.rate_limit_tenant_per_min))
    wait = _limiter.acquire(limits)
    return math.ceil(wait) if wait > 0 else 0


def reset_rate_limits() -> None:
    """Tests only."""
    _limiter._hits.clear()
