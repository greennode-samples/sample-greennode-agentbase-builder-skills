"""A2A end-to-end: real app (uvicorn) + real a2a-sdk client. Fake LLM, in-memory memory."""

from __future__ import annotations

import hashlib
import importlib
import socket
import sys
import threading
import time

import httpx
import pytest
import uvicorn
from a2a.client import ClientConfig, create_client
from a2a.types import a2a_pb2 as pb
from a2a.utils.errors import TaskNotFoundError
from langchain_core.messages import AIMessage

from app.config import get_settings

KEY = "a2a-test-key"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def a2a_server(monkeypatch):
    port = _free_port()
    monkeypatch.setenv("AUTH_MODE", "api_key")
    monkeypatch.setenv("AUTH_API_KEY_SHA256", f'["{hashlib.sha256(KEY.encode()).hexdigest()}"]')
    monkeypatch.setenv("A2A_ENABLED", "true")
    monkeypatch.setenv("A2A_PUBLIC_URL", f"http://127.0.0.1:{port}")
    monkeypatch.setenv("HITL_TOOLS", '["remember"]')
    get_settings.cache_clear()
    sys.modules.pop("main", None)
    main = importlib.import_module("main")
    srv = uvicorn.Server(uvicorn.Config(main.app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=srv.run, daemon=True).start()
    for _ in range(200):
        if srv.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True


async def _send(base: str, user: str, text: str, context_id: str):
    headers = {"X-GreenNode-AgentBase-Custom-Api-Key": KEY, "X-GreenNode-AgentBase-User-Id": user}
    async with httpx.AsyncClient(headers=headers, timeout=30) as http:
        client = await create_client(
            base, client_config=ClientConfig(streaming=False, httpx_client=http)
        )
        req = pb.SendMessageRequest(
            message=pb.Message(
                message_id=f"m-{time.time_ns()}",
                role=pb.ROLE_USER,
                context_id=context_id,
                parts=[pb.Part(text=text)],
            )
        )
        last = None
        async for resp in client.send_message(req):
            last = resp[0] if isinstance(resp, tuple) else resp
        return last.task, client, http


def _text(task: pb.Task) -> str:
    parts = [p.text for a in task.artifacts for p in a.parts if p.text]
    return "\n".join(parts) or "\n".join(p.text for p in task.status.message.parts)


def test_agent_card_public_and_rpc_requires_auth(a2a_server):
    card = httpx.get(f"{a2a_server}/.well-known/agent-card.json").json()
    assert card["supportedInterfaces"][0]["url"].endswith("/a2a")
    assert "apiKey" in card["securitySchemes"]
    r = httpx.post(f"{a2a_server}/a2a", json={"jsonrpc": "2.0", "id": 1, "method": "SendMessage"})
    assert r.status_code == 401


async def test_send_message_completes(a2a_server, fake_llm):
    fake_llm(AIMessage("Hello from the agent"))
    task, _, _ = await _send(a2a_server, "alice", "hi", "ctx-1")
    assert task.status.state == pb.TASK_STATE_COMPLETED
    assert "Hello from the agent" in _text(task)


async def test_tasks_isolated_per_user(a2a_server, fake_llm):
    fake_llm(AIMessage("alice's secret"))
    task, _, _ = await _send(a2a_server, "alice", "hi", "ctx-iso")
    headers = {"X-GreenNode-AgentBase-Custom-Api-Key": KEY, "X-GreenNode-AgentBase-User-Id": "bob"}
    async with httpx.AsyncClient(headers=headers, timeout=30) as http:
        bob = await create_client(
            a2a_server, client_config=ClientConfig(streaming=False, httpx_client=http)
        )
        with pytest.raises(TaskNotFoundError):  # task store keyed by owner = user
            await bob.get_task(pb.GetTaskRequest(id=task.id))
    headers["X-GreenNode-AgentBase-User-Id"] = "alice"
    async with httpx.AsyncClient(headers=headers, timeout=30) as http:
        alice = await create_client(
            a2a_server, client_config=ClientConfig(streaming=False, httpx_client=http)
        )
        got = await alice.get_task(pb.GetTaskRequest(id=task.id))
        assert got.id == task.id


async def test_hitl_over_a2a(a2a_server, fake_llm):
    fake_llm(
        AIMessage(
            "", tool_calls=[{"name": "remember", "args": {"fact": "allergic to shrimp"}, "id": "c1"}]
        ),
        AIMessage("Remembered."),
    )
    task, _, _ = await _send(a2a_server, "alice", "remember: I'm allergic to shrimp", "ctx-hitl")
    assert task.status.state == pb.TASK_STATE_INPUT_REQUIRED
    assert "remember" in _text(task)
    task2, _, _ = await _send(a2a_server, "alice", "approve", "ctx-hitl")
    assert task2.status.state == pb.TASK_STATE_COMPLETED and "Remembered" in _text(task2)


async def test_client_tool_propagates_user(a2a_server, fake_llm, tmp_path, monkeypatch):
    seen = {}
    import app.a2a.server as srv_mod

    original = srv_mod.AgentBaseAuthBackend.authenticate

    async def spy(self, conn):
        result = await original(self, conn)
        seen["user"] = result[1].username
        return result

    monkeypatch.setattr(srv_mod.AgentBaseAuthBackend, "authenticate", spy)
    cfg = tmp_path / "a2a_agents.json"
    cfg.write_text(
        f'{{"agents": {{"peer": {{"url": "{a2a_server}", "auth": "api_key", "api_key": "{KEY}"}}}}}}'
    )
    monkeypatch.setenv("A2A_AGENTS_FILE", str(cfg))
    get_settings.cache_clear()
    fake_llm(AIMessage("answer from peer"))
    from app.a2a.client import build_a2a_tools
    from app.auth.inbound import Principal

    [tool] = build_a2a_tools(get_settings(), Principal(user_id="alice"))
    assert tool.name == "ask_peer"
    out = await tool.ainvoke(
        {"message": "hello"}, config={"configurable": {"actor_id": "alice", "thread_id": "s-1"}}
    )
    assert "answer from peer" in out
    assert seen["user"] == "alice"  # target agent gets the right user ⇒ per-user memory isolation
