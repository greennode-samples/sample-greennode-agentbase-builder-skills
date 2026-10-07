"""Authentication / authorization of the server itself (rarely changes when you add tools)."""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
import subprocess
import sys
import time
from types import SimpleNamespace

import httpx
import jwt
import jwt.jwk_set_cache
import pytest
from conftest import (
    API_KEY,
    API_KEY_ENV,
    JWKS,
    JWT_ENV,
    LOCAL_ENV,
    ROOT,
    call,
    free_port,
    list_tools,
    make_token,
    text,
)
from cryptography.hazmat.primitives.asymmetric import ec

LIST = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}


def test_health_is_public(api_key_server):
    assert httpx.get(f"{api_key_server}/health").status_code == 200


def test_mcp_requires_auth(api_key_server):
    r = httpx.post(f"{api_key_server}/mcp", json=LIST)
    assert r.status_code == 401
    assert "resource_metadata" in r.headers.get("www-authenticate", "")  # RFC 9728 discovery


def test_wrong_api_key_rejected(api_key_server):
    r = httpx.post(f"{api_key_server}/mcp", headers={"Authorization": "Bearer nope"}, json=LIST)
    assert r.status_code == 401


async def test_api_key_lists_and_calls_but_no_user_identity(start_server):
    # Grant the notes scopes so the refusal can only come from current_user(): 2LO has no user, and
    # all API-key callers must never share one notes bucket.
    base = start_server({**API_KEY_ENV, "MCP_API_KEY_SCOPES": '["notes.read", "notes.write"]'})
    assert "add_note" in [t.name for t in await list_tools(base, API_KEY)]
    assert not (await call(base, API_KEY, "server_time")).isError
    for tool, args in (("list_notes", {}), ("add_note", {"text": "x"})):
        res = await call(base, API_KEY, tool, args)
        assert res.isError and "requires user identity" in text(res), text(res)


async def test_jwt_missing_scope(jwt_server):
    res = await call(jwt_server, make_token("carol", scope="notes.read"), "add_note", {"text": "x"})
    assert res.isError and "notes.write" in text(res)


@pytest.mark.parametrize(
    "override",
    [
        {"aud": "other-api"},  # token issued for another API of the same IdP
        {"aud": None},  # no audience at all
        {"iss": "https://evil.test"},
        {"exp": int(time.time()) - 3600},
    ],
    ids=["wrong-audience", "missing-audience", "wrong-issuer", "expired"],
)
def test_jwt_invalid_tokens_rejected(jwt_server, override):
    r = httpx.post(
        f"{jwt_server}/mcp",
        headers={"Authorization": f"Bearer {make_token('carol', **override)}"},
        json=LIST,
    )
    assert r.status_code == 401


def test_jwt_requires_audience():
    """No MCP_AUDIENCE ⇒ refuse to start (otherwise tokens minted for any other API would pass)."""
    sys.modules.pop("settings", None)
    from settings import Settings

    env = {k.removeprefix("MCP_").lower(): v for k, v in JWT_ENV.items() if k != "MCP_AUDIENCE"}
    with pytest.raises(ValueError, match="MCP_AUDIENCE"):
        Settings(**env)


async def test_jwt_verifier_never_skips_audience(monkeypatch):
    """Even if Settings validation is bypassed (empty audience), the verifier fails closed."""
    for mod in ("settings", "auth"):
        sys.modules.pop(mod, None)
    from auth import JwtVerifier
    from settings import Settings

    s = Settings.model_construct(
        auth_mode="jwt", issuer="https://idp.test", jwks_url=JWKS.url, audience=""
    )
    verifier = JwtVerifier(s)
    for aud in ("other-api", None):
        assert await verifier.verify_token(make_token("carol", aud=aud)) is None


def _status(base: str, token: str) -> int:
    """Status of a raw MCP request (200 = token accepted)."""
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream"}
    return httpx.post(f"{base}/mcp", headers=headers, json=LIST).status_code


def test_jwt_alg_key_mismatch_is_401_not_500(jwt_server):
    """Forger signs with its own EC key, header ES256 + our RSA kid: PyJWT raised TypeError ⇒ 500."""
    forged = make_token("carol", alg="ES256", key=ec.generate_private_key(ec.SECP256R1()))
    assert _status(jwt_server, forged) == 401


async def test_jwt_ec_key_from_jwks_accepted(jwt_server):
    JWKS.add("ec1", "EC")
    assert await list_tools(jwt_server, make_token("carol", kid="ec1"))


async def test_jwt_key_removed_from_jwks_rejected_after_cache_lifespan(jwt_server, monkeypatch):
    """IdP rotates k1 out (e.g. compromised): once the cached JWK set expires (1h) k1 tokens must fail.
    A per-kid cache that never expires (PyJWKClient cache_keys=True) trusted k1 until restart."""
    old = make_token("carol")
    assert _status(jwt_server, old) == 200  # k1 fetched and cached
    JWKS.add("k2")
    JWKS.published = ["k2"]
    real = time.monotonic
    monkeypatch.setattr(  # only the JWK-set cache's clock moves past its 1h lifespan
        jwt.jwk_set_cache, "time", SimpleNamespace(monotonic=lambda: real() + 3601)
    )
    assert _status(jwt_server, old) == 401
    assert await list_tools(jwt_server, make_token("carol", kid="k2"))


