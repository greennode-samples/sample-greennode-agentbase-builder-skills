"""AgentBase Runtime entrypoint. KEEP THIS FILE THIN — logic lives in app/.

Platform contract: listen on 0.0.0.0:8080, GET /health returns 200 (local dev with AUTH_MODE=none: 127.0.0.1).
The SDK provides POST /invocations; the handler is async and returns a dict (JSON) or an async generator (SSE).
"""

from __future__ import annotations

import asyncio
import contextlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from greennode_agentbase import GreenNodeAgentBaseApp, PingStatus, RequestContext  # noqa: E402
from greennode_agentbase.runtime.app import XAccelBufferingMiddleware  # noqa: E402
from starlette.middleware import Middleware  # noqa: E402
from starlette.middleware.cors import CORSMiddleware  # noqa: E402
from starlette.requests import Request  # noqa: E402
from starlette.responses import JSONResponse  # noqa: E402
from starlette.routing import Route  # noqa: E402

from app import health, service  # noqa: E402
from app.a2a.client import warn_if_timeouts_unsafe  # noqa: E402
from app.auth.inbound import prefetch_jwks  # noqa: E402
from app.config import Settings, get_settings  # noqa: E402
from app.limits import RequestLimitsMiddleware  # noqa: E402
from app.logging_setup import configure_logging  # noqa: E402
from app.observability import tracing  # noqa: E402
from app.reflection import drain_background  # noqa: E402
from app.tools.mcp import log_iam_principal  # noqa: E402

settings = get_settings()
configure_logging(settings)
warn_if_timeouts_unsafe(settings)  # A2A client: this agent's timeouts vs A2A_TIMEOUT_S


def bind_host(s: Settings, dockerenv: Path = Path("/.dockerenv")) -> str:
    """Loopback for a local run without auth: AUTH_MODE=none trusts the User-Id header, so on 0.0.0.0 anyone on
    the LAN could call the agent as any user. A container needs 0.0.0.0 (`make docker-run` publishes it on
    127.0.0.1 only); APP_ENV=local is refused on the Runtime (which has no /.dockerenv)."""
    if s.is_local and s.auth_mode == "none" and not dockerenv.exists():
        return "127.0.0.1"
    return "0.0.0.0"


@contextlib.asynccontextmanager
async def lifespan(_app):
    # Blocking calls (Memory bridge, JWKS) must not queue user authentication behind a slow Memory (EXECUTOR_THREADS)
    asyncio.get_running_loop().set_default_executor(
        ThreadPoolExecutor(max_workers=settings.executor_threads, thread_name_prefix="agent")
    )
    tracing.init_tracing(settings)
    await prefetch_jwks(settings)  # the first request doesn't wait for the JWKS download
    # Policy Group principal iam:<sub> in the startup log (never fails startup)
    await log_iam_principal(settings)
    try:
        yield
    finally:
        # Background self-evals (REFLECTION_MODE) run after their response: finish or cancel them BEFORE the flush
        await drain_background()
        tracing.shutdown_tracing()


# Passing middleware => the SDK drops its default middleware, so XAccelBufferingMiddleware (SSE) must be re-added.
# Outermost: refuse oversized bodies and overload BEFORE anything parses or queues them (app/limits.py)
middleware = [
    Middleware(
        RequestLimitsMiddleware,
        max_body_bytes=settings.max_request_bytes,
        max_concurrent=settings.max_concurrent_requests,
    ),
    Middleware(XAccelBufferingMiddleware),
]
if settings.cors_allow_origins:
    middleware.append(
        Middleware(
            CORSMiddleware,
            allow_origins=settings.cors_allow_origins,
            allow_methods=["POST", "GET", "OPTIONS"],
            allow_headers=["*"],
        )
    )

app = GreenNodeAgentBaseApp(lifespan=lifespan, middleware=middleware)


async def ready(_request: Request) -> JSONResponse:
    """Readiness (app/health.py): 200 ready / degraded, 503 when users cannot be authenticated. `/health` stays the
    platform's liveness probe. Names failing components only — reasons are in the log."""
    status, failing = await health.readiness(settings, lambda: prefetch_jwks(settings))
    body = {"status": status, "failing": failing, "version": settings.agent_version}
    return JSONResponse(body, status_code=503 if status == "not_ready" else 200)


# BEFORE the A2A routes below: their Mount("") answers every path that comes after it
app.router.routes.append(Route("/ready", ready, methods=["GET"]))

if (
    settings.a2a_enabled
):  # A2A server: /.well-known/agent-card.json + POST /a2a (xem app/a2a/server.py)
    from app.a2a.server import a2a_routes

    app.router.routes.extend(a2a_routes(settings))


@app.entrypoint
async def handler(payload: dict, context: RequestContext):
    return await service.handle(payload, context)


@app.ping
def health_check() -> PingStatus:
    return PingStatus.HEALTHY


if __name__ == "__main__":
    app.run(port=8080, host=bind_host(settings))
