from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from typing import Any

import jwt
import jwt.jwk_set_cache
import pytest
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from greennode_agentbase import RequestContext
from greennode_agentbase.exceptions import GreenNodeRequestError

from app.auth import inbound
from app.config import get_settings


class JwksServer:
    """A real JWKS endpoint on 127.0.0.1: PyJWKClient fetches it over HTTP, so kid lookup, the key's alg/type
    and key rotation go through the production code path (no mocked signing key)."""

    def __init__(self) -> None:
        self.private: dict[str, Any] = {}
        self.published: list[str] = []
        self.broken = False  # True ⇒ answer like a proxy maintenance page (200, HTML)
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                if outer.broken:
                    body = b"<html><body>Down for maintenance</body></html>"
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                body = json.dumps({"keys": [outer.jwk(k) for k in outer.published]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_port}/jwks.json"

    def add(self, kid: str, kind: str = "RSA") -> None:
        self.private[kid] = (
            rsa.generate_private_key(public_exponent=65537, key_size=2048)
            if kind == "RSA"
            else ec.generate_private_key(ec.SECP256R1())
        )
        self.published.append(kid)

    def jwk(self, kid: str) -> dict:
        key = self.private[kid]
        if isinstance(key, rsa.RSAPrivateKey):
            data, alg = jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key(), as_dict=True), "RS256"
        else:
            data, alg = jwt.algorithms.ECAlgorithm.to_jwk(key.public_key(), as_dict=True), "ES256"
        return {**data, "kid": kid, "alg": alg, "use": "sig"}

    def close(self) -> None:
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None


@pytest.fixture
def jwt_env(monkeypatch):
    jwks = JwksServer()
    jwks.add("k1")
    monkeypatch.setenv("AUTH_MODE", "jwt")
    monkeypatch.setenv("AUTH_JWKS_URL", jwks.url)
    monkeypatch.setenv("AUTH_ISSUER", "https://issuer.test")
    monkeypatch.setenv("AUTH_AUDIENCE", "agent-api")
    get_settings.cache_clear()
    inbound._jwks_client.cache_clear()

    def make(sub="user-1", *, kid="k1", alg=None, key=None, **over):
        now = int(time.time())
        claims = {
            "sub": sub,
            "iss": "https://issuer.test",
            "aud": "agent-api",
            "iat": now,
            "exp": now + 300,
            **over,
        }
        claims = {k: v for k, v in claims.items() if v is not None}  # aud=None ⇒ no `aud` claim
        signer = key or jwks.private[kid]
        alg = alg or ("RS256" if isinstance(signer, rsa.RSAPrivateKey) else "ES256")
        return jwt.encode(claims, signer, algorithm=alg, headers={"kid": kid})

    make.jwks = jwks
    yield make
    jwks.close()
    inbound._jwks_client.cache_clear()


def _ctx(token=None, user=None):
    headers = {"Authorization": f"Bearer {token}"} if token else None
    return RequestContext(session_id="s", user_id=user, request_headers=headers)


def test_valid_token_sets_user(jwt_env):
    p = inbound.authenticate(_ctx(jwt_env()), get_settings())
    assert p.user_id == "user-1"


def test_missing_token(jwt_env):
    with pytest.raises(GreenNodeRequestError) as e:
        inbound.authenticate(_ctx(), get_settings())
    assert e.value.status_code == 401


def test_expired_and_wrong_audience(jwt_env):
    for token in (jwt_env(exp=int(time.time()) - 100), jwt_env(aud="other")):
        with pytest.raises(GreenNodeRequestError) as e:
            inbound.authenticate(_ctx(token), get_settings())
        assert e.value.status_code == 401


def test_es256_header_with_rsa_kid_is_401_not_500(jwt_env):
    """alg/key-type mismatch (EC-signed ES256 token pointing at an RSA kid) used to raise TypeError ⇒ 500."""
    forged = jwt_env(kid="k1", alg="ES256", key=ec.generate_private_key(ec.SECP256R1()))
    with pytest.raises(GreenNodeRequestError) as e:
        inbound.authenticate(_ctx(forged), get_settings())
    assert e.value.status_code == 401


