"""A2A end-to-end: real app (uvicorn) + real a2a-sdk client. Fake LLM, in-memory memory."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import importlib
import json
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import jwt
import pytest
import uvicorn
from a2a.client import ClientConfig, create_client
from a2a.types import a2a_pb2 as pb
from a2a.utils.errors import TaskNotFoundError
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from langchain_core.messages import AIMessage, ToolMessage

from app.config import get_settings

KEY = "a2a-test-key"
RPC = {"jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": {}}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class HttpStub:
    """Plain HTTP server on 127.0.0.1 serving fixed JSON per path and recording every request it receives."""

    def __init__(self, routes: dict[str, dict] | None = None) -> None:
        self.routes = routes or {}
        self.requests: list[tuple[str, str, dict]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _reply(self):
                n = int(self.headers.get("Content-Length") or 0)
                if n:
                    self.rfile.read(n)
                outer.requests.append((self.command, self.path, dict(self.headers)))
                body = json.dumps(outer.routes.get(self.path, {})).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_POST = _reply  # noqa: N815

            def log_message(self, *args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.httpd.server_port}"

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


@contextlib.contextmanager
def _serve(monkeypatch, env: dict[str, str]):
    """The real app (main.py) on uvicorn, A2A enabled."""
    port = _free_port()
    for k, v in {
        "A2A_ENABLED": "true",
        "A2A_PUBLIC_URL": f"http://127.0.0.1:{port}",
        **env,
    }.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()
    from app.auth import inbound

    inbound._jwks_client.cache_clear()
    sys.modules.pop("main", None)
    main = importlib.import_module("main")
    srv = uvicorn.Server(uvicorn.Config(main.app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=srv.run, daemon=True).start()
    for _ in range(200):
        if srv.started:
            break
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        srv.should_exit = True


@pytest.fixture
def a2a_server(monkeypatch):
    env = {
        "AUTH_MODE": "api_key",
        "AUTH_API_KEY_SHA256": f'["{hashlib.sha256(KEY.encode()).hexdigest()}"]',
        "HITL_TOOLS": '["remember"]',
    }
    with _serve(monkeypatch, env) as base:
        yield base


JWT_ENV = {
    "AUTH_MODE": "jwt",
    "AUTH_ISSUER": "https://issuer.test",
    "AUTH_AUDIENCE": "agent-api",
    "AUTH_FORWARD_CLAIMS": '["roles"]',
}


@pytest.fixture
def a2a_jwt_server(monkeypatch):
    """App in AUTH_MODE=jwt against a real local JWKS endpoint; yields (base url, make_token)."""
    signer = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(signer.public_key(), as_dict=True)
    jwks = HttpStub({"/jwks.json": {"keys": [{**jwk, "kid": "k1", "alg": "RS256", "use": "sig"}]}})

    def make_token(sub="user-1", *, key=signer, alg="RS256", **claims) -> str:
        now = int(time.time())
        body = {"sub": sub, "iss": "https://issuer.test", "aud": "agent-api", "iat": now}
        return jwt.encode(
            {**body, "exp": now + 300, **claims}, key, algorithm=alg, headers={"kid": "k1"}
        )

    with _serve(monkeypatch, {**JWT_ENV, "AUTH_JWKS_URL": f"{jwks.base}/jwks.json"}) as base:
        yield base, make_token
    jwks.close()


async def _send(base: str, user: str, text: str, context_id: str):
    headers = {"X-GreenNode-AgentBase-Custom-Api-Key": KEY, "X-GreenNode-AgentBase-User-Id": user}
    return await _send_with(base, headers, text, context_id)


async def _send_with(base: str, headers: dict, text: str, context_id: str):
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
            "",
            tool_calls=[{"name": "remember", "args": {"fact": "allergic to shrimp"}, "id": "c1"}],
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


async def _ask_peer(a2a_server, tmp_path, monkeypatch, message: str, hitl: str = "[]"):
    """Call ask_peer(message); returns (tool output, number of requests that reached the target)."""
    import app.a2a.server as srv_mod

    hits = []
    original = srv_mod.AgentBaseAuthBackend.authenticate

    async def spy(self, conn):
        if conn.url.path.startswith("/a2a"):
            hits.append(conn.url.path)
        return await original(self, conn)

    monkeypatch.setattr(srv_mod.AgentBaseAuthBackend, "authenticate", spy)
    cfg = tmp_path / "a2a_agents.json"
    cfg.write_text(
        f'{{"agents": {{"peer": {{"url": "{a2a_server}", "auth": "api_key", "api_key": "{KEY}"}}}}}}'
    )
    monkeypatch.setenv("A2A_AGENTS_FILE", str(cfg))
    monkeypatch.setenv("HITL_TOOLS", hitl)
    get_settings.cache_clear()
    from app.a2a.client import build_a2a_tools
    from app.auth.inbound import Principal

    [tool] = build_a2a_tools(get_settings(), Principal(user_id="alice"))
    out = await tool.ainvoke(
        {"message": message}, config={"configurable": {"actor_id": "alice", "thread_id": "s-1"}}
    )
    return out, len(hits)


@pytest.mark.parametrize("word", ["approve", "Yes", "đồng ý", "reject: too risky"])
async def test_llm_cannot_relay_decision_without_hitl(
    a2a_server, fake_llm, tmp_path, monkeypatch, word
):
    out, reached = await _ask_peer(a2a_server, tmp_path, monkeypatch, word)
    assert reached == 0  # never sent: the LLM must not confirm the other agent's actions by itself
    assert "NOT SENT" in out


async def test_decision_relayed_when_tool_is_human_approved(
    a2a_server, fake_llm, tmp_path, monkeypatch
):
    fake_llm(AIMessage("peer handled it"))
    out, reached = await _ask_peer(a2a_server, tmp_path, monkeypatch, "approve", hitl='["ask_*"]')
    assert reached >= 1 and "peer handled it" in out


def test_plain_http_agents_refused_outside_local(tmp_path, monkeypatch, caplog):
    from app.a2a.client import load_a2a_agents

    cfg = tmp_path / "a2a_agents.json"
    cfg.write_text(
        '{"agents": {"plain": {"url": "http://peer.internal", "auth": "api_key", "api_key": "k"},'
        ' "tls": {"url": "https://peer.example", "auth": "api_key", "api_key": "k"}}}'
    )
    for k, v in {
        "A2A_AGENTS_FILE": str(cfg),
        "APP_ENV": "dev",
        "AUTH_MODE": "api_key",
        "AUTH_API_KEY_SHA256": '["' + "0" * 64 + '"]',
    }.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()
    assert set(load_a2a_agents(get_settings())) == {"tls"}
    assert "must be https" in caplog.text


# --------------------------------------------------------------------------- verified identity reaches the graph
async def test_a2a_runs_with_verified_claims_and_token(a2a_jwt_server, fake_llm, monkeypatch):
    """/a2a must hand the graph the SAME Principal as /invocations: AUTH_FORWARD_CLAIMS reach tools (RBAC)
    and the token is kept (user_jwt to downstream agents) — not a claim-less Principal(user_id)."""
    import app.service as service_mod
    from app.tools.local_tools import get_current_time, whoami

    base, make_token = a2a_jwt_server
    monkeypatch.setattr("app.tools.get_local_tools", lambda: [get_current_time, whoami])
    seen = []
    original = service_mod.collect_tools

    async def spy(settings, principal):
        seen.append(principal)
        return await original(settings, principal)

    monkeypatch.setattr(service_mod, "collect_tools", spy)
    model = fake_llm(
        AIMessage("", tool_calls=[{"name": "whoami", "args": {}, "id": "w1"}]), AIMessage("done")
    )
    token = make_token(roles=["admin"])
    task, _, _ = await _send_with(base, {"Authorization": f"Bearer {token}"}, "who am I?", "ctx-c")
    assert task.status.state == pb.TASK_STATE_COMPLETED
    [tool_msg] = [m for m in model.calls[-1] if isinstance(m, ToolMessage)]
    assert "user_id=user-1" in tool_msg.content and "'roles'" in tool_msg.content
    assert seen[-1].token == token and seen[-1].claims["roles"] == ["admin"]


# --------------------------------------------------------------------------- auth errors keep their status
def test_a2a_api_key_errors_keep_their_status(a2a_server):
    url = f"{a2a_server}/a2a"
    no_user = {"X-GreenNode-AgentBase-Custom-Api-Key": KEY}
    assert httpx.post(url, json=RPC, headers=no_user).status_code == 400  # missing User-Id
    wrong = {"X-GreenNode-AgentBase-Custom-Api-Key": "nope", "X-GreenNode-AgentBase-User-Id": "a"}
    assert httpx.post(url, json=RPC, headers=wrong).status_code == 401


def test_a2a_and_invocations_jwt_errors_keep_their_status(a2a_jwt_server):
    base, make_token = a2a_jwt_server
    spoof = {
        "Authorization": f"Bearer {make_token()}",
        "X-GreenNode-AgentBase-User-Id": "someone-else",
    }
    assert httpx.post(f"{base}/a2a", json=RPC, headers=spoof).status_code == 403
    # ES256 token pointing at the RSA kid: rejected as 401 (used to be an unhandled TypeError ⇒ 500)
    forged = make_token(key=ec.generate_private_key(ec.SECP256R1()), alg="ES256")
    bad = {"Authorization": f"Bearer {forged}", "X-GreenNode-AgentBase-Session-Id": "s-1"}
    assert httpx.post(f"{base}/a2a", json=RPC, headers=bad).status_code == 401
    r = httpx.post(f"{base}/invocations", json={"message": "hi"}, headers=bad)
    assert r.status_code == 401


def test_a2a_idp_outage_is_503(monkeypatch):
    dead_jwks = f"http://127.0.0.1:{_free_port()}/jwks.json"  # nothing listens there
    with _serve(monkeypatch, {**JWT_ENV, "AUTH_JWKS_URL": dead_jwks}) as base:
        token = jwt.encode(
            {"sub": "u", "exp": int(time.time()) + 60},
            rsa.generate_private_key(public_exponent=65537, key_size=2048),
            algorithm="RS256",
            headers={"kid": "k1"},
        )
        r = httpx.post(f"{base}/a2a", json=RPC, headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 503


# --------------------------------------------------------------------------- HITL races
async def test_concurrent_approvals_do_not_ask_again(a2a_server, fake_llm, monkeypatch):
    """Two 'approve' for one pending action: one runs it; the other must NOT say an action is still awaiting
    confirmation (nothing is pending any more)."""
    import app.service as service_mod

    fake_llm(
        AIMessage("", tool_calls=[{"name": "remember", "args": {"fact": "likes tea"}, "id": "c1"}]),
        AIMessage("Remembered."),
    )
    task, _, _ = await _send(a2a_server, "alice", "remember I like tea", "ctx-race")
    assert task.status.state == pb.TASK_STATE_INPUT_REQUIRED
    original = service_mod._Run._prepare

    async def slow_prepare(self):  # the first resume holds the session lock a while
        out = await original(self)
        if self.kind == "resume":
            await asyncio.sleep(0.5)
        return out

    monkeypatch.setattr(service_mod._Run, "_prepare", slow_prepare)
    (t1, _, _), (t2, _, _) = await asyncio.gather(
        _send(a2a_server, "alice", "approve", "ctx-race"),
        _send(a2a_server, "alice", "approve", "ctx-race"),
    )
    states = sorted([t1.status.state, t2.status.state])
    assert states == sorted([pb.TASK_STATE_COMPLETED, pb.TASK_STATE_FAILED])
    loser = t1 if t1.status.state == pb.TASK_STATE_FAILED else t2
    assert "already handled" in _text(loser)


async def test_chat_while_approval_pending_asks_for_the_decision(a2a_server, fake_llm):
    fake_llm(
        AIMessage("", tool_calls=[{"name": "remember", "args": {"fact": "x"}, "id": "c1"}]),
        AIMessage("Remembered."),
    )
    task, _, _ = await _send(a2a_server, "alice", "remember x", "ctx-busy")
    assert task.status.state == pb.TASK_STATE_INPUT_REQUIRED
    task, _, _ = await _send(a2a_server, "alice", "what's the weather?", "ctx-busy")
    assert task.status.state == pb.TASK_STATE_INPUT_REQUIRED
    assert "awaiting confirmation" in _text(task) and "remember" in _text(task)


# --------------------------------------------------------------------------- client: where credentials go
def _peer_config(tmp_path, monkeypatch, url: str) -> None:
    cfg = tmp_path / "a2a_agents.json"
    cfg.write_text(
        json.dumps({"agents": {"peer": {"url": url, "auth": "api_key", "api_key": KEY}}})
    )
    monkeypatch.setenv("A2A_AGENTS_FILE", str(cfg))
    get_settings.cache_clear()


async def test_card_pointing_elsewhere_gets_no_credentials(a2a_server, tmp_path, monkeypatch):
    """The target's Agent Card names the RPC URL. A card advertising another origin (plain http, other host)
    must not receive the API key / user JWT."""
    from app.a2a.client import build_a2a_tools
    from app.auth.inbound import Principal

    async with httpx.AsyncClient() as http:
        card = (await http.get(f"{a2a_server}/.well-known/agent-card.json")).json()
    elsewhere = HttpStub()
    card["supportedInterfaces"][0]["url"] = f"{elsewhere.base}/a2a"
    target = HttpStub({"/.well-known/agent-card.json": card})
    try:
        _peer_config(tmp_path, monkeypatch, target.base)
        [tool] = build_a2a_tools(get_settings(), Principal(user_id="alice"))
        with pytest.raises(Exception) as e:  # noqa: B017 — HEAD failed later with a parse error
            await tool.ainvoke(
                {"message": "hi"},
                config={"configurable": {"actor_id": "alice", "thread_id": "s-1"}},
            )
        assert (
            elsewhere.requests == []
        )  # nothing — let alone the API key — reached the other origin
        assert "another origin" in str(e.value)
    finally:
        target.close()
        elsewhere.close()


async def test_context_id_keeps_distinct_sessions_distinct(
    a2a_server, fake_llm, tmp_path, monkeypatch
):
    """contextId = the caller's (validated) session id, unchanged: 'ABC-1' and 'XYZ-1' used to both become '-1'
    (upper-case stripped) ⇒ two sessions shared the target's memory."""
    import app.service as service_mod
    from app.a2a.client import build_a2a_tools
    from app.auth.inbound import Principal

    sessions = []
    original = service_mod.run_chat

    async def spy(message, **kw):
        sessions.append(kw["session_id"])
        return await original(message, **kw)

    monkeypatch.setattr(service_mod, "run_chat", spy)
    fake_llm(AIMessage("ok"))
    _peer_config(tmp_path, monkeypatch, a2a_server)
    [tool] = build_a2a_tools(get_settings(), Principal(user_id="alice"))
    for thread in ("ABC-1", "XYZ-1"):
        conf = {"configurable": {"actor_id": "alice", "thread_id": thread}}
        await tool.ainvoke({"message": "hi"}, config=conf)
    assert sessions == ["a2a-ABC-1", "a2a-XYZ-1"]


