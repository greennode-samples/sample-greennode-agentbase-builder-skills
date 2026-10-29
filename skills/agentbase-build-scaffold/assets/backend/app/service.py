"""Orchestrates one request: auth -> tools -> graph -> invoke/stream, wrapped in a Langfuse trace.

Payload contract (POST /invocations) — keep stable, the frontend & eval depend on it:

  {"type": "chat",     "message": "...", "stream": false}
  {"type": "resume",   "interrupt_id": "<interrupt.id>",                   # HITL — always echo it
                       "decisions": [{"tool_call_id","action":"approve|edit|reject","args"?,"reason"?}],
                       "stream": false}
  {"type": "feedback", "trace_id": "...", "feedback_token": "...", "score": 1, "comment": "..."}
                       # score: 1 | 0 | -1; feedback_token comes from the response — binds the trace to the user

Headers:
  Authorization: Bearer <user JWT>                 (or AUTH_TOKEN_HEADER)
  X-GreenNode-AgentBase-Session-Id: <uuid>         (required for chat/resume)
  X-GreenNode-AgentBase-User-Id: <sub>             (optional; if present must match the token)

Response non-stream:
  {"status": "success",     "response", "tools_used", "session_id", "trace_id", "feedback_token"}
  `session_id` may differ from the one sent: a session past SESSION_MAX_TURNS is rotated (summary + recent messages
  carried over) — the client MUST use the returned id from then on (also in `interrupt` / `done` events).
  {"status": "interrupted", "interrupt": {"id","type","tool_calls","message"}, "session_id", "trace_id",
   "feedback_token"}
  A resume may be answered with ANOTHER `interrupted` (new interrupt id) — handle `interrupt` until `success`.
Errors: HTTP status + {"error", "error_type", "details"}. 409 while an approval is pending (chat, or a resume
  whose decisions/interrupt_id belong to an earlier card): details.interrupt = the pending approval ⇒ show it.
  409 also when another request of the session is still running past REQUEST_TIMEOUT_S. 429 = rate limit
  (details.retry_after_s), 413 = body too large, 503 = overloaded (Retry-After) — app/limits.py.
Response stream (SSE, one `data: {...}` per line):
  token | tool_start | tool_end | reset {reason: self_eval_retry | llm_fallback} (clear the shown text) |
  status {phase: thinking | tool | checking, label, tools?} (progress line until the next token; ignore unknown
  phases/events) | interrupt {…same as interrupted} | done {…same as success} | error {message, status, interrupt?}
  The turn is over at done/interrupt/error: work after it (background self-eval) never needs the connection.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import logging
import re
import time
import uuid
import weakref
from collections.abc import AsyncIterator
from typing import Any

import openai
from greennode_agentbase import GreenNodeAgentBaseContext, RequestContext
from greennode_agentbase.exceptions import GreenNodeRequestError
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage
from langgraph.errors import GraphRecursionError
from langgraph.types import Command

from app import health
from app.auth.inbound import (
    Principal,
    authenticate_async,
    validate_session_id,
    validate_user_id,
)
from app.config import get_settings
from app.graph.builder import build_graph, recursion_limit
from app.hitl import (
    ALREADY_HANDLED,
    describe_decisions,
    describe_interrupt,
    interrupt_payload,
    is_placeholder_tool_message,
    is_rejected_tool_message,
    outcome_unknown_messages,
    pending_approval,
    stale_decision_ids,
    unfinished_tool_calls,
    validate_decisions,
)
from app.limits import rate_limit_wait
from app.llm import TIERS, fallbacks_for, model_for
from app.memory.short_term import get_checkpointer, thread_config
from app.observability import tracing
from app.observability.tracing import REASONING_KEY
from app.reflection import judge_after_response
from app.tools import collect_tools
from app.tools.mcp import reset_policy_denials

log = logging.getLogger(__name__)
MAX_MESSAGE_CHARS = 16_000
MAX_TRACE_ID_CHARS = 128
MAX_COMMENT_CHARS = 2000  # a user's free-text feedback; it is stored and forwarded to Langfuse


class _ClientError(GreenNodeRequestError):
    """An error written FOR the caller (validation, HITL state, mapped infra errors) — the only
    GreenNodeRequestError passed through as is. The SDK raises GreenNodeRequestError too (Memory / Identity:
    "HTTP 403 … principal iam:sa-… not allowed memory:GetEvents on mem-…"): never forwarded verbatim."""


# `error_type` in the SDK's JSON error body = the class name: keep the documented "GreenNodeRequestError"
_ClientError.__name__ = GreenNodeRequestError.__name__


def _error(msg: str, status: int = 400, details: dict | None = None) -> GreenNodeRequestError:
    return _ClientError(msg, status_code=status, details=details)


async def handle(payload: dict[str, Any], context: RequestContext) -> Any:
    if not isinstance(payload, dict):  # JSON array/string body ⇒ 400, not an AttributeError 500
        raise _error("Request body must be a JSON object")
    kind = payload.get("type", "chat")
    principal = await authenticate_async(context, get_settings())

    if kind == "feedback":
        return _handle_feedback(payload, principal)
    if kind not in ("chat", "resume"):
        raise _error(f"Unknown payload type: {kind}")
    if not context.session_id:
        raise _error("Header X-GreenNode-AgentBase-Session-Id is required")
    validate_session_id(context.session_id)  # prevents path traversal into another user's session
    # Per VERIFIED user / tenant (app/limits.py): one caller can't spend the account's shared LLM / Memory quota
    if wait := rate_limit_wait(principal.user_id, principal.tenant_id, get_settings()):
        raise _error(
            f"Too many requests — try again in {wait}s", 429, details={"retry_after_s": wait}
        )

    if kind == "chat":
        raw = payload.get("message")
        if raw is not None and not isinstance(raw, str):
            raise _error("`message` must be a string")
        message = (raw or "").strip()
        if not message:
            raise _error("`message` is required")
        if len(message) > MAX_MESSAGE_CHARS:
            raise _error(f"`message` exceeds {MAX_MESSAGE_CHARS} chars")
        graph_input: Any = {"messages": [HumanMessage(message)]}
        trace_input: Any = message
    else:
        try:
            decisions = validate_decisions(payload.get("decisions"))
        except ValueError as e:
            raise _error(str(e)) from e
        if not isinstance(payload.get("interrupt_id"), str | None):
            raise _error("`interrupt_id` must be a string")
        graph_input = Command(resume=decisions)
        # readable in the session view; tool names are added once the checkpoint is loaded (_prepare)
        trace_input = describe_decisions(decisions)

    run = _Run(
        principal,
        context.session_id,
        kind,
        graph_input,
        trace_input,
        interrupt_id=payload.get("interrupt_id"),
        rotate=True,
    )
    if payload.get("stream"):
        return run.stream()
    return await run.invoke()


def _principal_for(user_id: str, principal: Principal | None) -> Principal:
    """A2A passes the verified Principal (claims + token survive ⇒ RBAC / user_jwt keep working)."""
    if principal is None:
        return Principal(user_id=user_id)
    if principal.user_id != user_id:
        raise ValueError("principal.user_id must equal user_id")
    return principal


async def run_chat(
    message: str, *, user_id: str, session_id: str, principal: Principal | None = None
) -> dict:
    """Call the agent in-process, BYPASSING inbound auth — only for eval / tests / internal jobs / A2A.
    Callers that already authenticated (A2A) pass `principal` so claims and the token are kept."""
    validate_user_id(user_id)
    validate_session_id(session_id)
    if message is not None and not isinstance(message, str):
        raise _error("`message` must be a string")
    message = (message or "").strip()
    if not message:
        raise _error("`message` is required")
    if len(message) > MAX_MESSAGE_CHARS:
        raise _error(f"`message` exceeds {MAX_MESSAGE_CHARS} chars")
    run = _Run(
        _principal_for(user_id, principal),
        session_id,
        "chat",
        {"messages": [HumanMessage(message)]},
        message,
    )
    return await run.invoke()


async def run_resume(
    decisions: list[dict], *, user_id: str, session_id: str, principal: Principal | None = None
) -> dict:
    """Resume HITL in-process — only for eval / tests / A2A (pass `principal` when already authenticated)."""
    validate_user_id(user_id)
    validate_session_id(session_id)
    try:
        decisions = validate_decisions(decisions)
    except ValueError as e:
        raise _error(str(e)) from e
    run = _Run(
        _principal_for(user_id, principal),
        session_id,
        "resume",
        Command(resume=decisions),
        {"resume": decisions},
    )
    return await run.invoke()


def feedback_token(user_id: str, trace_id: str) -> str:
    """HMAC(user_id, trace_id): only the user who received the answer can score that trace."""
    msg = f"{user_id}\x00{trace_id}".encode()
    return hmac.new(get_settings().signing_key, msg, hashlib.sha256).hexdigest()


async def pending_tool_calls(*, user_id: str, session_id: str) -> list[dict]:
    """Tool calls pending approval (HITL) for exactly this (user, session) — used by A2A."""
    s = get_settings()
    graph = build_graph(settings=s, tools=[], checkpointer=get_checkpointer(s))
    snap = await graph.aget_state(thread_config(s, session_id=session_id, user_id=user_id))
    return (pending_approval(snap, s) or {}).get("tool_calls", [])


def _handle_feedback(payload: dict[str, Any], principal: Principal) -> dict:
    trace_id = payload.get("trace_id")
    score = payload.get("score")
    valid_trace = isinstance(trace_id, str) and 0 < len(trace_id) <= MAX_TRACE_ID_CHARS
    if not valid_trace or isinstance(score, bool) or score not in (-1, 0, 1):
        raise _error("feedback requires trace_id and score in {-1, 0, 1}")
    comment = payload.get("comment")
    if comment is not None and (not isinstance(comment, str) or len(comment) > MAX_COMMENT_CHARS):
        raise _error(f"`comment` must be a string of at most {MAX_COMMENT_CHARS} chars")
    token = payload.get("feedback_token")
    token = token if isinstance(token, str) else ""
    # bytes: compare_digest on str raises TypeError for non-ASCII input (a 500 instead of a 403)
    expected = feedback_token(principal.user_id, trace_id)
    if not hmac.compare_digest(token.encode(), expected.encode()):
        raise _error("feedback_token is not valid for this user", 403)
    # user_id ⇒ deterministic score id: re-tapping 👍/👎 updates the same score instead of adding duplicates
    ok = tracing.record_feedback(
        trace_id=trace_id,
        value=float(score),
        comment=comment,
        user_id=principal.user_id,
    )
    return {"status": "success" if ok else "ignored"}


def _trace_meta(kind: str) -> dict[str, Any]:
    s = get_settings()
    return {
        "request_id": GreenNodeAgentBaseContext.get_request_id(),
        "request_type": kind,
        **{f"llm_model_{t}": model_for(t) for t in TIERS},
        **{f"llm_fallbacks_{t}": ",".join(fallbacks_for(t)) for t in TIERS},
        "llm_adaptive_routing": s.llm_adaptive_routing,
        "memory_backend": s.memory_backend,
        "memory_id": s.memory_id,
        "auth_mode": s.auth_mode,
        "hitl_tools": ",".join(s.hitl_tools),
        "reflection_enabled": s.reflection_enabled,
    }


def _trace_auth(principal: Principal) -> None:
    claims = principal.claims
    tracing.event(
        "auth",
        output={
            "user_id": principal.user_id,
            "tenant_id": principal.tenant_id,
            "roles": sorted(principal.roles),
            "mode": get_settings().auth_mode,
            "iss": claims.get("iss"),
            "aud": claims.get("aud"),
            "exp": claims.get("exp"),
            "client": claims.get("azp") or claims.get("client_id"),
        },
    )


def _final_text(messages: list) -> str:
    last = messages[-1] if messages else None
    if isinstance(last, AIMessage) and not last.tool_calls:
        return last.text
    return ""


def _tools_used(messages: list) -> list[str]:
    """Names of tools that actually RAN in the current turn (after the last HumanMessage) — not the bridge's
    placeholders, not calls the user rejected in HITL (eval `expected_tools` / `--hitl reject` rely on this)."""
    used: list[str] = []
    for m in reversed(messages):
        if isinstance(m, HumanMessage):
            break
        if (
            isinstance(m, ToolMessage)
            and m.name
            and not is_placeholder_tool_message(m)
            and not is_rejected_tool_message(m)
        ):
            used.append(m.name)
    return list(reversed(used))


# A (user, session) runs only 1 request at a time within the process: blocks double-tap resume
# (approved tool running twice) and 2 parallel chats overwriting the checkpoint. Across replicas: see interrupt_id and
# hitl.idempotency_key.
_SESSION_LOCKS: weakref.WeakValueDictionary[tuple[str, str], asyncio.Lock] = (
    weakref.WeakValueDictionary()
)


def _session_lock(user_id: str, session_id: str) -> asyncio.Lock:
    key = (user_id, session_id)
    lock = _SESSION_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _SESSION_LOCKS[key] = lock
    return lock


async def _acquire(lock: asyncio.Lock, wait_s: float) -> bool:
    """Wait at most `wait_s` for the session lock (the wait used to be unbounded: retries of a slow turn queued up
    forever). Never leaves the lock held when giving up or when the caller is cancelled."""
    waiter = asyncio.ensure_future(lock.acquire())
    try:
        done, _ = await asyncio.wait({waiter}, timeout=wait_s)
    except BaseException:
        _abandon(waiter, lock)
        raise
    if done:
        return True
    _abandon(waiter, lock)
    return False


def _abandon(waiter: asyncio.Future, lock: asyncio.Lock) -> None:
    def release_if_acquired(w: asyncio.Future) -> None:
        if not w.cancelled() and w.exception() is None:
            lock.release()

    if waiter.done():
        release_if_acquired(waiter)
    else:
        waiter.cancel()
        waiter.add_done_callback(release_if_acquired)


_SESSION_BUSY = "Another request of this session is still running — try again when it ends"


def _user_error(e: BaseException) -> dict:
    """The error the caller receives (HTTP error / SSE `error` event) — the root trace output of a failed turn.
    `invoke` raises the already-mapped error (a _ClientError, kept as is by _map_error)."""
    mapped = _map_error(e)
    return {"error": mapped.message, "status": getattr(mapped, "status_code", 500)}


# Errors _map_error turns into a status for the caller (anything else ⇒ SDK 500 / SSE "failed")
_MAPPED_ERRORS = (GreenNodeRequestError, GraphRecursionError, openai.APIError, TimeoutError)


def _mapped(e: BaseException) -> GreenNodeRequestError:
    """_map_error + log what the caller won't see (the SDK's / LLM provider's own message) + what `GET /ready`
    reports (app.health): a rejected LLM key or a refused Memory call shows as `degraded`, not only in a user's 502."""
    mapped = _map_error(e)
    if isinstance(e, GreenNodeRequestError) and mapped is not e:
        log.warning("backing service error (HTTP %s): %s", e.status_code, e.message)
        health.record("backing_service", False, f"HTTP {getattr(e, 'status_code', '?')}")
    elif isinstance(e, openai.APIError):
        log.warning("LLM error %s: %s", getattr(e, "status_code", type(e).__name__), e.message)
        health.record("llm", False, f"HTTP {getattr(e, 'status_code', None) or type(e).__name__}")
    return mapped


