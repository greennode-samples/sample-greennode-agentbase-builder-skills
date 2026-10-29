"""A2A (Agent2Agent) server — lets OTHER agents call this agent via the A2A standard (a2a-sdk 1.x, JSON-RPC).

Routes (same container :8080, next to /invocations):
  GET  /.well-known/agent-card.json   Agent Card (public — describes skills + how to authenticate)
  POST /a2a                           JSON-RPC: SendMessage, GetTask, ListTasks, CancelTask...

Authentication & per-user isolation (same as /invocations):
  - Every /a2a request goes through `authenticate()` (AUTH_MODE jwt | api_key) ⇒ same HTTP status as
    /invocations (401 invalid credentials, 403 User-Id ≠ token subject, 400 missing User-Id, 503 IdP down).
  - The authenticated user is attached to `request.user` ⇒ the a2a-sdk task store keys tasks by owner =
    user_id ⇒ user B cannot GetTask/ListTasks user A's tasks.
  - The verified Principal (claims + token) rides on `request.auth` and is passed to run_chat/run_resume ⇒
    tools see the same AUTH_FORWARD_CLAIMS / user token as on /invocations (RBAC, user_jwt).
  - A2A `contextId` ⇒ session_id `a2a-<contextId>`; memory is keyed by (session, user) like every other flow.
  - HITL: graph pauses ⇒ task `INPUT_REQUIRED` + description of the tool awaiting approval; the caller sends
    the next message on the same contextId with "approve" or "reject: <reason>" to continue. A decision word
    is a decision ONLY while something is pending; otherwise it is answered "Nothing is awaiting confirmation"
    without calling the LLM (a relayed "approve" must never reach the model as a chat message).
  - Unexpected errors ⇒ task FAILED with a generic message (details only in the log), never str(exception).
"""

from __future__ import annotations

import logging
import time
import uuid
from collections import OrderedDict

from a2a.auth.user import User
from a2a.helpers.proto_helpers import new_task, new_text_message
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.context import ServerCallContext
from a2a.server.events.event_queue_v2 import EventQueue
from a2a.server.owner_resolver import resolve_user_scope
from a2a.server.request_handlers import DefaultRequestHandlerV2
from a2a.server.routes.agent_card_routes import create_agent_card_routes
from a2a.server.routes.jsonrpc_routes import create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import a2a_pb2 as pb
from greennode_agentbase import RequestContext as AgentBaseRequestContext
from greennode_agentbase.exceptions import GreenNodeRequestError
from starlette.applications import Starlette
from starlette.authentication import (
    AuthCredentials,
    AuthenticationBackend,
    AuthenticationError,
    SimpleUser,
)
from starlette.middleware import Middleware
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.requests import HTTPConnection
from starlette.responses import JSONResponse
from starlette.routing import BaseRoute, Mount

from app.a2a.decisions import code_matches, confirmation_code, parse_decision
from app.auth.inbound import Principal, authenticate_async, validate_session_id
from app.config import Settings, get_settings

log = logging.getLogger(__name__)

RPC_PATH = "/a2a"


# --------------------------------------------------------------------------- auth
class PrincipalCredentials(AuthCredentials):
    """`request.auth` for /a2a: carries the verified Principal. a2a-sdk copies `request.auth` into
    `ServerCallContext.state["auth"]` ⇒ the executor reads it there (request.user only holds the name)."""

    def __init__(self, principal: Principal):
        super().__init__(["authenticated"])
        self.principal = principal


class A2AAuthError(AuthenticationError):
    """Keeps the status of the inbound-auth error (401 / 403 / 400 / 503) instead of a blanket 401."""

    def __init__(self, message: str, status_code: int):
        super().__init__(message)
        self.status_code = status_code


class AgentBaseAuthBackend(AuthenticationBackend):
    """Reuses the agent's inbound auth for A2A (same headers as /invocations)."""

    async def authenticate(self, conn: HTTPConnection):
        headers = dict(conn.headers)
        ctx = AgentBaseRequestContext(
            session_id=None,
            user_id=conn.headers.get("x-greennode-agentbase-user-id"),
            request_headers=headers,
        )
        try:
            principal = await authenticate_async(ctx, get_settings())
        except GreenNodeRequestError as e:
            raise A2AAuthError(e.message, e.status_code or 401) from e
        # SimpleUser.display_name = user_id = the a2a-sdk task owner
        return PrincipalCredentials(principal), SimpleUser(principal.user_id)


def _auth_error(_conn: HTTPConnection, exc: Exception) -> JSONResponse:
    return JSONResponse({"error": str(exc)}, status_code=getattr(exc, "status_code", 401))


def _verified_principal(context: RequestContext) -> Principal:
    """The Principal AgentBaseAuthBackend verified for this request — fail closed if it is missing."""
    call = context.call_context
    principal = getattr((call.state or {}).get("auth") if call else None, "principal", None)
    if not isinstance(principal, Principal) or principal.user_id != call.user.user_name:
        raise GreenNodeRequestError("Unauthenticated A2A request", status_code=401)
    return principal


