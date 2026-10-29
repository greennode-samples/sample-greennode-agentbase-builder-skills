"""Connect external tools via MCP.

Connection standard (declared in mcp_servers.json; ${ENV_VAR} is expanded inside string values only,
an unset/empty variable makes that server a config error — skipped with an error log):
  - Prod: agent -> AgentBase MCP Gateway (MCP Connector) -> MCP server. The gateway handles inbound auth,
    outbound credentials (Identity providerName) and Policy Group. Create with /agentbase-gateway.
  - Local: may connect directly to an MCP server (streamable_http or stdio) via "envs": ["local"].

Auth type per server (`auth`) — REQUIRED for HTTP servers, there is no default (a default of "iam" would send the
agent's platform credential to any URL that forgot it):
  - "iam"      : the agent's IAM Bearer token (gateway inboundAuth.mode = IAM). Token auto-refreshes.
                 Only for the AgentBase MCP Gateway (*.agentbase-gateway.aiplatform.vngcloud.vn).
  - "user_jwt" : forwards the end-user's JWT (gateway inboundAuth.mode = JWT) => per-request.
                 Also meant for the gateway; other hosts log a warning (every user's JWT is sent there).
  - "none"     : no auth from the agent; a static key for a self-built MCP server goes in
                 "headers": {"Authorization": "Bearer ${MY_KEY}"} (only allowed with "none").
"iam" and "user_jwt" send a credential ⇒ the URL must be https:// (http only for localhost).

Optional per server: "prefix": false keeps the MCP tool names unprefixed (default: `<server>_<tool>`);
"policy_all_or_nothing": true when the Policy Group grants this connector as a whole (one deny ⇒ the whole
connector is short-circuited for the turn).
"""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import json
import logging
import os
import re
import threading
import time
from collections.abc import AsyncGenerator, Generator, Iterator
from contextvars import ContextVar
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from langchain_core.tools import BaseTool, ToolException
from langchain_mcp_adapters.client import MultiServerMCPClient
from langgraph.errors import GraphBubbleUp

from app.auth.inbound import Principal
from app.config import Settings
from app.observability import tracing

log = logging.getLogger(__name__)

IAM_TOKEN_URL = "https://iam.api.vngcloud.vn/accounts-api/v2/auth/token"