# --------------------------------------------------------------------------- a2a_agents.json parsing
def _load(tmp_path, monkeypatch, agents: dict, **env: str):
    from app.a2a.client import load_a2a_agents

    cfg = tmp_path / "a2a_agents.json"
    cfg.write_text(json.dumps({"agents": agents}))
    monkeypatch.setenv("A2A_AGENTS_FILE", str(cfg))
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()
    return load_a2a_agents(get_settings())


def test_env_values_with_quotes_and_backslashes_are_kept_verbatim(tmp_path, monkeypatch):
    key = 'k"ey\\with\\back\\slash'
    agents = {
        "peer": {"url": "${PEER_URL}", "auth": "api_key", "api_key": "${PEER_KEY}"},
        "other": {"url": "https://other.example", "auth": "api_key", "api_key": "static"},
    }
    loaded = _load(tmp_path, monkeypatch, agents, PEER_URL="https://peer.example", PEER_KEY=key)
    assert loaded["peer"]["api_key"] == key and loaded["peer"]["url"] == "https://peer.example"
    assert "other" in loaded  # one odd value doesn't break every agent


def test_env_value_cannot_inject_config_keys(tmp_path, monkeypatch):
    agents = {"peer": {"url": "https://peer.example", "auth": "api_key", "api_key": "${PEER_KEY}"}}
    evil = 'x", "auth": "none", "x": "'
    loaded = _load(tmp_path, monkeypatch, agents, PEER_KEY=evil)
    assert loaded["peer"]["auth"] == "api_key" and loaded["peer"]["api_key"] == evil


def test_unset_env_var_skips_agent_instead_of_sending_literal(tmp_path, monkeypatch, caplog):
    monkeypatch.delenv("PEER_KEY_UNSET", raising=False)
    agents = {
        "peer": {"url": "https://p.example", "auth": "api_key", "api_key": "${PEER_KEY_UNSET}"},
        "off": {"url": "${ALSO_UNSET}", "auth": "api_key", "api_key": "k", "enabled": False},
    }
    assert _load(tmp_path, monkeypatch, agents) == {}
    assert "PEER_KEY_UNSET" in caplog.text and "ALSO_UNSET" not in caplog.text


def test_unknown_auth_mode_is_skipped(tmp_path, monkeypatch, caplog):
    agents = {"peer": {"url": "https://p.example", "auth": "iam"}}
    assert _load(tmp_path, monkeypatch, agents) == {}
    assert "auth must be one of" in caplog.text
