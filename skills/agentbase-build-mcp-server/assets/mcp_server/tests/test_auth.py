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
    RESOURCE_URL,
    ROOT,
    call,
    free_port,
    list_tools,
    make_token,
    text,
)
from cryptography.hazmat.primitives.asymmetric import ec

LIST = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
ALL_ROLES = ["notes.read", "notes.write"]


def test_health_is_public(api_key_server):
    assert httpx.get(f"{api_key_server}/health").status_code == 200


def test_mcp_requires_auth(api_key_server, jwt_server):
    r = httpx.post(f"{jwt_server}/mcp", json=LIST)
    assert r.status_code == 401
    # RFC 9728 discovery points at the public https resource URL (MCP_RESOURCE_URL), never localhost
    www = r.headers.get("www-authenticate", "")
    assert 'resource_metadata="https://mcp.test/.well-known/oauth-protected-resource/mcp"' in www
    # api_key: no authorization server to advertise ⇒ no resource_metadata
    r = httpx.post(f"{api_key_server}/mcp", json=LIST)
    assert r.status_code == 401 and "resource_metadata" not in r.headers.get("www-authenticate", "")


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


def test_metadata_endpoint(jwt_server, api_key_server):
    r = httpx.get(f"{jwt_server}/.well-known/oauth-protected-resource/mcp")
    assert r.status_code == 200
    meta = r.json()
    assert meta["resource"] == RESOURCE_URL
    assert meta["authorization_servers"] == ["https://idp.test/"]
    # api_key mode used to advertise ITSELF (http://localhost…) as the authorization server
    r = httpx.get(f"{api_key_server}/.well-known/oauth-protected-resource/mcp")
    assert r.status_code == 404


def test_jwt_algorithms_must_be_asymmetric():
    """Same rule as the agent (AUTH_ALGORITHMS): HS256 with a public JWKS = anyone can sign; none = unsigned."""
    sys.modules.pop("settings", None)
    from settings import Settings

    env = {k.removeprefix("MCP_").lower(): v for k, v in JWT_ENV.items()}
    for bad in (["HS256"], ["RS256", "none"], []):
        with pytest.raises(ValueError, match="MCP_JWT_ALGORITHMS"):
            Settings(**{**env, "jwt_algorithms": bad})
    ok = Settings(**{**env, "jwt_algorithms": ["RS256", "EdDSA"]})
    assert ok.jwt_algorithms == ["RS256", "EdDSA"]


def test_jwt_outside_local_requires_https_resource_url(monkeypatch):
    sys.modules.pop("settings", None)
    from settings import Settings

    env = {k.removeprefix("MCP_").lower(): v for k, v in JWT_ENV.items()}
    for bad in ("http://localhost:8080/mcp", "http://mcp.example.com/mcp"):
        with pytest.raises(ValueError, match="MCP_RESOURCE_URL"):
            Settings(**{**env, "resource_url": bad})
    assert Settings(**{**env, "resource_url": "http://localhost:8081/mcp", "app_env": "local"})
    for k, v in API_KEY_ENV.items():
        monkeypatch.setenv(k, v)
    # api_key never publishes it ⇒ not required
    assert Settings().resource_url.startswith("http://")


# ----------------------------------------------------------------------------- OAuth 2LO = no user
@pytest.mark.parametrize(
    "claims",
    [
        {"sub": "agent-client@clients", "gty": "client-credentials"},  # Auth0
        {"sub": "agent-client"},  # Okta & most IdPs: sub = client id (azp="agent-client")
        {"sub": "0oa1", "cid": "0oa1", "azp": None},  # Okta `cid`
        {"sub": "sa-uuid", "preferred_username": "service-account-agent-client"},  # Keycloak
        {"sub": "sp-oid", "oid": "sp-oid", "scope": None, "roles": ALL_ROLES},  # Entra app-only
        {"sub": "sp-oid", "idtyp": "app"},  # Entra with the idtyp optional claim
    ],
    ids=["auth0-gty", "sub-is-azp", "okta-cid", "keycloak-sa", "entra-sub-oid", "entra-idtyp"],
)
async def test_oauth_2lo_token_has_no_user(jwt_server, claims):
    """A client-credentials token carries `sub` (the client's): accepting it as a user would put every user of
    the agent in ONE notes bucket. Shared tools still work for it."""
    tok = make_token(**claims)
    assert not (await call(jwt_server, tok, "server_time")).isError
    for tool, args in (("list_notes", {}), ("add_note", {"text": "x"})):
        res = await call(jwt_server, tok, tool, args)
        assert res.isError and "requires user identity" in text(res), text(res)


async def test_user_token_claim_required_when_configured(start_server):
    base = start_server({**JWT_ENV, "MCP_USER_TOKEN_CLAIM": "email"})
    machine = await call(base, make_token("svc-123"), "list_notes")  # no email ⇒ 2LO
    assert machine.isError and "requires user identity" in text(machine)
    user = await call(base, make_token("alice", email="alice@vng.com.vn"), "list_notes")
    assert not user.isError and user.structuredContent["total"] == 0


async def test_entra_app_roles_are_scopes(start_server):
    """Entra app-only tokens carry application permissions in `roles` (no `scp`)."""

    def catalog(request: httpx.Request) -> httpx.Response:
        body = {"sku": "SKU-1", "name": "Lamp", "price": 1.0, "currency": "USD", "in_stock": True}
        return httpx.Response(200, json=body)

    base = start_server(JWT_ENV, backend=catalog)
    tok = make_token("sp-oid", oid="sp-oid", scope=None, roles=["catalog.read"])
    res = await call(base, tok, "get_product", {"sku": "SKU-1"})
    assert not res.isError and res.structuredContent["name"] == "Lamp", text(res)
    no_role = make_token("sp-oid", oid="sp-oid", scope=None, roles=["other.read"])
    res = await call(base, no_role, "get_product", {"sku": "SKU-1"})
    assert res.isError and "catalog.read" in text(res)


def test_jwks_unreachable_is_401_not_500(start_server):
    base = start_server({**JWT_ENV, "MCP_JWKS_URL": f"http://127.0.0.1:{free_port()}/jwks.json"})
    assert _status(base, make_token("carol")) == 401


def test_require_scope_fails_closed_without_token(load_server):
    """Outside a request carrying a verified token (get_access_token() is None) ⇒ refused, not allowed."""
    server = load_server(JWT_ENV)
    with pytest.raises(server.Forbidden, match="catalog.read"):
        server.require_scope("catalog.read")
    load_server(LOCAL_ENV).require_scope("catalog.read")  # auth none (local only): always passes


def test_sqlite_refused_outside_local(load_server, monkeypatch, caplog):
    """Runtime FS is wiped on redeploy and replicas don't share it ⇒ SQLite only with an explicit opt-in."""
    monkeypatch.delenv("MCP_ALLOW_SQLITE", raising=False)
    env = {k: v for k, v in JWT_ENV.items() if k != "MCP_ALLOW_SQLITE"}
    with pytest.raises(RuntimeError, match="Postgres"):
        load_server(env)
    with caplog.at_level(logging.ERROR):
        load_server({**env, "MCP_ALLOW_SQLITE": "true"})
    assert "LOST on every redeploy" in caplog.text


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