def iam_principal(token: str) -> str | None:
    """Principal that Policy Group uses for an IAM caller = `iam:<sub>` of the IAM token (the service
    account's user id, NOT the client_id). Only decodes the payload for logging; no signature check."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        sub = json.loads(base64.urlsafe_b64decode(payload)).get("sub")
        return f"iam:{sub}" if sub else None
    except (IndexError, ValueError):
        return None


# A gateway 401 with a token younger than this is not a stale token (wrong principal, inbound misconfig…)
# ⇒ no refetch: a persistent 401 must not turn every MCP request into an IAM token request.
_MIN_REFRESH_INTERVAL_S = 30.0


class IAMBearerAuth(httpx.Auth):
    """httpx.Auth that fetches an IAM token (client_credentials) from GREENNODE_CLIENT_ID/SECRET, cached until exp.

    - Single-flight: concurrent requests (several `auth: iam` servers loaded in parallel) share ONE token
      request (asyncio.Lock per event loop / threading.Lock for sync clients).
    - A 401 from the gateway (token revoked / expired early) ⇒ one retry with a fresh token.
    On AgentBase Runtime these variables are injected automatically; locally read from env/.greennode.json.
    `transport` is a test seam (httpx.MockTransport) for the token endpoint.
    """

    def __init__(self, transport: Any = None) -> None:
        from greennode_agentbase import IAMCredentials

        self._creds = IAMCredentials()
        self._transport = transport
        self._token: str | None = None
        self._exp = 0.0
        self._fetched_at = 0.0
        self.principal: str | None = None
        self._sync_lock = threading.Lock()
        self._async_lock: asyncio.Lock | None = None
        self._async_lock_loop: asyncio.AbstractEventLoop | None = None

    def has_credentials(self) -> bool:
        return self._creds.is_available()

    def _request_token(self) -> httpx.Request:
        self._creds.require()
        return httpx.Request(
            "POST",
            IAM_TOKEN_URL,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={"grant_type": "client_credentials"},
        )

    def _store(self, resp: httpx.Response) -> None:
        resp.raise_for_status()
        data = resp.json()
        self._token = data["access_token"]
        self._fetched_at = time.time()
        self._exp = self._fetched_at + int(data.get("expires_in", 1800)) * 0.9
        if self.principal is None:
            self.principal = iam_principal(self._token)
            log.info("IAM principal of the agent (use in Policy Group): %s", self.principal)

    def _valid(self) -> bool:
        return bool(self._token) and time.time() < self._exp

    def _usable(self, stale: str | None) -> bool:
        """Cached token still good — `stale` = a token the gateway just rejected (401)."""
        if not self._valid():
            return False
        return self._token != stale or time.time() - self._fetched_at < _MIN_REFRESH_INTERVAL_S

    def _basic(self) -> httpx.BasicAuth:
        return httpx.BasicAuth(self._creds.client_id or "", self._creds.client_secret or "")

    def _fetch_sync(self) -> None:
        with httpx.Client(auth=self._basic(), timeout=30, transport=self._transport) as c:
            self._store(c.send(self._request_token()))

    def refresh(self) -> None:
        """Fetch a new token now (sync). Raises httpx.HTTPStatusError if IAM rejects the credentials."""
        with self._sync_lock:
            self._fetch_sync()

    def token(self, stale: str | None = None) -> str:
        """Current token (sync), fetched if expired or equal to `stale`."""
        with self._sync_lock:
            if not self._usable(stale):
                self._fetch_sync()
            return self._token or ""

    def _lock(self) -> asyncio.Lock:
        # an asyncio.Lock is bound to one event loop (tests run one loop per test)
        loop = asyncio.get_running_loop()
        if self._async_lock is None or self._async_lock_loop is not loop:
            self._async_lock, self._async_lock_loop = asyncio.Lock(), loop
        return self._async_lock

    async def atoken(self, stale: str | None = None) -> str:
        """Current token, fetched if expired or equal to `stale`. Concurrent callers share one fetch."""
        if self._usable(stale):
            return self._token or ""
        async with self._lock():
            if not self._usable(stale):  # another task may have fetched it while we waited
                async with httpx.AsyncClient(
                    auth=self._basic(), timeout=30, transport=self._transport
                ) as c:
                    self._store(await c.send(self._request_token()))
        return self._token or ""

    def sync_auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response]:
        token = self.token()
        request.headers["Authorization"] = f"Bearer {token}"
        response = yield request
        if response.status_code == 401 and (fresh := self.token(stale=token)) != token:
            request.headers["Authorization"] = f"Bearer {fresh}"
            yield request

    async def async_auth_flow(
        self, request: httpx.Request
    ) -> AsyncGenerator[httpx.Request, httpx.Response]:
        token = await self.atoken()
        request.headers["Authorization"] = f"Bearer {token}"
        response = yield request
        # Gateway 401 = token revoked / expired early ⇒ retry ONCE with a fresh token (never a loop)
        if response.status_code == 401 and (fresh := await self.atoken(stale=token)) != token:
            request.headers["Authorization"] = f"Bearer {fresh}"
            yield request


_iam_auth: IAMBearerAuth | None = None


def _get_iam_auth() -> IAMBearerAuth:
    global _iam_auth
    if _iam_auth is None:
        _iam_auth = IAMBearerAuth()
    return _iam_auth


_AUTH_MODES = ("iam", "user_jwt", "none")
_GATEWAY_HOST_SUFFIX = ".agentbase-gateway.aiplatform.vngcloud.vn"


def _is_loopback(host: str | None) -> bool:
    if not host:
        return False
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


# What `inject_args` can fill: the VERIFIED caller (JWT / API-key principal), never a value the model wrote
INJECTABLE = ("user_id", "tenant_id")


def mcp_config_error(cfg: dict[str, Any]) -> str | None:
    """Why this server entry must not be loaded (None = OK). Fails closed: never guess an auth mode."""
    for flag in ("prefix", "policy_all_or_nothing"):
        if flag in cfg and not isinstance(cfg[flag], bool):
            return f'"{flag}" must be true or false (got {cfg[flag]!r})'
    inject = cfg.get("inject_args")
    if inject is not None and (
        not isinstance(inject, dict)
        or not all(isinstance(k, str) and v in INJECTABLE for k, v in inject.items())
    ):
        return (
            f'"inject_args" must map argument names to one of {list(INJECTABLE)} (got {inject!r})'
        )
    if cfg.get("transport") == "stdio":
        return None  # local process, no HTTP auth
    auth = cfg.get("auth")
    if auth not in _AUTH_MODES:
        return f'"auth" is required, one of {list(_AUTH_MODES)} (got {auth!r})'
    if auth != "none" and any(k.lower() == "authorization" for k in cfg.get("headers") or {}):
        return f'headers.Authorization conflicts with auth="{auth}" (it would be overwritten); use auth="none"'
    url = urlsplit(str(cfg.get("url") or ""))
    if auth != "none" and url.scheme != "https" and not _is_loopback(url.hostname):
        # the IAM token / user JWT would cross the network in clear text
        return f'auth="{auth}" sends a credential ⇒ "url" must be https:// (http: localhost only)'
    return None


_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand_env(value: Any, missing: set[str]) -> Any:
    """Expand `${VAR}` inside string leaves of the ALREADY-PARSED JSON (never over the raw text: a `"` in a
    value would break the whole file and a crafted value could inject keys such as "auth"). Substituted
    values are not re-expanded. Unset or empty variables are collected in `missing` (caller skips the server
    instead of sending a literal `${VAR}` or an empty credential)."""
    if isinstance(value, str):

        def sub(m: re.Match[str]) -> str:
            if not (v := os.environ.get(m.group(1))):
                missing.add(m.group(1))
                return m.group(0)
            return v

        return _ENV_REF.sub(sub, value)
    if isinstance(value, dict):
        return {k: _expand_env(v, missing) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v, missing) for v in value]
    return value


# What each auth mode sends — meant for the AgentBase MCP Gateway only; any other host logs this warning.
_NON_GATEWAY_WARNING = {
    "iam": "the agent's IAM token (platform credential) is sent there. Use auth=none (+ headers) for "
    "other servers",
    "user_jwt": "every end user's JWT is forwarded there. Keep it only for your own MCP server that "
    "validates the same IdP; otherwise use auth=none (+ headers)",
}


# Parsed mcp_servers.json per path: re-parsed only when the file (mtime/size), APP_ENV or the value of a
# referenced ${VAR} changes ⇒ config warnings/errors are logged once, not on every request.
_config_cache: dict[str, tuple[tuple[Any, ...], list[str], dict[str, dict[str, Any]]]] = {}


def load_mcp_config(settings: Settings) -> dict[str, dict[str, Any]]:
    path = Path(settings.mcp_config_file)
    try:
        st = path.stat()
    except FileNotFoundError:
        return {}
    stamp = (st.st_mtime_ns, st.st_size, settings.app_env)
    cached = _config_cache.get(str(path))
    if cached is not None:
        key, refs, servers = cached
        if key == (*stamp, *(os.environ.get(v) for v in refs)):
            return dict(servers)
    raw_text = path.read_text(encoding="utf-8")
    refs = sorted(set(_ENV_REF.findall(raw_text)))
    servers = _parse_mcp_config(raw_text, settings)
    _config_cache[str(path)] = ((*stamp, *(os.environ.get(v) for v in refs)), refs, servers)
    return dict(servers)


def _parse_mcp_config(raw_text: str, settings: Settings) -> dict[str, dict[str, Any]]:
    raw = json.loads(raw_text)
    servers = {}
    for name, entry in (raw.get("servers") or {}).items():
        envs = entry.get("envs")
        if envs and settings.app_env not in envs:
            continue
        if entry.get("enabled", True) is False:
            continue
        missing: set[str] = set()
        cfg = _expand_env(entry, missing)
        if missing:
            log.error(
                "MCP server %r skipped — environment variable(s) %s unset or empty (referenced as ${VAR} "
                "in mcp_servers.json)",
                name,
                ", ".join(sorted(missing)),
            )
            continue
        if err := mcp_config_error(cfg):
            log.error("MCP server %r skipped — invalid mcp_servers.json entry: %s", name, err)
            continue
        host = urlsplit(cfg.get("url", "")).hostname or ""
        warning = _NON_GATEWAY_WARNING.get(cfg.get("auth"))
        if warning and not host.endswith(_GATEWAY_HOST_SUFFIX):
            log.warning(
                "MCP server %r uses auth=%s but %r is not an AgentBase MCP Gateway — %s.",
                name,
                cfg["auth"],
                host,
                warning,
            )
        servers[name] = cfg
    return servers


async def log_iam_principal(settings: Settings, timeout_s: float = 10.0) -> None:
    """Startup (main.py lifespan): log the agent's Policy Group principal (`IAM principal … iam:<sub>`)
    before any request, when an enabled `auth: iam` server is configured and IAM credentials exist.
    Fetches the IAM token once (it is then reused by the MCP calls). Never fails startup —
    `make check-creds` prints the same principal."""
    try:
        if not any(c.get("auth") == "iam" for c in load_mcp_config(settings).values()):
            return
        auth = _get_iam_auth()
        if not auth.has_credentials():
            log.warning(
                "MCP servers use auth=iam but no IAM credentials are configured (GREENNODE_CLIENT_ID/"
                "SECRET or .greennode.json) — those servers will fail; run `make check-creds`"
            )
            return
        await asyncio.wait_for(auth.atoken(), timeout=timeout_s)
    except Exception as e:  # noqa: BLE001 — informational only, the agent must still start
        log.warning(
            "IAM principal not logged at startup (%s) — it is logged on the first MCP call; "
            "`make check-creds` shows it too",
            _error_summary(e),
        )


def _connection(cfg: dict[str, Any], principal: Principal, settings: Settings) -> dict[str, Any]:
    transport = cfg.get("transport", "streamable_http")
    if transport == "stdio":
        return {
            "transport": "stdio",
            "command": cfg["command"],
            "args": cfg.get("args", []),
            "env": cfg.get("env"),
        }
    conn: dict[str, Any] = {
        "transport": transport,
        "url": cfg["url"],
        "headers": dict(cfg.get("headers") or {}),
        "timeout": settings.mcp_tool_timeout_s,
        # The adapter default is very long ⇒ a "silent" MCP stream holds the request for minutes
        "sse_read_timeout": settings.mcp_tool_timeout_s,
    }
    auth = cfg.get("auth", "none")
    if auth == "iam":
        conn["auth"] = _get_iam_auth()
    elif auth == "user_jwt":
        if not principal.token:
            raise PermissionError("MCP server requires a user JWT but the request has no token")
        conn["headers"]["Authorization"] = f"Bearer {principal.token}"
    return conn


# Policy denials in the current request: server -> denied tool names. A deny blocks THAT tool for the turn;
# the whole connector is short-circuited once CONNECTOR_DENY_THRESHOLD distinct tools were denied (seen in
# practice: a reasoning model tried 8 blocked tools in turn ⇒ 84s) or at the first deny when the server is
# "policy_all_or_nothing". Recreated per request (reset_policy_denials).
_DENIALS: ContextVar[dict[str, set[str]] | None] = ContextVar("mcp_policy_denials", default=None)
CONNECTOR_DENY_THRESHOLD = 2


def reset_policy_denials() -> None:
    """Call at the start of each request (service) — shared by all LangGraph nodes/tasks of the request."""
    _DENIALS.set({})


POLICY_DENIED_MARKERS = ("denied by policy", "no policy allows this request")


def _leaf_errors(e: BaseException) -> Iterator[BaseException]:
    """The real errors behind (nested) ExceptionGroups — the MCP client raises HTTP errors from inside
    anyio TaskGroups, so `str(e)` is only "unhandled errors in a TaskGroup (1 sub-exception)"."""
    if isinstance(e, BaseExceptionGroup):
        for sub in e.exceptions:
            yield from _leaf_errors(sub)
    else:
        yield e


def _http_status(e: BaseException) -> int | None:
    return getattr(getattr(e, "response", None), "status_code", None)


def _insufficient_scope(e: BaseException) -> str | None:
    """`WWW-Authenticate: Bearer error="insufficient_scope"` on a 403 = the MCP SERVER rejected the token's
    scopes (RFC 6750) — a connector/IdP configuration problem, not a Policy Group deny. Returns the header."""
    for x in (e, *_leaf_errors(e)):
        if _http_status(x) == 403:
            www = x.response.headers.get("www-authenticate", "")  # type: ignore[attr-defined]
            if "insufficient_scope" in www:
                return www
    return None


def _is_policy_denied(e: BaseException) -> bool:
    """Gateway deny = HTTP 403 on tools/call (the body "No policy allows this request" never reaches the
    exception) or an MCP error whose text carries a deny marker."""
    for x in (e, *_leaf_errors(e)):
        if _http_status(x) == 403 or any(m in str(x).lower() for m in POLICY_DENIED_MARKERS):
            return True
    return False


_URL = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s'\"<>]+")


def _llm_error_summary(e: BaseException) -> str:
    """What the LLM may see of a transport/HTTP failure: status or error type + message WITHOUT any URL
    (query strings carry API keys: `?tavilyApiKey=…`; hosts are internal topology)."""
    leaf = next(_leaf_errors(e), e)
    if status := _http_status(leaf):
        return f"HTTP {status}"
    if isinstance(leaf, TimeoutError | httpx.TimeoutException):
        return "timeout"
    text = _URL.sub("<url>", (str(leaf).strip().splitlines() or [""])[0])[:200]
    return f"{type(leaf).__name__}: {text}" if text else type(leaf).__name__


def _guard_policy(tool: BaseTool, server: str, *, all_or_nothing: bool = False) -> BaseTool:
    """Turn MCP failures into short, actionable tool results (+ WARNING trace events on Langfuse):

    - Policy deny — the gateway answers HTTP 403 ("No policy allows this request"; the adapter raises
      ExceptionGroup[httpx.HTTPStatusError]) or MCP error "Request denied by policy." (isError ⇒
      ToolException) ⇒ `POLICY_DENIED` (do not retry), event `mcp.policy_denied`. Otherwise the LLM thinks
      it is transient and retries many times.
    - 403 with WWW-Authenticate error="insufficient_scope" (the MCP server, not the Policy Group) ⇒
      `MCP_SCOPE_ERROR`, event `mcp.scope_error`.
    - Any other transport/HTTP failure ⇒ `MCP_ERROR (<server>): <summary>` without URL/query (ToolNode
      would otherwise print the raw exception — incl. `url '…?apiKey=SECRET'` — into the LLM context,
      checkpoint and traces), event `mcp.tool_error`.
    - An MCP tool's own error result (isError, not a deny) passes through unchanged: it is the server's
      message for the LLM.
    """
    if getattr(tool, "_policy_guarded", False) or tool.coroutine is None:
        return tool
    original = tool.coroutine

    def _result(msg: str) -> Any:
        return (msg, None) if tool.response_format == "content_and_artifact" else msg

    def _blocked() -> str | None:
        denied = (_DENIALS.get() or {}).get(server)
        if not denied:
            return None
        if all_or_nothing or len(denied) >= CONNECTOR_DENY_THRESHOLD:
            return "connector"
        return "tool" if tool.name in denied else None

    def _denied(scope: str | None = None) -> Any:
        short_circuit = scope is not None
        if scope is None:  # a real deny from the gateway: remember it for this turn
            denials = _DENIALS.get()
            if denials is not None:
                denials.setdefault(server, set()).add(tool.name)
            scope = _blocked() or "tool"
        tracing.event(
            "mcp.policy_denied",
            level="WARNING",
            metadata={
                "server": server,
                "tool": tool.name,
                "scope": scope,
                "short_circuit": short_circuit,
            },
        )
        if scope == "connector":
            msg = (
                f"POLICY_DENIED: the agent is not allowed to use tools of connector '{server}' (blocked by "
                "the MCP Gateway Policy Group). Do NOT call any tool of this connector again this turn; "
                "answer with the information available and tell the user permission is missing."
            )
        else:
            msg = (
                f"POLICY_DENIED: the agent is not allowed to use tool '{tool.name}' (blocked by the MCP "
                "Gateway Policy Group). Do NOT call this tool again this turn; other tools may still be "
                "allowed. Answer with the information available and tell the user permission is missing."
            )
        return _result(msg)

    def _scope_error(www: str) -> Any:
        tracing.event(
            "mcp.scope_error",
            level="WARNING",
            metadata={"server": server, "tool": tool.name, "www_authenticate": www[:300]},
        )
        log.warning(
            "MCP server %r refused %s: insufficient_scope (%s)", server, tool.name, www[:300]
        )
        return _result(
            f"MCP_SCOPE_ERROR ({server}): the MCP server refused tool '{tool.name}' — the connector's "
            "credential lacks a required OAuth scope (configuration problem, not a Policy Group deny, "
            "not transient). Do NOT retry; tell the user this action is not available right now."
        )

    def _upstream_error(e: BaseException) -> Any:
        summary = _llm_error_summary(e)
        tracing.event(
            "mcp.tool_error",
            level="WARNING",
            metadata={"server": server, "tool": tool.name, "error": summary},
        )
        log.warning("MCP tool %s (server %r) failed: %s", tool.name, server, _error_summary(e))
        return _result(
            f"MCP_ERROR ({server}): tool '{tool.name}' failed — {summary}. Upstream failure, not your "
            "arguments: retry at most once, then answer with what you have and tell the user the tool "
            "is temporarily unavailable."
        )

    async def guarded(*args: Any, **kwargs: Any) -> Any:
        if scope := _blocked():
            return _denied(scope)  # no redundant request to the gateway
        try:
            result = await original(*args, **kwargs)
        except ToolException as e:
            # MCP isError result (adapter's _MCPToolExecutionError) or an interceptor's ToolException
            if _is_policy_denied(e):
                return _denied()
            raise  # the adapter's handler turns the server's isError content into the ToolMessage
        except GraphBubbleUp:  # interrupt()/Command from an interceptor: LangGraph control flow
            raise
        except Exception as e:  # noqa: BLE001 — ExceptionGroup[HTTPStatusError], timeouts, connection errors
            if (www := _insufficient_scope(e)) is not None:
                return _scope_error(www)
            if _is_policy_denied(e):
                return _denied()
            return _upstream_error(e)
        # Do NOT scan successful result content: a web page/document containing "denied by policy" would
        # lock the whole connector (prompt-injection attack). The gateway returns denies via the error path (ToolException / HTTP 403).
        return result

    tool.coroutine = guarded
    object.__setattr__(tool, "_policy_guarded", True)
    return tool


# Cache the tool list for user-independent servers (iam/none) to avoid tools/list on every request.
_static_tools: dict[str, tuple[float, list[BaseTool]]] = {}
_STATIC_TTL_S = 300


_failed: dict[
    str, tuple[float, str]
] = {}  # negative cache: failed server ⇒ skipped for MCP_FAILURE_TTL_S


def _is_auth_error(e: BaseException) -> bool:
    """401/403 status or PermissionError only — never digits in the message: URLs such as
    `gw-search-54037…` contain "403" and made every failure look like an auth error (never cached)."""
    leaves = (e, *_leaf_errors(e))
    return any(isinstance(x, PermissionError) or _http_status(x) in (401, 403) for x in leaves)


# Query-string values and URL userinfo are often credentials (`?tavilyApiKey=…`, `https://u:p@host`).
_QUERY_VALUE = re.compile(r"([?&][^=&#\s'\"]+=)[^&#\s'\"]+")
_USERINFO = re.compile(r"(//)[^/@\s'\"]+@")


def _safe_url(url: str | None) -> str | None:
    """scheme://host[:port]/path — no query string, fragment or userinfo (traces must not carry secrets)."""
    if not url:
        return url
    p = urlsplit(url)
    return f"{p.scheme}://{p.netloc.rpartition('@')[2]}{p.path}"