def _map_error(e: BaseException) -> GreenNodeRequestError:
    """Infrastructure errors ⇒ meaningful HTTP codes instead of a generic 500. Only _ClientError reaches the
    caller as is: an SDK GreenNodeRequestError < 500 (Memory/Identity 400/401/403/404) would leak internals and
    a 401/403 that frontends read as "session expired" ⇒ generic 502."""
    if isinstance(e, _ClientError):
        return e
    if isinstance(e, GreenNodeRequestError):
        status = getattr(e, "status_code", 500) or 500
        if status in (429,) or status >= 500:  # Memory/Identity overloaded or failing
            return _error("Backing service temporarily overloaded, try again later", 503)
        return _error("Backing service rejected the request", 502)
    if isinstance(e, GraphRecursionError):
        return _error(
            "The request needs too many steps — please split up the question (MAX_TOOL_ROUNDS)", 422
        )
    if isinstance(e, openai.APIStatusError) and e.status_code in (401, 402, 403, 404):
        # Not an outage: key rejected / plan expired / model not enabled for the key / model id wrong. Generic text
        # (the provider body stays in the log) — retrying won't help, the deployment config must be fixed.
        return _error(
            f"LLM configuration error (HTTP {e.status_code}) — check the model ids and LLM_API_KEY "
            "(see make check-creds)",
            502,
        )
    if isinstance(e, openai.APIError):  # after all fallbacks have been tried
        return _error("LLM provider error or overloaded (fallbacks tried)", 502)
    if isinstance(e, TimeoutError):
        return _error(f"Agent timed out ({get_settings().request_timeout_s:.0f}s)", 504)
    return _error("Agent failed to process the request", 500)