# --------------------------------------------------------------------------- task store
class _Owner(User):
    """Just enough of a user to address one owner's tasks when evicting."""

    def __init__(self, name: str):
        self._name = name

    @property
    def is_authenticated(self) -> bool:
        return True

    @property
    def user_name(self) -> str:
        return self._name


class BoundedTaskStore(InMemoryTaskStore):
    """InMemoryTaskStore that cannot grow without bound: at most `max_tasks`, none kept idle past `ttl_s`.

    The SDK's store never forgets, so a long-running replica held every task of every caller forever. A task is
    "idle" from its last save; the oldest go first. Tasks are per replica — the conversation (checkpoint, pending
    approval) is in Memory and works on any replica; only `GetTask` / `ListTasks` of an old task needs the replica
    that ran it. A shared store (a2a-sdk's DatabaseTaskStore) is the answer if callers poll tasks across replicas."""

    def __init__(self, max_tasks: int = 10_000, ttl_s: float = 3600.0):
        super().__init__()
        self.max_tasks = max_tasks
        self.ttl_s = ttl_s
        self._seen: OrderedDict[tuple[str, str], float] = (
            OrderedDict()
        )  # (owner, task id) → last save, oldest first

    async def save(self, task, context: ServerCallContext) -> None:
        await super().save(task, context)
        key = (resolve_user_scope(context), task.id)
        self._seen[key] = time.monotonic()
        self._seen.move_to_end(key)
        await self._evict()

    async def delete(self, task_id: str, context: ServerCallContext) -> None:
        await super().delete(task_id, context)
        self._seen.pop((resolve_user_scope(context), task_id), None)

    async def _evict(self) -> None:
        now = time.monotonic()
        while self._seen:
            (owner, task_id), at = next(iter(self._seen.items()))
            if len(self._seen) <= self.max_tasks and now - at < self.ttl_s:
                return
            self._seen.popitem(last=False)
            await super().delete(task_id, ServerCallContext(user=_Owner(owner)))


# --------------------------------------------------------------------------- executor
def _text_of(message: pb.Message | None) -> str:
    return "\n".join(p.text for p in (message.parts if message else []) if p.text).strip()


NOTHING_PENDING = (
    "Nothing is awaiting confirmation — the decision was already handled or has expired. "
    "Send a new message."
)
FAILED_GENERIC = "The agent failed to process the request."


def _describe(calls: list[dict]) -> str:
    return ", ".join(f"{t['name']}({t['args']})" for t in calls)


def _code_for(user_id: str, session_id: str, calls: list[dict]) -> str:
    return confirmation_code(
        get_settings().signing_key, user_id, session_id, [t["id"] for t in calls]
    )


def _ask_text(lead: str, user_id: str, session_id: str, calls: list[dict]) -> str:
    """The confirmation request: what would run, and the code that approving it needs (see decisions.py)."""
    code = _code_for(user_id, session_id, calls)
    return (
        f"{lead}: {_describe(calls)}. Reply 'approve {code}' to run it, or 'reject: <reason>' to cancel. "
        "Only the person this request is for may decide."
    )