def _error_summary(e: BaseException) -> str:
    """`Type: message` of the real (leaf) error, first line, query values/userinfo masked, ≤ 300 chars."""
    leaf = next(_leaf_errors(e), e)
    text = (str(leaf).strip().splitlines() or [""])[0]
    text = _USERINFO.sub(r"\1***@", _QUERY_VALUE.sub(r"\1***", text))
    return f"{type(leaf).__name__}: {text[:300]}"


async def _load_server(
    name: str, cfg: dict, settings: Settings, principal: Principal, errors: dict[str, str] | None
) -> list[BaseTool]:
    auth = cfg.get("auth", "none")
    per_user = auth == "user_jwt"
    cached = _static_tools.get(name)
    hit = not per_user and cached is not None and time.time() - cached[0] < _STATIC_TTL_S
    failed = None if per_user else _failed.get(name)
    skip = failed is not None and time.time() - failed[0] < settings.mcp_failure_ttl_s
    span_input = {
        "server": name,
        "transport": cfg.get("transport", "streamable_http"),
        "url": _safe_url(cfg.get("url")),
        "auth": auth,
        "cached": hit,
        "skipped_recent_failure": skip,
    }
    with tracing.step("mcp.list_tools", input=span_input) as st:
        if hit:
            server_tools = cached[1]
        elif skip:
            # Seen in practice: a failing connector made every request wait ~15s ⇒ no retry within the TTL
            if errors is not None:
                errors[name] = f"skipped (failed {int(time.time() - failed[0])}s ago): {failed[1]}"
            st.set(level="WARNING", status_message=f"skipped: {failed[1]}")
            return []
        else:
            # "prefix": false keeps the MCP tool names (a name collision ⇒ collect_tools error)
            prefix = cfg.get("prefix", True)
            try:
                client = MultiServerMCPClient(
                    {name: _connection(cfg, principal, settings)}, tool_name_prefix=prefix
                )
                server_tools = await asyncio.wait_for(
                    client.get_tools(server_name=name), timeout=settings.mcp_list_timeout_s
                )
            except Exception as e:  # noqa: BLE001 — one failing MCP server must not crash the agent
                msg = _error_summary(e)
                log.warning("MCP server '%s' unavailable, skipping: %s", name, msg)
                # Negative cache ONLY for shared servers + infra errors. A per-user server (user_jwt) or
                # one user's permission/401 error must not make the server disappear for every user.
                if not per_user and not _is_auth_error(e):
                    _failed[name] = (time.time(), msg)
                if errors is not None:
                    errors[name] = msg
                st.set(level="WARNING", status_message=msg)
                return []
            _failed.pop(name, None)
            all_or_nothing = cfg.get("policy_all_or_nothing", False)
            server_tools = [
                _guard_policy(t, name, all_or_nothing=all_or_nothing) for t in server_tools
            ]
            if allow := cfg.get("allow_tools"):  # original MCP tool names
                strip = f"{name}_" if prefix else ""
                server_tools = [t for t in server_tools if t.name.removeprefix(strip) in allow]
            if not per_user:
                _static_tools[name] = (time.time(), server_tools)
        st.set(output=[t.name for t in server_tools])
        return server_tools