class _Run:
    def __init__(
        self,
        principal: Principal,
        session_id: str,
        kind: str,
        graph_input,
        trace_input,
        interrupt_id: str | None = None,
        rotate: bool = False,
    ):
        self.interrupt_id = interrupt_id
        # SESSION_MAX_TURNS rotation: HTTP chats only (clients adopt the returned session_id; A2A / eval keep theirs)
        self.rotate = rotate
        self.root: Any = None  # root observation of this request (set by invoke/stream), None when tracing is off
        self.settings = get_settings()
        self.principal = principal
        self.session_id = session_id
        self.kind = kind
        self.graph_input = graph_input
        self.trace_input = trace_input
        # tool name → its metadata["status_label"] (SSE status)
        self.tool_labels: dict[str, str] = {}

    def _trace(self, tags: list[str]):
        return tracing.trace_request(
            settings=self.settings,
            user_id=self.principal.user_id,
            session_id=self.session_id,
            input=self.trace_input,
            metadata=_trace_meta(self.kind),
            tags=[
                f"type:{self.kind}",
                *tags,
                *([f"tenant:{t}"] if (t := self.principal.tenant_id) else []),
            ],
            error_output=_user_error,
        )

    async def _prepare(self):
        reset_policy_denials()
        # Identity SDK keys 3LO / delegated credentials by this context user id — set it from the VERIFIED
        # principal on every path (HTTP, A2A, run_chat for eval/jobs), never leave it unset or header-derived.
        GreenNodeAgentBaseContext.set_user_id(self.principal.user_id)
        _trace_auth(self.principal)
        tools = await collect_tools(self.settings, self.principal)
        self.tool_labels = {
            t.name: str(t.metadata["status_label"])
            for t in tools
            if isinstance(t.metadata, dict) and t.metadata.get("status_label")
        }
        graph = build_graph(
            settings=self.settings, tools=tools, checkpointer=get_checkpointer(self.settings)
        )
        claims = self.principal.claims or {}
        config = thread_config(
            self.settings,
            session_id=self.session_id,
            user_id=self.principal.user_id,
            # only allowlisted claims — tools read them via config["configurable"]["user_claims"]
            user_claims={k: claims[k] for k in self.settings.auth_forward_claims if k in claims},
        )
        # tenant / roles from the verified token (multi-tenant agents) — tools read them, never a tool parameter
        config["configurable"]["tenant_id"] = self.principal.tenant_id
        config["configurable"]["roles"] = sorted(self.principal.roles)
        config["callbacks"] = tracing.langchain_callbacks()
        config["recursion_limit"] = recursion_limit(self.settings)
        config["run_name"] = "agent.graph"

        # output only (session/kind in metadata): a debugging span, kept out of the session page's I/O view
        with tracing.step(
            "memory.checkpoint_load",
            metadata={
                "backend": self.settings.memory_backend,
                "session_id": self.session_id,
                "kind": self.kind,
            },
        ) as st:
            snapshot = await graph.aget_state(config)
            st.set(
                output={
                    "messages": len((snapshot.values or {}).get("messages", [])),
                    "next": list(snapshot.next or ()),
                    "interrupts": len(getattr(snapshot, "interrupts", None) or ()),
                }
            )
        if unfinished := unfinished_tool_calls(snapshot):
            # The previous request ended INSIDE the tools node: close those calls as "outcome unknown" (they may have
            # run) — never leave them dangling for the bridge's "interrupted" placeholder, which models retried.
            values = snapshot.values or {}
            rejected = values.get("hitl_rejected") or {}
            await graph.aupdate_state(
                config,
                {"messages": outcome_unknown_messages(unfinished, rejected), "hitl_rejected": {}},
                as_node="tools",
            )
            tracing.event(
                "hitl.outcome_unknown",
                level="WARNING",
                metadata={"tools": [tc["name"] for tc in unfinished if tc["id"] not in rejected]},
            )
            snapshot = await graph.aget_state(config)
        pending = pending_approval(snapshot, self.settings)
        if self.kind == "chat" and pending:
            # details.interrupt = the pending card (same object as the interrupted response) ⇒ client re-shows it
            raise _error(
                "Awaiting tool approval — send {type: resume} before chatting again",
                409,
                details={"interrupt": pending},
            )
        if self.kind == "resume":
            if not pending:
                raise _error("No approval request is pending in this session", 409)
            decisions = getattr(self.graph_input, "resume", None) or []
            if self.root is not None:
                self.root.update(
                    input=describe_decisions(decisions, pending["tool_calls"]),
                    metadata={"resume": decisions, "interrupt_id": self.interrupt_id},
                )
            visible = [i.id for i in (getattr(snapshot, "interrupts", None) or ())]
            # the bridge may not see the __interrupt__ write yet (visible empty) ⇒ the decision ids decide below
            if self.interrupt_id and visible and self.interrupt_id not in visible:
                raise _error(
                    f"{ALREADY_HANDLED} (interrupt_id is no longer valid)",
                    409,
                    details={"interrupt": pending},
                )
            # Works without interrupt_id (A2A, eval) and when the snapshot hides the interrupt: a double-tap /
            # retried resume of card 1, serialized behind the lock, must not be applied to card 2.
            if stale_decision_ids(decisions, pending["tool_calls"]):
                raise _error(
                    f"{ALREADY_HANDLED} (its tool calls are no longer pending)",
                    409,
                    details={"interrupt": pending},
                )
        max_turns = self.settings.session_max_turns
        turns = (snapshot.values or {}).get("turns", 0)
        if self.kind == "chat" and self.rotate and max_turns and turns >= max_turns:
            config = await self._rotate(graph, config, snapshot.values or {}, turns)
        return graph, config

    async def _rotate(self, graph, config: dict, values: dict, turns: int) -> dict:
        """Continue this conversation in a NEW stored session, seeded with the summary and the (already compressed)
        messages: the bridge reads every event of a session each turn, so reads would keep growing. The client gets
        the new id in `session_id`. The old session is no longer written (its events expire with eventExpiryDuration)."""
        new_id = str(uuid.uuid4())
        fresh = thread_config(self.settings, session_id=new_id, user_id=self.principal.user_id)
        config = {**config, "configurable": {**config["configurable"], **fresh["configurable"]}}
        seed = {
            "messages": values.get("messages", []),
            "summary": values.get("summary", ""),
            "turns": 0,
        }
        with tracing.step(
            "memory.session_rotated",
            metadata={"from": self.session_id, "to": new_id, "turns": turns},
        ) as st:
            await graph.aupdate_state(config, seed, as_node="__start__")
            st.set(output={"messages_carried": len(seed["messages"])})
        self.session_id = new_id
        return config

    def _fb(self, trace_id: str | None) -> str | None:
        return feedback_token(self.principal.user_id, trace_id) if trace_id else None

    async def _result(self, root, state: dict, interrupts: Any) -> dict:
        """Result taken DIRECTLY from the graph output (no checkpoint re-read — see hitl.py). Runs inside the request's
        trace: a background self-eval started here lands in the same trace, after the response went out."""
        messages = state.get("messages", [])
        trace_id = tracing.current_trace_id()
        health.record("llm", True)  # a whole turn ran: the model and the stores answered
        health.record("backing_service", True)
        if interrupt := interrupt_payload(interrupts):
            if root is not None:
                # readable in the session view; the raw payload stays in metadata
                root.update(output=describe_interrupt(interrupt), metadata={"interrupt": interrupt})
            tracing.score_trace("hitl_requested", 1)
            return {
                "status": "interrupted",
                "interrupt": interrupt,
                "session_id": self.session_id,
                "trace_id": trace_id,
                "feedback_token": self._fb(trace_id),
            }
        answer, tools_used = _final_text(messages), _tools_used(messages)
        if root is not None:
            # trace output = what a reviewer / evaluator / experiment compares: the answer text itself
            root.update(output=answer, metadata={"tools_used": tools_used})
        # REFLECTION_MODE background/auto: score the answer AFTER it is sent (no retry, nobody waits) — never raises
        judge_after_response(state, self.settings)
        return {
            "status": "success",
            "response": answer,
            "tools_used": tools_used,
            "session_id": self.session_id,
            "trace_id": trace_id,
            "feedback_token": self._fb(trace_id),
        }

    def _log_request(
        self, started: float, status: int, *, stream: bool, result: dict | None = None
    ) -> None:
        """One structured line per request (message "request"): status, duration, ids — never the message, the answer
        or a tool argument. The fields a log-based dashboard or alert needs; the full story is the Langfuse trace."""
        fields: dict[str, Any] = {
            "type": self.kind,
            "status": status,
            "duration_ms": round((time.monotonic() - started) * 1000),
            "stream": stream,
            "session_id": self.session_id,
            "user": hashlib.sha256(self.principal.user_id.encode()).hexdigest()[:12],
        }
        if self.principal.tenant_id:
            fields["tenant_id"] = self.principal.tenant_id
        if result:
            fields.update(
                outcome=result["status"],
                trace_id=result.get("trace_id"),
                tools=len(result.get("tools_used") or []),
            )
        log.info("request", extra={"fields": fields})

    async def invoke(self) -> dict:
        started, status, result = time.monotonic(), 500, None
        lock = _session_lock(self.principal.user_id, self.session_id)
        if not await _acquire(lock, self.settings.request_timeout_s):
            self._log_request(started, 409, stream=False)
            raise _error(_SESSION_BUSY, 409)
        try:
            with self._trace([]) as root:
                try:
                    async with asyncio.timeout(self.settings.request_timeout_s):
                        self.root = root
                        graph, config = await self._prepare()
                        out = await graph.ainvoke(self.graph_input, config)
                except _MAPPED_ERRORS as e:
                    mapped = _mapped(e)
                    if mapped is e:
                        raise
                    raise mapped from e
                result = await self._result(root, out, out.get("__interrupt__"))
                status = 200
                return result
        except BaseException as e:
            status = (
                499 if isinstance(e, asyncio.CancelledError) else getattr(e, "status_code", 500)
            )
            raise
        finally:
            lock.release()
            self._log_request(started, status, stream=False, result=result)

    async def stream(self) -> AsyncIterator[dict]:
        started, status, result = time.monotonic(), 500, None
        lock = _session_lock(self.principal.user_id, self.session_id)
        if not await _acquire(lock, self.settings.request_timeout_s):
            self._log_request(started, 409, stream=True)
            yield {"event": "error", "message": _SESSION_BUSY, "status": 409}
            return
        it = None
        try:
            with (
                self._trace(["stream"]) as root,
                _deadline(self.settings.request_timeout_s) as left,
            ):
                # progress line at once: the first LLM call may take seconds (a reasoning model: a minute)
                status = _Status()
                for event in status.show("thinking"):
                    yield event
                async with asyncio.timeout(max(left(), 0.001)):
                    self.root = root
                    graph, config = await self._prepare()
                status.labels = self.tool_labels
                interrupts, final = None, None
                # langgraph_step → (id of the agent message being streamed, text already sent?)
                streaming: dict[Any, tuple[str, bool]] = {}
                # "tasks": node starts ⇒ status (tools running, answer being checked)
                it = graph.astream(
                    self.graph_input, config, stream_mode=["messages", "updates", "values", "tasks"]
                ).__aiter__()
                while True:
                    # Enforce the limit even when the graph is "silent" (LLM/Memory/MCP hung, no events emitted)
                    try:
                        mode, data = await asyncio.wait_for(it.__anext__(), timeout=max(left(), 0))
                    except StopAsyncIteration:
                        break
                    if mode == "values":
                        final = data
                        continue
                    if mode == "updates" and isinstance(data, dict) and data.get("__interrupt__"):
                        interrupts = data["__interrupt__"]
                    for event in _stream_events(mode, data, streaming, status):
                        yield event
                result = await self._result(root, final or {}, interrupts)
                status = 200
                if result["status"] == "interrupted":
                    yield {"event": "interrupt", **result}
                else:
                    yield {"event": "done", **result}
        except _MAPPED_ERRORS as e:
            mapped = _mapped(e)
            status = mapped.status_code
            event = {"event": "error", "message": mapped.message, "status": mapped.status_code}
            if interrupt := (mapped.details or {}).get("interrupt"):
                event["interrupt"] = interrupt  # same as the non-stream 409's details.interrupt
            yield event
        except GeneratorExit:  # the client closed the stream (Stop, closed tab)
            status = 499
            raise
        except Exception:  # noqa: BLE001 — stream already open, must return the error as an event
            log.exception("stream failed")
            yield {
                "event": "error",
                "message": "Agent failed to process the request.",
                "status": 500,
            }
        finally:
            if it is not None:
                with contextlib.suppress(Exception):
                    await it.aclose()
            lock.release()
            self._log_request(started, status, stream=True, result=result)


