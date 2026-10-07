"""A2A client — calls OTHER agents (A2A servers) as a LangChain tool.

Declared in a2a_agents.json (`${ENV}` is expanded inside string values only, after JSON parsing; an unset or
empty variable skips that agent with an error log):
  {"agents": {"stock": {"url": "https://<endpoint>", "description": "Stock agent...",
                        "auth": "api_key", "api_key": "${A2A_STOCK_API_KEY}"}}}
  auth: api_key  — sends header X-GreenNode-AgentBase-Custom-Api-Key (target agent built from this template)
        user_jwt — forwards the end-user's JWT (target agent on the same IdP)
        none     — local only
  No IAM mode: a target whose Runtime Inbound Auth is "IAM Permissions" can't be called with this client.

Per-user isolation: always sends X-GreenNode-AgentBase-User-Id = current user (from RunnableConfig,
not from the LLM) and contextId = current session ⇒ the target agent keeps memory per user/session.

Credentials only go to the configured origin: the RPC URL comes from the target's Agent Card, so interfaces
on another scheme/host/port are dropped (none left ⇒ the call is refused before anything is sent).
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from a2a.client import A2ACardResolver, Client, ClientConfig, ClientFactory
from a2a.types import a2a_pb2 as pb
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, StructuredTool

from app.a2a.decisions import parse_decision
from app.auth.inbound import Principal
from app.config import Settings
from app.hitl import requires_approval
from app.observability import tracing

log = logging.getLogger(__name__)
_NAME_RE = re.compile(r"[^a-z0-9_]+")
_DEFAULT_PORTS = {"http": 80, "https": 443}
_AUTH_MODES = ("api_key", "user_jwt", "none")
_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand_env(value: Any, missing: set[str]) -> Any:
    """Expand `${VAR}` inside string leaves of the ALREADY-PARSED JSON — never over the raw text, where a `"` or
    `\\` in a value (API keys) breaks the whole file and a crafted value can inject keys such as "auth".
    Substituted values are not re-expanded. Unset/empty variables go to `missing` (the caller skips the agent
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


def load_a2a_agents(settings: Settings) -> dict[str, dict[str, Any]]:
    path = Path(settings.a2a_agents_file)
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    agents = {}
    for name, entry in (raw.get("agents") or {}).items():
        if not isinstance(entry, dict) or not entry.get("enabled", True):
            continue
        missing: set[str] = set()
        cfg = _expand_env(entry, missing)
        if missing:
            log.error(
                "A2A agent %r skipped — environment variable(s) %s unset or empty (referenced as ${VAR} "
                "in a2a_agents.json)",
                name,
                ", ".join(sorted(missing)),
            )
            continue
        # Fail closed: an unknown mode (e.g. "iam", not supported) must not silently send no credentials
        if cfg.get("auth", "api_key") not in _AUTH_MODES:
            log.error(
                "A2A agent %r skipped: auth must be one of %s (got %r)",
                name,
                list(_AUTH_MODES),
                cfg.get("auth"),
            )
            continue
        # Each call carries an API key or the user's JWT ⇒ never over plain http outside local
        if not settings.is_local and not str(cfg.get("url", "")).startswith("https://"):
            log.error("A2A agent %r skipped: url must be https:// outside APP_ENV=local", name)
            continue
        agents[name] = cfg
    return agents


def _headers(cfg: dict, settings: Settings, principal: Principal, user_id: str) -> dict[str, str]:
    h = {"X-GreenNode-AgentBase-User-Id": user_id}
    if cfg.get("auth", "api_key") == "api_key":
        h[cfg.get("api_key_header", settings.auth_api_key_header)] = cfg["api_key"]
    elif cfg["auth"] == "user_jwt":
        if not principal.token:
            raise PermissionError(
                "Target agent requires the user's JWT but the request has no token"
            )
        h["Authorization"] = f"Bearer {principal.token}"
    return h


def _origin(url: str) -> tuple[str, str, int | None] | None:
    try:
        p = urlsplit(url)
        scheme = p.scheme.lower()
        return scheme, (p.hostname or "").lower(), p.port or _DEFAULT_PORTS.get(scheme)
    except ValueError:  # malformed port
        return None


async def _pinned_client(url: str, http: httpx.AsyncClient) -> Client:
    """A2A client whose RPC endpoint is on the CONFIGURED origin. The Agent Card (served by the target) names
    the RPC URL; following it blindly would send our API key / the user's JWT wherever the card points (another
    host, plain http). Only interfaces with the same scheme + host + port as `url` are kept."""
    want = _origin(url)
    if want is None or not want[1]:
        raise PermissionError(f"Invalid A2A agent url {url!r}")
    card = await A2ACardResolver(http, url).get_agent_card()
    kept = [i for i in card.supported_interfaces if _origin(i.url) == want]
    if not kept:
        advertised = [i.url for i in card.supported_interfaces]
        raise PermissionError(
            f"Agent card of {url} advertises RPC endpoints on another origin {advertised}: refusing to send "
            "credentials there (the target must set A2A_PUBLIC_URL to the URL configured here)"
        )
    pinned = pb.AgentCard()
    pinned.CopyFrom(card)
    del pinned.supported_interfaces[:]
    pinned.supported_interfaces.extend(kept)
    return ClientFactory(ClientConfig(streaming=False, httpx_client=http)).create(pinned)


def _collect_text(resp: Any) -> tuple[str, str]:
    """(state, text) from the a2a client's StreamResponse / (StreamResponse, Task)."""
    if isinstance(resp, tuple):
        resp = resp[0]
    task = resp.task if resp.HasField("task") else None
    if task is not None:
        parts = [p.text for a in task.artifacts for p in a.parts if p.text]
        if not parts and task.status.HasField("message"):
            parts = [p.text for p in task.status.message.parts if p.text]
        return pb.TaskState.Name(task.status.state), "\n".join(parts)
    if resp.HasField("message"):
        return "MESSAGE", "\n".join(p.text for p in resp.message.parts if p.text)
    return "UNKNOWN", ""


def build_a2a_tools(settings: Settings, principal: Principal) -> list[BaseTool]:
    tools: list[BaseTool] = []
    for name, cfg in load_a2a_agents(settings).items():
        tool_name = f"ask_{_NAME_RE.sub('_', name.lower())}"

        async def _call(message: str, config: RunnableConfig, _n=name, _c=cfg, _t=tool_name) -> str:
            # The target's HITL accepts "approve"/"reject" as a decision. Only a HUMAN may send it: the LLM
            # could otherwise confirm the other agent's side effects by itself (prompt injection, overeager
            # model). With `_t` in HITL_TOOLS the user approves this exact call first ⇒ relaying is allowed.
            if parse_decision(message) and not requires_approval(_t, settings):
                log.warning("A2A decision %r to %s blocked: add %r to HITL_TOOLS", message, _n, _t)
                return (
                    f"[{_n}] NOT SENT: confirming or rejecting an action of agent '{_n}' requires the "
                    "user's explicit approval, which is not configured for this agent. Tell the user the "
                    "action was not confirmed."
                )
            conf = config.get("configurable") or {}
            user_id, session_id = conf["actor_id"], conf["thread_id"]
            with tracing.step(
                "a2a.call", input={"agent": _n, "url": _c["url"], "message": message}
            ) as st:
                headers = _headers(_c, settings, principal, user_id)
                async with httpx.AsyncClient(
                    headers=headers, timeout=settings.a2a_timeout_s
                ) as http:
                    client = await _pinned_client(_c["url"], http)
                    req = pb.SendMessageRequest(
                        message=pb.Message(
                            message_id=os.urandom(8).hex(),
                            role=pb.ROLE_USER,
                            # session_id is already validated ([A-Za-z0-9-]) ⇒ sent as is: rewriting it
                            # (e.g. lower-casing) would merge distinct sessions on the target
                            context_id=session_id,
                            parts=[pb.Part(text=message)],
                        )
                    )
                    state, text = "UNKNOWN", ""
                    async for resp in client.send_message(req):
                        state, text = _collect_text(resp)
                st.set(output={"state": state, "text": text[:2000]})
            if state == "TASK_STATE_INPUT_REQUIRED":
                return (
                    f"[{_n} needs confirmation] {text}\n(Ask the user. Only send 'approve' / "
                    "'reject: <reason>' after the user explicitly decides.)"
                )
            if state in ("TASK_STATE_FAILED", "TASK_STATE_REJECTED"):
                return f"[{_n} error] {text}"
            return text

        tools.append(
            StructuredTool.from_function(
                coroutine=_call,
                name=tool_name,
                description=f"Ask agent '{name}' via A2A. {cfg.get('description', '')}".strip(),
            )
        )
    return tools