def _inject_args(tool: BaseTool, mapping: dict[str, str], principal: Principal) -> BaseTool:
    """Copy of `tool` whose arguments in `mapping` ({"owner_id": "user_id", "org": "tenant_id"}) are set from the
    VERIFIED principal on every call and removed from the schema the model sees.

    An MCP server behind the Gateway (auth `iam`) is called with the AGENT's credential: it cannot tell which end
    user or tenant a call is for, so it scopes by an argument — and an argument the model writes can be steered by
    prompt injection (`owner_id` of another user). Here the model neither sees nor sets it: a value it sends anyway
    is overwritten, and a caller without the value (no tenant claim) fails the call instead of sending nothing.
    The cached tool list is shared by every user, so the copy is made per request."""
    original = tool.coroutine
    if original is None:
        return tool
    values = {"user_id": principal.user_id, "tenant_id": principal.tenant_id}
    schema = tool.args_schema
    if isinstance(schema, dict):  # MCP tools carry the server's JSON Schema
        schema = {
            **schema,
            "properties": {
                k: v for k, v in (schema.get("properties") or {}).items() if k not in mapping
            },
        }
        if "required" in schema:
            schema["required"] = [k for k in schema["required"] if k not in mapping]

    async def injected(*args: Any, **kwargs: Any) -> Any:
        for param, source in mapping.items():
            if not (value := values[source]):
                msg = f"MCP_ERROR: '{tool.name}' needs the caller's {source}, which this request does not carry."
                return (msg, None) if tool.response_format == "content_and_artifact" else msg
            kwargs[param] = value  # always overwritten: whatever the model sent is ignored
        return await original(*args, **kwargs)

    return tool.model_copy(update={"coroutine": injected, "args_schema": schema})


async def get_mcp_tools(
    settings: Settings, principal: Principal, errors: dict[str, str] | None = None
) -> list[BaseTool]:
    """Load tools from ALL servers IN PARALLEL (one slow server does not add up latency)."""
    servers = load_mcp_config(settings)
    results = await asyncio.gather(
        *(_load_server(n, c, settings, principal, errors) for n, c in servers.items())
    )
    tools: list[BaseTool] = []
    for cfg, group in zip(servers.values(), results, strict=True):
        if inject := cfg.get("inject_args"):
            group = [_inject_args(t, inject, principal) for t in group]
        tools.extend(group)
    return tools