@contextlib.contextmanager
def _deadline(seconds: float):
    """Countdown for the stream (an async generator can't be wrapped in asyncio.timeout)."""
    end = time.monotonic() + seconds
    yield lambda: end - time.monotonic()


# --------------------------------------------------------------------------- SSE progress status
# Short user-facing labels of the `status` event. Clients may show their own (localized) text per phase / tool name;
# `label` is the default. Never put tool arguments or results in a status (they may hold personal data / secrets).
STATUS_LABELS = {"thinking": "Thinking…", "checking": "Checking the answer…"}
# Built-in tools; any other tool: its `metadata["status_label"]` (set it on your @tool) or "Using <name>…"
TOOL_STATUS_LABELS = {
    "search_knowledge": "Searching the knowledge base…",
    "remember": "Saving to memory…",
    "recall_memory": "Looking through your saved notes…",
    "get_current_time": "Checking the time…",
}
# graph node starting ⇒ phase shown (not listed: thinking). `approval` shows nothing: an interrupt follows.
_NODE_PHASES = {"tools": "tool", "reflect": "checking", "approval": None}


_NAME_SEPARATORS = re.compile(r"[_\-\s]+")


def tool_status_label(name: str, labels: dict[str, str] | None = None) -> str:
    """User-facing label of a running tool: its metadata label, a built-in one, else derived from the NAME only."""
    if label := (labels or {}).get(name) or TOOL_STATUS_LABELS.get(name):
        return label
    if name.startswith("ask_"):  # A2A client tools (app/a2a/client.py)
        return f"Asking {_NAME_SEPARATORS.sub(' ', name.removeprefix('ask_')).strip()}…"
    return f"Using {_NAME_SEPARATORS.sub(' ', name).strip()}…"


