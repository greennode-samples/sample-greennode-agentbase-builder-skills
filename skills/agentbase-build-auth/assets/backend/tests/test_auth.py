from __future__ import annotations

import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from greennode_agentbase import RequestContext
from greennode_agentbase.exceptions import GreenNodeRequestError

from app.auth import inbound
from app.config import get_settings


@pytest.fixture
def jwt_env(monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    monkeypatch.setenv("AUTH_MODE", "jwt")
    monkeypatch.setenv("AUTH_JWKS_URL", "https://issuer.test/jwks.json")
    monkeypatch.setenv("AUTH_ISSUER", "https://issuer.test")
    monkeypatch.setenv("AUTH_AUDIENCE", "agent-api")
    get_settings.cache_clear()

    class _Key:
        def __init__(self, k):
            self.key = k

    class _Client:
        def get_signing_key_from_jwt(self, token):
            return _Key(key.public_key())

    monkeypatch.setattr(inbound, "_jwks_client", lambda url: _Client())

    def make(sub="user-1", **over):
        now = int(time.time())
        claims = {
            "sub": sub,
            "iss": "https://issuer.test",
            "aud": "agent-api",
            "iat": now,
            "exp": now + 300,
            **over,
        }
        return jwt.encode(claims, key, algorithm="RS256")

    return make


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
        with pytest.raises(GreenNodeRequestError):
            inbound.authenticate(_ctx(token), get_settings())


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