class AgentBaseExecutor(AgentExecutor):
    """Wraps the standard graph (service.run_chat / run_resume) as an A2A task."""

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        try:
            await self._execute(context, event_queue, updater)
        except Exception:  # noqa: BLE001 — a2a-sdk would return str(e) to the caller as an InternalError
            log.exception("A2A task %s failed", context.task_id)
            await updater.failed(updater.new_agent_message([pb.Part(text=FAILED_GENERIC)]))

    async def _execute(
        self, context: RequestContext, event_queue: EventQueue, updater: TaskUpdater
    ) -> None:
        from app import service  # avoid a circular import at app init

        session_id = f"a2a-{context.context_id}"
        if (
            context.current_task is None
        ):  # A2A v1: the Task must be enqueued before any status update
            await event_queue.enqueue_event(
                new_task(
                    context.task_id,
                    context.context_id,
                    pb.TASK_STATE_SUBMITTED,
                    history=[context.message] if context.message else None,
                )
            )
        text = _text_of(context.message)
        await updater.start_work()
        try:
            principal = _verified_principal(context)
        except GreenNodeRequestError as e:
            await updater.failed(updater.new_agent_message([pb.Part(text=e.message)]))
            return
        user_id = principal.user_id
        resuming = False
        try:
            validate_session_id(
                session_id
            )  # contextId ends up in the Memory path (prevent traversal)
            decision = parse_decision(text)
            pending = (
                await service.pending_tool_calls(user_id=user_id, session_id=session_id)
                if decision
                else []
            )
            if decision and not pending:
                # Deterministic, no LLM call: a stray / duplicate "approve" is not a chat message
                await updater.failed(updater.new_agent_message([pb.Part(text=NOTHING_PENDING)]))
                return
            if pending:
                approve, reason, code = decision
                if approve and not code_matches(code, _code_for(user_id, session_id, pending)):
                    # A bare / wrong / stale "approve" decides nothing: it is answered with the request again
                    await updater.requires_input(
                        updater.new_agent_message(
                            [
                                pb.Part(
                                    text=_ask_text(
                                        "Approving needs the code of this request — an action is awaiting "
                                        "confirmation",
                                        user_id,
                                        session_id,
                                        pending,
                                    )
                                )
                            ]
                        )
                    )
                    return
                decisions = [
                    {
                        "tool_call_id": tc["id"],
                        "action": "approve" if approve else "reject",
                        "reason": None if approve else (reason or "Rejected via A2A"),
                    }
                    for tc in pending
                ]
                resuming = True
                result = await service.run_resume(
                    decisions, user_id=user_id, session_id=session_id, principal=principal
                )
            else:
                result = await service.run_chat(
                    text, user_id=user_id, session_id=session_id, principal=principal
                )
        except GreenNodeRequestError as e:
            if e.status_code == 409:
                # 409 = "approval pending but the caller chatted" OR "nothing pending any more" (a concurrent
                # request already applied the decision) ⇒ ask the checkpoint which one instead of guessing.
                still_pending = await service.pending_tool_calls(
                    user_id=user_id, session_id=session_id
                )
                if still_pending:
                    await updater.requires_input(
                        updater.new_agent_message(
                            [
                                pb.Part(
                                    text=_ask_text(
                                        "An action is awaiting confirmation",
                                        user_id,
                                        session_id,
                                        still_pending,
                                    )
                                )
                            ]
                        )
                    )
                else:
                    gone = (
                        NOTHING_PENDING
                        if resuming
                        else "The conversation changed while this message was processed (concurrent "
                        "request) — send it again."
                    )
                    await updater.failed(updater.new_agent_message([pb.Part(text=gone)]))
                return
            await updater.failed(updater.new_agent_message([pb.Part(text=e.message)]))
            return

        if result["status"] == "interrupted":
            await updater.requires_input(
                updater.new_agent_message(
                    [
                        pb.Part(
                            text=_ask_text(
                                "Confirmation required before running",
                                user_id,
                                session_id,
                                result["interrupt"]["tool_calls"],
                            )
                        )
                    ]
                )
            )
            return
        await updater.add_artifact([pb.Part(text=result["response"])], name="response")
        await updater.complete()

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        await updater.cancel()


# --------------------------------------------------------------------------- card
def build_agent_card(settings: Settings) -> pb.AgentCard:
    base = settings.a2a_public_url.rstrip("/")
    if settings.auth_mode == "api_key":
        schemes = {
            "apiKey": pb.SecurityScheme(
                api_key_security_scheme=pb.APIKeySecurityScheme(
                    location="header",
                    name=settings.auth_api_key_header,
                    description="API key; send header X-GreenNode-AgentBase-User-Id for per-user memory",
                )
            )
        }
    else:
        schemes = {
            "bearer": pb.SecurityScheme(
                http_auth_security_scheme=pb.HTTPAuthSecurityScheme(
                    scheme="bearer",
                    bearer_format="JWT",
                    description=f"JWT issued by {settings.auth_issuer}",
                )
            )
        }
    return pb.AgentCard(
        name=settings.agent_name,
        description=settings.a2a_description,
        version=settings.agent_version,
        supported_interfaces=[
            pb.AgentInterface(url=f"{base}{RPC_PATH}", protocol_binding="JSONRPC")
        ],
        capabilities=pb.AgentCapabilities(streaming=False, push_notifications=False),
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
        security_schemes=schemes,
        # Declared schemes alone don't say they are REQUIRED — clients read security_requirements
        security_requirements=[
            pb.SecurityRequirement(schemes={name: pb.StringList() for name in schemes})
        ],
        skills=[
            pb.AgentSkill(
                id=s["id"],
                name=s["name"],
                description=s["description"],
                tags=s.get("tags", []),
                examples=s.get("examples", []),
            )
            for s in settings.a2a_skills
        ],
    )


def a2a_routes(settings: Settings) -> list[BaseRoute]:
    card = build_agent_card(settings)
    handler = DefaultRequestHandlerV2(
        agent_executor=AgentBaseExecutor(),
        task_store=BoundedTaskStore(settings.a2a_max_tasks, settings.a2a_task_ttl_s),
        agent_card=card,
    )
    # Sub-app holds only the JSON-RPC route at /a2a, wrapped in AuthenticationMiddleware. Mount("") is
    # placed AFTER /invocations, /health, agent card ⇒ those routes are unaffected.
    rpc_app = Starlette(
        routes=create_jsonrpc_routes(handler, rpc_url=RPC_PATH),
        middleware=[
            Middleware(
                AuthenticationMiddleware, backend=AgentBaseAuthBackend(), on_error=_auth_error
            )
        ],
    )
    return [*create_agent_card_routes(card), Mount("", app=rpc_app)]


def new_request_id() -> str:
    return uuid.uuid4().hex


__all__ = ["a2a_routes", "build_agent_card", "new_text_message"]