def test_ec_key_from_jwks_accepted(jwt_env):
    jwt_env.jwks.add("ec1", "EC")
    assert inbound.authenticate(_ctx(jwt_env(kid="ec1")), get_settings()).user_id == "user-1"


def test_key_removed_from_jwks_is_rejected_after_cache_lifespan(jwt_env, monkeypatch):
    """The IdP rotates k1 out (e.g. compromised). Once the cached JWK set expires (1h), k1 tokens must fail —
    a never-expiring per-kid cache (PyJWKClient cache_keys=True) kept trusting k1 until restart."""
    old = jwt_env()
    assert inbound.authenticate(_ctx(old), get_settings()).user_id == "user-1"
    jwt_env.jwks.add("k2")
    jwt_env.jwks.published = ["k2"]
    real = time.monotonic
    monkeypatch.setattr(  # only the JWK-set cache's clock moves past its 1h lifespan
        jwt.jwk_set_cache, "time", SimpleNamespace(monotonic=lambda: real() + 3601)
    )
    with pytest.raises(GreenNodeRequestError) as e:
        inbound.authenticate(_ctx(old), get_settings())
    assert e.value.status_code == 401
    assert inbound.authenticate(_ctx(jwt_env(kid="k2")), get_settings()).user_id == "user-1"


def test_jwks_unreachable_is_503(jwt_env):
    token = jwt_env()
    jwt_env.jwks.close()
    with pytest.raises(GreenNodeRequestError) as e:
        inbound.authenticate(_ctx(token), get_settings())
    assert e.value.status_code == 503


def test_jwks_maintenance_page_is_503_not_401(jwt_env):
    """IdP/proxy outage serving HTML at the JWKS URL: 503 (client retries), never 401 (would log users out)."""
    token = jwt_env()
    jwt_env.jwks.broken = True
    with pytest.raises(GreenNodeRequestError) as e:
        inbound.authenticate(_ctx(token), get_settings())
    assert e.value.status_code == 503


def test_keycloak_id_token_rejected(jwt_env):
    """Keycloak marks the token type in `typ` (access: Bearer; ID: ID)."""
    assert inbound.authenticate(_ctx(jwt_env(typ="Bearer")), get_settings()).user_id == "user-1"
    with pytest.raises(GreenNodeRequestError) as e:
        inbound.authenticate(_ctx(jwt_env(typ="ID")), get_settings())
    assert e.value.status_code == 401


def test_subject_shaped_like_a_hashed_id_cannot_take_it_over(jwt_env):
    """`auth0|abc` maps to u-<hash>; a different account whose raw `sub` IS that u-<hash> must not become it."""
    hashed = inbound.user_id_from_subject("auth0|abc", "https://issuer.test")
    assert hashed.startswith("u-") and len(hashed) == 42
    assert inbound.authenticate(_ctx(jwt_env("auth0|abc")), get_settings()).user_id == hashed
    squatter = inbound.authenticate(_ctx(jwt_env(hashed)), get_settings()).user_id
    assert squatter != hashed
    assert inbound.user_id_from_subject("user-1", "https://issuer.test") == "user-1"


def test_user_header_spoofing_rejected(jwt_env):
    with pytest.raises(GreenNodeRequestError) as e:
        inbound.authenticate(_ctx(jwt_env(), user="someone-else"), get_settings())
    assert e.value.status_code == 403


@pytest.fixture
def api_key_env(monkeypatch):
    import hashlib

    monkeypatch.setenv("AUTH_MODE", "api_key")
    monkeypatch.setenv("AUTH_API_KEY_SHA256", f'["{hashlib.sha256(b"secret-key-1").hexdigest()}"]')
    get_settings.cache_clear()


def _key_ctx(key=None, user=None):
    headers = {"X-GreenNode-AgentBase-Custom-Api-Key": key} if key else None
    return RequestContext(session_id="s", user_id=user, request_headers=headers)


def test_api_key_valid_uses_caller_user_id(api_key_env):
    assert (
        inbound.authenticate(_key_ctx("secret-key-1", "alice"), get_settings()).user_id == "alice"
    )


