"""What this replica knows about the services it needs, and `GET /ready` (main.py) that reports it.

`/health` (the platform's liveness probe) stays 200 while the process runs: restarting a replica does not fix an
identity provider or an LLM provider that is down. `/ready` tells an operator, a smoke test or a load balancer more:

  ready      this replica can serve requests
  degraded   it can, but a dependency failed recently (HTTP 200, `failing` names it) — the LLM key was rejected, the
             Memory store refused a call, a provider is overloaded. Fed by what real requests did, so it needs no extra
             calls; a failure older than STALE_S is forgotten, a success clears it
  not_ready  it cannot authenticate users (AUTH_MODE=jwt and the JWKS cannot be fetched) — HTTP 503. `/ready` re-fetches
             the JWKS itself (at most every PROBE_EVERY_S), so a replica that recovers says so without waiting for a
             user request

The response names failing components only — never URLs, keys or error text (those stay in the log).
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable

from app.config import Settings

STALE_S = 300.0  # a failure nobody has seen again for this long is forgotten
PROBE_EVERY_S = 10.0  # `/ready` re-checks a failed JWKS at most this often

# component -> (ok, monotonic time, reason for the log)
_state: dict[str, tuple[bool, float, str]] = {}
_last_probe = 0.0


def record(component: str, ok: bool, reason: str = "") -> None:
    """Called where a dependency is used: `jwks` (auth), `llm` and `backing_service` (service.py)."""
    _state[component] = (ok, time.monotonic(), reason)


def failing(now: float | None = None) -> dict[str, str]:
    """component -> reason, for the failures seen within STALE_S."""
    now = time.monotonic() if now is None else now
    return {c: why for c, (ok, at, why) in _state.items() if not ok and now - at < STALE_S}


def reset() -> None:
    """Tests only."""
    global _last_probe
    _state.clear()
    _last_probe = 0.0


async def readiness(
    settings: Settings, probe_jwks: Callable[[], Awaitable[None]] | None = None
) -> tuple[str, list[str]]:
    """("ready" | "degraded" | "not_ready", names of the failing components)."""
    global _last_probe
    bad = failing()
    if "jwks" in bad and probe_jwks is not None and time.monotonic() - _last_probe >= PROBE_EVERY_S:
        _last_probe = time.monotonic()
        await probe_jwks()  # records the outcome
        bad = failing()
    names = sorted(bad)
    if settings.auth_mode == "jwt" and "jwks" in bad:
        return "not_ready", names
    return ("degraded" if names else "ready"), names
