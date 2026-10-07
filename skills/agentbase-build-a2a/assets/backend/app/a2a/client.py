"""A2A client — calls OTHER agents (A2A servers) as a LangChain tool.

Declared in a2a_agents.json (supports ${ENV}):
  {"agents": {"stock": {"url": "https://<endpoint>", "description": "Stock agent...",
                        "auth": "api_key", "api_key": "${A2A_STOCK_API_KEY}"}}}
  auth: api_key  — sends header X-GreenNode-AgentBase-Custom-Api-Key (target agent built from this template)
        user_jwt — forwards the end-user's JWT (target agent on the same IdP)
        none     — local only

Per-user isolation: always sends X-GreenNode-AgentBase-User-Id = current user (from RunnableConfig,
not from the LLM) and contextId = current session ⇒ the target agent keeps memory per user/session.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

import httpx
from a2a.client import ClientConfig, create_client
from a2a.types import a2a_pb2 as pb
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, StructuredTool

from app.auth.inbound import Principal
from app.config import Settings
from app.observability import tracing

log = logging.getLogger(__name__)
_NAME_RE = re.compile(r"[^a-z0-9_]+")


def load_a2a_agents(settings: Settings) -> dict[str, dict[str, Any]]:
    path = Path(settings.a2a_agents_file)
    if not path.exists():
        return {}
    raw = json.loads(os.path.expandvars(path.read_text(encoding="utf-8")))
    return {n: c for n, c in (raw.get("agents") or {}).items() if c.get("enabled", True)}


def _headers(cfg: dict, settings: Settings, principal: Principal, user_id: str) -> dict[str, str]:
    h = {"X-GreenNode-AgentBase-User-Id": user_id}
    if cfg.get("auth", "api_key") == "api_key":
        h[cfg.get("api_key_header", settings.auth_api_key_header)] = cfg["api_key"]
    elif cfg["auth"] == "user_jwt":
        if not principal.token:
            raise PermissionError("Target agent requires the user's JWT but the request has no token")
        h["Authorization"] = f"Bearer {principal.token}"
    return h


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

        async def _call(message: str, config: RunnableConfig, _n=name, _c=cfg) -> str:
            conf = config.get("configurable") or {}
            user_id, session_id = conf["actor_id"], conf["thread_id"]
            with tracing.step(
                "a2a.call", input={"agent": _n, "url": _c["url"], "message": message}
            ) as st:
                headers = _headers(_c, settings, principal, user_id)
                async with httpx.AsyncClient(
                    headers=headers, timeout=settings.a2a_timeout_s
                ) as http:
                    client = await create_client(
                        _c["url"], client_config=ClientConfig(streaming=False, httpx_client=http)
                    )
                    req = pb.SendMessageRequest(
                        message=pb.Message(
                            message_id=os.urandom(8).hex(),
                            role=pb.ROLE_USER,
                            context_id=_NAME_RE.sub("-", f"{session_id}"),
                            parts=[pb.Part(text=message)],
                        )
                    )
                    state, text = "UNKNOWN", ""
                    async for resp in client.send_message(req):
                        state, text = _collect_text(resp)
                st.set(output={"state": state, "text": text[:2000]})
            if state == "TASK_STATE_INPUT_REQUIRED":
                return f"[{_n} needs confirmation] {text}"
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