def test_api_key_requires_user_id_no_shared_bucket(api_key_env):
    with pytest.raises(GreenNodeRequestError) as e:
        inbound.authenticate(_key_ctx("secret-key-1"), get_settings())
    assert e.value.status_code == 400


def test_api_key_missing_or_wrong(api_key_env):
    for ctx in (_key_ctx(), _key_ctx("wrong")):
        with pytest.raises(GreenNodeRequestError) as e:
            inbound.authenticate(ctx, get_settings())
        assert e.value.status_code == 401


def test_api_key_mode_requires_hashes(monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "api_key")
    monkeypatch.delenv("AUTH_API_KEY_SHA256", raising=False)
    get_settings.cache_clear()
    with pytest.raises(ValueError, match="AUTH_API_KEY_SHA256"):
        get_settings()


# --- APP_ENV must never default to local on the Runtime (local allows AUTH_MODE=none ⇒ User-Id spoofing)
def test_local_app_env_refused_on_runtime(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # Runtime image: no .greennode.json
    # Deploy env file that forgot APP_ENV ⇒ default "local"
    monkeypatch.delenv("APP_ENV", raising=False)
    monkeypatch.setenv("GREENNODE_AGENT_IDENTITY", "injected-by-runtime")
    get_settings.cache_clear()
    with pytest.raises(ValueError, match="APP_ENV=local is not allowed on AgentBase Runtime"):
        get_settings()


def test_local_dev_with_identity_env_still_allowed(monkeypatch, tmp_path):
    """A developer may export GREENNODE_AGENT_IDENTITY locally (Identity decorators); .greennode.json marks local."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".greennode.json").write_text('{"client_id": "c", "client_secret": "s"}')
    monkeypatch.setenv("GREENNODE_AGENT_IDENTITY", "my-agent")
    get_settings.cache_clear()
    assert get_settings().is_local


# --- No-audience IdPs (e.g. Cognito access tokens): the client allowlist replaces `aud`
@pytest.fixture
def no_aud_env(jwt_env, monkeypatch):
    monkeypatch.delenv("AUTH_AUDIENCE", raising=False)
    monkeypatch.setenv("AUTH_ALLOW_NO_AUDIENCE", "true")
    monkeypatch.setenv("AUTH_ALLOWED_CLIENT_IDS", '["app-1"]')
    get_settings.cache_clear()
    return jwt_env


def test_no_audience_accepts_allowed_client(no_aud_env):
    for claim in ("client_id", "azp"):
        token = no_aud_env(aud=None, **{claim: "app-1"}, token_use="access")
        assert inbound.authenticate(_ctx(token), get_settings()).user_id == "user-1"


def test_no_audience_rejects_other_client_and_id_tokens(no_aud_env):
    for token in (
        no_aud_env(aud=None, client_id="other-app"),  # another app of the same user pool
        no_aud_env(aud=None),  # no client claim at all
        no_aud_env(aud=None, client_id="app-1", token_use="id"),  # ID token, not access token
    ):
        with pytest.raises(GreenNodeRequestError) as e:
            inbound.authenticate(_ctx(token), get_settings())
        assert e.value.status_code == 401


def test_no_audience_outside_local_requires_client_allowlist(monkeypatch):
    for k, v in {
        "APP_ENV": "prod",
        "AUTH_MODE": "jwt",
        "AUTH_JWKS_URL": "https://issuer.test/jwks.json",
        "AUTH_ISSUER": "https://issuer.test",
        "AUTH_ALLOW_NO_AUDIENCE": "true",
        "MEMORY_BACKEND": "agentbase",
        "MEMORY_ID": "mem-1",
        "MEMORY_STRATEGY_ID": "strat-1",  # valid memory config: isolate the auth check
    }.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("AUTH_AUDIENCE", raising=False)
    monkeypatch.delenv("AUTH_ALLOWED_CLIENT_IDS", raising=False)
    get_settings.cache_clear()
    with pytest.raises(ValueError, match="AUTH_ALLOWED_CLIENT_IDS"):
        get_settings()