class _Status:
    """Progress line of one stream: emits a `status` event only when what the client shows changes. The client
    clears it when answer text arrives (a `token`), so `shown` is reset then too."""

    def __init__(self, labels: dict[str, str] | None = None):
        self.shown: tuple | None = None
        self.labels = labels or {}

    def show(self, phase: str, tools: list[str] | None = None) -> list[dict]:
        if phase == "tool":
            names = list(dict.fromkeys(tools or []))
            if not names:
                return []
            label = " · ".join(dict.fromkeys(tool_status_label(n, self.labels) for n in names))
            event = {"event": "status", "phase": "tool", "label": label, "tools": names}
        else:
            event = {"event": "status", "phase": phase, "label": STATUS_LABELS.get(phase, phase)}
        key = (event["phase"], event["label"])
        if key == self.shown:
            return []
        self.shown = key
        return [event]

    def cleared(self) -> None:
        self.shown = None


def _pending_tool_names(state: Any) -> list[str]:
    """Tools the `tools` node is about to RUN: the last AI message's calls, minus those already answered and those
    the user rejected (HITL — `hitl_rejected`, answered by the tools node with an error message, nothing runs)."""
    if not isinstance(state, dict):
        return []
    messages = state.get("messages", [])
    answered: set[str] = set(state.get("hitl_rejected") or {})
    for m in reversed(messages):
        if isinstance(m, ToolMessage):
            answered.add(m.tool_call_id)
        elif isinstance(m, AIMessage):
            return [tc["name"] for tc in m.tool_calls if tc.get("id") not in answered]
    return []


