"""Test harness: real server (uvicorn, in a thread) + real MCP client over streamable HTTP.

Adding a tool? Write its tests in tests/test_tools.py with these fixtures — no harness code needed:
  local_server  — MCP_AUTH_MODE=none, user = "dev-user"
  jwt_server    — MCP_AUTH_MODE=jwt; mint tokens with make_token("alice", scope="…"), aud=None drops a claim.
                  Keys come from JWKS, a real local JWKS endpoint (JWKS.add("k2"), JWKS.published = [...])
  api_key_server— MCP_AUTH_MODE=api_key; token = API_KEY (no user identity; scopes = MCP_API_KEY_SCOPES,
                  `catalog.read` in API_KEY_ENV)
  start_server(env, backend=handler) — any env + a fake internal system (httpx.MockTransport handler)
  load_server(env) — import server.py with that env, without serving (e.g. check mcp.settings)
  call(base, token, tool, args) / list_tools(base, token) / session(base, token, fn)
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import socket
import sys
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType

import httpx
import jwt
import pytest
import uvicorn
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client
from mcp.types import CallToolResult, Tool

ROOT = Path(__file__).resolve().parents[1]
API_KEY = "test-key-123"
ISSUER, AUDIENCE = "https://idp.test", "mcp-api"
ALL_SCOPES = "notes.read notes.write catalog.read"


class JwksServer:
    """The IdP's JWKS endpoint, for real, on 127.0.0.1: the server's PyJWKClient fetches it over HTTP, so
    kid lookup, key type/alg checks and key rotation run through the production code (no mocked key)."""

    def __init__(self) -> None:
        self.private: dict[str, rsa.RSAPrivateKey | ec.EllipticCurvePrivateKey] = {}
        self.published: list[str] = []
        self.delay = 0.0  # seconds before answering (a slow IdP)
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                time.sleep(outer.delay)
                body = json.dumps({"keys": [outer.jwk(k) for k in outer.published]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{httpd.server_port}/jwks.json"
        self.add("k1")

    def add(self, kid: str, kind: str = "RSA") -> None:
        """Generate a key pair and publish its public key (kind: RSA → RS256, EC → ES256)."""
        self.private[kid] = (
            rsa.generate_private_key(public_exponent=65537, key_size=2048)
            if kind == "RSA"
            else ec.generate_private_key(ec.SECP256R1())
        )
        self.published.append(kid)

    def jwk(self, kid: str) -> dict:
        pub = self.private[kid].public_key()
        if isinstance(pub, rsa.RSAPublicKey):
            data, alg = jwt.algorithms.RSAAlgorithm.to_jwk(pub, as_dict=True), "RS256"
        else:
            data, alg = jwt.algorithms.ECAlgorithm.to_jwk(pub, as_dict=True), "ES256"
        return {**data, "kid": kid, "alg": alg, "use": "sig"}

    def reset(self) -> None:
        self.published, self.delay = ["k1"], 0.0


JWKS = JwksServer()  # one for the test run; reset after every test

LOCAL_ENV = {"MCP_APP_ENV": "local", "MCP_AUTH_MODE": "none"}
API_KEY_ENV = {
    "MCP_APP_ENV": "dev",
    "MCP_AUTH_MODE": "api_key",
    "MCP_API_KEY_SHA256": f'["{hashlib.sha256(API_KEY.encode()).hexdigest()}"]',
    "MCP_API_KEY_SCOPES": '["catalog.read"]',  # as in .env.example: shared-data tools only
}
JWT_ENV = {
    "MCP_APP_ENV": "dev",
    "MCP_AUTH_MODE": "jwt",
    "MCP_ISSUER": ISSUER,
    "MCP_JWKS_URL": JWKS.url,
    "MCP_AUDIENCE": AUDIENCE,
}


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch, tmp_path):
    """Run in an empty dir (own SQLite file, no developer .env) with no MCP_* vars from the shell."""
    monkeypatch.chdir(tmp_path)
    for k in list(os.environ):
        if k.startswith("MCP_"):
            monkeypatch.delenv(k)
    yield
    JWKS.reset()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def load_server(monkeypatch) -> Callable[[dict[str, str]], ModuleType]:
    def _load(env: dict[str, str]) -> ModuleType:
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        for mod in ("settings", "auth", "store", "backend", "server"):
            sys.modules.pop(mod, None)
        return importlib.import_module("server")

    return _load


@pytest.fixture
def start_server(load_server) -> Callable[..., str]:
    servers: list[uvicorn.Server] = []

    def _start(
        env: dict[str, str], backend: Callable[[httpx.Request], httpx.Response] | None = None
    ):
        server = load_server(env)  # fresh modules ⇒ fresh JWKS cache per server
        if backend is not None:
            from backend import Backend

            server.backend = Backend(
                "http://backend.test", 2, transport=httpx.MockTransport(backend)
            )
        port = free_port()
        srv = uvicorn.Server(
            uvicorn.Config(
                server.mcp.streamable_http_app(), host="127.0.0.1", port=port, log_level="warning"
            )
        )
        threading.Thread(target=srv.run, daemon=True).start()
        for _ in range(100):
            if srv.started:
                break
            time.sleep(0.05)
        servers.append(srv)
        return f"http://127.0.0.1:{port}"

    yield _start
    for srv in servers:
        srv.should_exit = True


@pytest.fixture
def local_server(start_server) -> str:
    return start_server(LOCAL_ENV)


@pytest.fixture
def api_key_server(start_server) -> str:
    return start_server(API_KEY_ENV)


@pytest.fixture
def jwt_server(start_server) -> str:
    return start_server(JWT_ENV)


def make_token(
    sub: str,
    scope: str = ALL_SCOPES,
    *,
    kid: str = "k1",
    alg: str | None = None,
    key=None,
    **override,
) -> str:
    """JWT signed with JWKS key `kid` (or `key`, e.g. a forger's). Override any claim; `claim=None` removes
    it (e.g. aud=None). `alg` defaults to RS256 / ES256 from the signing key's type."""
    now = int(time.time())
    claims = {
        "sub": sub,
        "iss": ISSUER,
        "aud": AUDIENCE,
        "iat": now,
        "exp": now + 300,
        "scope": scope,
        "azp": "agent-client",
        **override,
    }
    claims = {k: v for k, v in claims.items() if v is not None}
    signer = key or JWKS.private[kid]
    alg = alg or ("RS256" if isinstance(signer, rsa.RSAPrivateKey) else "ES256")
    return jwt.encode(claims, signer, algorithm=alg, headers={"kid": kid})


async def session(base: str, token: str | None, fn):
    """Open one MCP session and return `await fn(client_session)`."""
    headers = {"Authorization": f"Bearer {token}"} if token else None
    async with create_mcp_http_client(headers=headers) as http:
        async with streamable_http_client(f"{base}/mcp", http_client=http) as (r, w, _):
            async with ClientSession(r, w) as s:
                await s.initialize()
                return await fn(s)


async def call(base: str, token: str | None, tool: str, args: dict | None = None) -> CallToolResult:
    return await session(base, token, lambda s: s.call_tool(tool, args or {}))


async def list_tools(base: str, token: str | None = None) -> list[Tool]:
    async def _list(s):
        return (await s.list_tools()).tools

    return await session(base, token, _list)


def text(res: CallToolResult) -> str:
    return " ".join(getattr(c, "text", "") for c in res.content)