async def test_slow_jwks_fetch_does_not_block_other_requests(jwt_server):
    """A JWKS cache miss (slow IdP) must not stall the event loop: /health stays fast meanwhile."""
    JWKS.delay = 1.5
    headers = {
        "Authorization": f"Bearer {make_token('carol')}",
        "Accept": "application/json, text/event-stream",
    }
    async with httpx.AsyncClient(base_url=jwt_server, timeout=10) as client:
        slow = asyncio.create_task(client.post("/mcp", headers=headers, json=LIST))
        await asyncio.sleep(0.3)  # the server is now fetching the JWKS for that token
        start = time.monotonic()
        assert (await client.get("/health")).status_code == 200
        assert time.monotonic() - start < 0.5 and not slow.done()
        assert (await slow).status_code == 200  # and the token is still verified


def test_metadata_endpoint(jwt_server):
    r = httpx.get(f"{jwt_server}/.well-known/oauth-protected-resource/mcp")
    if r.status_code == 404:
        r = httpx.get(f"{jwt_server}/.well-known/oauth-protected-resource")
    assert r.status_code == 200 and "https://idp.test" in r.text


def test_settings_fail_closed_without_env(monkeypatch):
    """No env at all (e.g. a runtime with a missing env file) ⇒ refuse to start unauthenticated."""
    sys.modules.pop("settings", None)
    from settings import Settings

    with pytest.raises(ValueError, match="only allowed"):
        Settings()
    monkeypatch.setenv("MCP_APP_ENV", "dev")
    with pytest.raises(ValueError, match="only allowed"):
        Settings()
    monkeypatch.setenv("MCP_APP_ENV", "local")  # what .env.example sets for local dev
    assert Settings().auth_mode == "none"


def _dockerfile_env() -> dict[str, str]:
    """MCP_* variables the image sets (Dockerfile `ENV` instructions, with line continuations)."""
    joined = (ROOT / "Dockerfile").read_text().replace("\\\n", " ")
    env: dict[str, str] = {}
    for line in joined.splitlines():
        if line.strip().upper().startswith("ENV "):
            for pair in shlex.split(line.strip()[4:]):
                k, _, v = pair.partition("=")
                if k.startswith("MCP_"):
                    env[k] = v
    return env


def test_docker_image_refuses_auth_none(monkeypatch):
    """The image's own env + an empty/incomplete runtime env file ⇒ startup error, never open."""
    image_env = _dockerfile_env()
    assert image_env.get("MCP_APP_ENV") == "prod"
    for k, v in image_env.items():
        monkeypatch.setenv(k, v)
    sys.modules.pop("settings", None)
    from settings import Settings

    with pytest.raises(ValueError, match="MCP_AUTH_MODE=none is only allowed"):
        Settings()
    monkeypatch.setenv("MCP_AUTH_MODE", "api_key")  # incomplete: no key hash
    with pytest.raises(ValueError, match="MCP_API_KEY_SHA256"):
        Settings()


def test_auth_none_binds_loopback_and_warns(load_server, caplog):
    with caplog.at_level(logging.WARNING):
        local = load_server(LOCAL_ENV)
    assert local.mcp.settings.host == "127.0.0.1"  # unauthenticated ⇒ never on the network
    assert "authentication is DISABLED" in caplog.text
    assert load_server(API_KEY_ENV).mcp.settings.host == "0.0.0.0"  # Runtime needs 0.0.0.0


def test_local_quickstart_real_process(tmp_path):
    """Exactly what the skill says: cp .env.example .env && python server.py, then call tools."""
    port = free_port()
    env_text = (ROOT / ".env.example").read_text().replace("8080", str(port))
    (tmp_path / ".env").write_text(env_text)
    env = {k: v for k, v in os.environ.items() if not k.startswith("MCP_")}
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "server.py")],
        cwd=tmp_path,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        for _ in range(100):
            if proc.poll() is not None:
                pytest.fail(f"server crashed on startup:\n{proc.stdout.read()}")
            try:
                if httpx.get(f"http://127.0.0.1:{port}/health").status_code == 200:
                    break
            except httpx.ConnectError:
                time.sleep(0.1)
        else:
            pytest.fail("server did not become healthy")

        def cli(*args: str) -> subprocess.CompletedProcess:
            return subprocess.run(
                [sys.executable, str(ROOT / "scripts" / "call_tool.py"), *args],
                env={**env, "MCP_URL": f"http://127.0.0.1:{port}/mcp"},
                capture_output=True,
                text=True,
                timeout=30,
            )

        assert "add_note" in cli().stdout
        assert cli("add_note", '{"text": "hello local"}').returncode == 0
        listed = cli("list_notes")
        assert listed.returncode == 0 and "hello local" in listed.stdout
        assert (tmp_path / "mcp.db").exists()  # data persisted in the local SQLite file
    finally:
        proc.terminate()
        proc.communicate(timeout=10)  # also closes the stdout pipe