def _stream_events(
    mode: str,
    data: Any,
    streaming: dict[Any, tuple[str, bool]] | None = None,
    status: _Status | None = None,
) -> list[dict]:
    """`streaming` (per request) remembers, per agent step, the message being streamed and whether text was already
    sent. A DIFFERENT message in the SAME step after text went out means the primary model failed mid-answer and a
    fallback restarted it (with text OR a tool call) ⇒ `reset` first, so the client never shows
    "partial primary answer + fallback answer" glued together.
    `status` (per request, None ⇒ no status events): `thinking` when a node starts (or a reasoning model streams its
    thinking after text was shown), `tool` when the tools node starts, `checking` when the reflect gate starts."""
    events: list[dict] = []
    if mode == "messages":
        chunk, meta = data
        if isinstance(chunk, AIMessageChunk) and meta.get("langgraph_node") == "agent":
            if streaming is not None and chunk.id:
                step = meta.get("langgraph_step")
                previous_id, sent_text = streaming.get(step, (None, False))
                if previous_id is not None and chunk.id != previous_id:
                    if sent_text:
                        events.append({"event": "reset", "reason": "llm_fallback"})
                        if status is not None:
                            status.cleared()
                    sent_text = False
                streaming[step] = (chunk.id, sent_text or bool(chunk.text))
            if chunk.text:
                events.append({"event": "token", "data": chunk.text})
                if status is not None:
                    status.cleared()  # the client replaces the status line with the answer text
            elif status is not None and status.shown is None:
                # thinking streamed after the line was cleared, e.g. the fallback model after a reset
                if chunk.response_metadata.get(REASONING_KEY):
                    events.extend(status.show("thinking"))
        return events
    if mode == "tasks":
        # task START payloads have "input" (results have "result"); custom nodes count as thinking
        if status is not None and isinstance(data, dict) and "input" in data:
            phase = _NODE_PHASES.get(data.get("name"), "thinking")
            if phase == "tool":
                events.extend(status.show("tool", _pending_tool_names(data["input"])))
            elif phase:
                events.extend(status.show(phase))
        return events
    for node, update in (data or {}).items():
        if not isinstance(update, dict):
            continue
        if node == "reflect" and update.get("critique"):
            events.append({"event": "reset", "reason": "self_eval_retry"})
            if status is not None:
                status.cleared()
        for m in update.get("messages", []):
            if node == "agent" and isinstance(m, AIMessage):
                events.extend({"event": "tool_start", "name": tc["name"]} for tc in m.tool_calls)
            elif node == "tools" and isinstance(m, ToolMessage):
                events.append({"event": "tool_end", "name": m.name, "status": m.status})
    return events
