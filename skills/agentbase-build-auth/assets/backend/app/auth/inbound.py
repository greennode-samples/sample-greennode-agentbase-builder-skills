"""Inbound auth — authenticates the agent's CALLER (frontend / other services).

Principles:
- The user_id used for memory/identity MUST come from the verified token; don't trust the
  X-GreenNode-AgentBase-User-Id header sent by the client. If the client sends it and it differs from `sub` => 403.
- After verification, override user_id in GreenNodeAgentBaseContext so @requires_api_key /
  @requires_access_token (USER_FEDERATION) use the right user.
- AUTH_MODE=none only runs with APP_ENV=local (enforced in config).
- The Runtime's own Inbound Auth (IAM / JWT / None) and IP access control are optional settings, and
  "No authorization" leaves the endpoint public ⇒ the agent ALWAYS authenticates its caller itself:
  jwt or api_key is mandatory outside local (defense in depth, whatever the Runtime setting).
- api_key: only for trusted callers (server/BFF/job/test) — NEVER embed the key in a mobile app. The
  User-Id header is REQUIRED (no shared bucket) — the trusted caller is responsible for its value (a BFF that
  verified the user's IdP token sends `user_id_from_subject(sub, iss)` — the same id jwt mode would use).
  Env stores only the key's SHA-256.
- The caller's User-Id is read from the HTTP request itself, never from `context.user_id`: the SDK fills that
  from the `agent_user_id` config value (.greennode.json / env) when the header is absent.
- Every user_id (header or JWT sub) goes through validate_user_id() before touching memory.
- Multi-tenant (AUTH_TENANT_CLAIM): the tenant comes from the verified token only and is part of the user id
  (`u-<sha256([iss, tenant, sub])>`), so every per-user store is per tenant too. api_key callers (trusted BFF) send that
  id themselves: `user_id_from_subject(sub, iss, tenant)`. Roles (AUTH_ROLES_CLAIM) gate tools via TOOL_ROLES.
- The IdP is bounded: JWKS fetch timeout JWKS_TIMEOUT_S, whole authentication AUTH_TIMEOUT_S ⇒ 503 (PyJWKClient
  fetches under one process-wide lock — an unbounded IdP outage would queue every request behind it).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import re
from dataclasses import dataclass, field
from fnmatch import fnmatch
from functools import lru_cache
from typing import Any

import jwt
from greennode_agentbase import GreenNodeAgentBaseContext, RequestContext
from greennode_agentbase.exceptions import GreenNodeRequestError

from app import health
from app.config import Settings

log = logging.getLogger(__name__)
LOCAL_USER_ID = "local-user"
USER_ID_HEADER = "X-GreenNode-AgentBase-User-Id"
JWKS_TIMEOUT_S = 5.0  # one JWKS HTTP fetch (PyJWT default: 30 s)
AUTH_TIMEOUT_S = 10.0  # the whole authentication incl. waiting for another request's fetch
# Unknown `kid` ⇒ at most one forced JWKS refetch per this window (random-kid tokens can't hammer the IdP)
JWKS_REFETCH_COOLDOWN_S = 30.0
# user_id is put into memory paths (/actors/{user_id}, /actors/{user}/sessions/...) ⇒ only safe
# characters allowed; "/", "..", whitespace... are banned to prevent namespace injection / cross-user reads.
USER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@+=-]{0,127}$")
# session_id/contextId goes into Memory paths (/actors/{u}/sessions/{s}/events) — the SDK does NOT encode it, httpx
# normalizes "../" ⇒ "../../victim/sessions/x" reads/writes another user's history (confirmed). Disallow
# "_" (the bridge cache key is f"{thread}_{actor}"), ":" (in-memory key), ".", "/", "%".
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,127}$")
TENANT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@:-]{0,127}$")


def validate_user_id(user_id: str) -> str:
    if not USER_ID_RE.fullmatch(user_id) or ".." in user_id:
        raise GreenNodeRequestError("Invalid user id format", status_code=400)
    return user_id


def validate_session_id(session_id: str | None) -> str:
    if not session_id or not SESSION_ID_RE.fullmatch(session_id):
        raise GreenNodeRequestError(
            "Invalid session id (letters, digits, '-' only; max 128 chars — use a UUID)",
            status_code=400,
        )
    return session_id


# Shape of a hashed user id. A raw subject with this shape is hashed too, otherwise a `sub` equal to another
# user's hashed id would map to that user (collision ⇒ cross-user memory).
HASHED_USER_ID_RE = re.compile(r"^u-[0-9a-f]{40}$")


def user_id_from_subject(subject: str, issuer: str | None, tenant: str | None = None) -> str:
    """An IdP `sub` may contain path-unsafe characters (Auth0 `auth0|abc`, ...) ⇒ map it stably
    to `u-<sha256(iss|sub)>` instead of rejecting. With a tenant the id is always hashed with it, JSON-encoded so
    no two (tenant, sub) pairs share an input ("a" + "b|c" vs "a|b" + "c")."""
    if tenant:
        scoped = json.dumps([issuer or "", tenant, subject], ensure_ascii=False)
        return "u-" + hashlib.sha256(scoped.encode()).hexdigest()[:40]
    if (
        USER_ID_RE.fullmatch(subject)
        and ".." not in subject
        and not HASHED_USER_ID_RE.fullmatch(subject)
    ):
        return subject
    return "u-" + hashlib.sha256(f"{issuer or ''}|{subject}".encode()).hexdigest()[:40]


@dataclass(frozen=True)
class Principal:
    user_id: str
    # repr=False: a Principal in a log line / exception / trace must not print the bearer token or the claims
    token: str | None = field(default=None, repr=False)
    claims: dict[str, Any] = field(default_factory=dict, repr=False)
    tenant_id: str | None = None
    roles: frozenset[str] = frozenset()


def _claim(claims: dict[str, Any], path: str) -> Any:
    """`realm_access.roles` → claims["realm_access"]["roles"] (a top-level key with dots wins)."""
    if path in claims:
        return claims[path]
    value: Any = claims
    for part in path.split("."):
        value = value.get(part) if isinstance(value, dict) else None
    return value


def tenant_from_claims(claims: dict[str, Any], settings: Settings) -> str | None:
    if not settings.auth_tenant_claim:
        return None
    raw = _claim(claims, settings.auth_tenant_claim)
    tenant = str(raw) if isinstance(raw, str | int) and str(raw) else ""
    if not tenant:
        raise _unauthorized(
            f"Token missing tenant claim '{settings.auth_tenant_claim}'", status=403
        )
    if not TENANT_ID_RE.fullmatch(tenant) or ".." in tenant:
        raise _unauthorized("Invalid tenant id format", status=403)
    if settings.auth_allowed_tenants and tenant not in settings.auth_allowed_tenants:
        raise _unauthorized("Tenant not allowed", status=403)
    return tenant


def roles_from_claims(claims: dict[str, Any], settings: Settings) -> frozenset[str]:
    if not settings.auth_roles_claim:
        return frozenset()
    raw = _claim(claims, settings.auth_roles_claim)
    if isinstance(raw, str):
        raw = raw.split()
    return (
        frozenset(str(r) for r in raw if isinstance(r, str | int))
        if isinstance(raw, list)
        else frozenset()
    )


def tool_allowed(tool_name: str, principal: Principal, settings: Settings) -> bool:
    """TOOL_ROLES: a tool matching a pattern needs at least one of that pattern's roles (every matching pattern)."""
    return all(
        principal.roles & set(roles)
        for pattern, roles in settings.tool_roles.items()
        if fnmatch(tool_name, pattern)
    )


def _unauthorized(msg: str, status: int = 401) -> GreenNodeRequestError:
    return GreenNodeRequestError(msg, status_code=status)


def _idp_unavailable() -> GreenNodeRequestError:
    # 503, not 401: a 401 would log every user out during an IdP / JWKS outage
    return _unauthorized("Identity provider unavailable", status=503)


@lru_cache(maxsize=4)
def _jwks_client(url: str) -> jwt.PyJWKClient:
    # The JWK SET is cached 1h; an unknown `kid` forces a refetch (at most once per cooldown window).
    # cache_keys stays False: that per-kid LRU never expires ⇒ a key the IdP removed (compromised/rotated
    # out) would stay trusted until restart. PyJWT ≥ 2.14 does not follow redirects ⇒ the URL must be final.
    return jwt.PyJWKClient(
        url,
        cache_keys=False,
        lifespan=3600,
        timeout=JWKS_TIMEOUT_S,
        cooldown_duration=JWKS_REFETCH_COOLDOWN_S,
    )


def _header(context: RequestContext, name: str) -> str | None:
    headers = context.request_headers or {}
    lowered = {k.lower(): v for k, v in headers.items()}
    return lowered.get(name.lower())


def _claimed_user_id(context: RequestContext) -> str | None:
    """The User-Id header the CALLER sent. From the HTTP request when there is one (/invocations): the SDK's
    `context.user_id` falls back to the `agent_user_id` config value when the header is absent — a config value
    is not a caller. In-process contexts (A2A auth backend, tests) set user_id from the request themselves."""
    headers = getattr(context.request, "headers", None)
    if headers is None:
        return context.user_id
    return {k.lower(): v for k, v in headers.items()}.get(USER_ID_HEADER.lower()) or None


def _extract_bearer(raw: str | None) -> str | None:
    if not raw:
        return None
    scheme, _, token = raw.partition(" ")
    return token.strip() if scheme.lower() == "bearer" and token else raw.strip()


def verify_jwt(token: str, settings: Settings) -> dict[str, Any]:
    try:
        key = _jwks_client(settings.auth_jwks_url).get_signing_key_from_jwt(token)
    except json.JSONDecodeError as e:
        # The IdP/proxy answered the JWKS URL with non-JSON (maintenance page…): an outage, not a bad token ⇒ 503
        # (a 401 would log every user out). PyJWT doesn't wrap this error.
        health.record("jwks", False, "JWKS answered with non-JSON")
        raise _idp_unavailable() from e
    except jwt.PyJWKClientError as e:
        # Only "no key for this kid" is the TOKEN's fault. Unreachable / redirected JWKS URL, `[]`, `{"keys": []}`,
        # no signing keys ⇒ the IdP side is broken ⇒ 503.
        if str(e).startswith("Unable to find a signing key that matches"):
            health.record("jwks", True)  # the JWKS itself was fetched fine
            raise _unauthorized("Invalid token: unknown signing key") from e
        health.record("jwks", False, "JWKS unreachable or unusable")
        raise _idp_unavailable() from e
    except jwt.PyJWKSetError as e:  # JWKS with no usable key
        health.record("jwks", False, "JWKS has no usable key")
        raise _idp_unavailable() from e
    except jwt.PyJWTError as e:  # malformed token (DecodeError)
        raise _unauthorized(f"Invalid token: {type(e).__name__}") from e
    except (TypeError, ValueError) as e:
        raise _unauthorized("Invalid token: unusable key or token") from e
    health.record("jwks", True)
    try:
        claims = jwt.decode(
            token,
            # The PyJWK (not key.key) binds the alg to the key — its JWKS `alg`, else RS256 for RSA / the
            # curve's ES* for EC ⇒ a header alg that doesn't fit the key (ES256 + RSA kid) is a 401, not a 500.
            key,
            algorithms=settings.auth_algorithms,
            audience=settings.auth_audience or None,
            issuer=settings.auth_issuer or None,
            options={"require": ["exp", "iat"], "verify_aud": bool(settings.auth_audience)},
            leeway=30,
        )
    except jwt.PyJWTError as e:
        raise _unauthorized(f"Invalid token: {type(e).__name__}") from e
    # malformed key/token that PyJWT doesn't wrap in PyJWTError ⇒ still 401, never an unauthenticated 500
    except (TypeError, ValueError) as e:
        raise _unauthorized("Invalid token: unusable key or token") from e
    # Only access tokens: reject the token types the IdP marks — Cognito `token_use` (id), Keycloak `typ`
    # (ID / Refresh / Offline / Logout). An IdP that marks neither relies on AUTH_AUDIENCE / AUTH_ALLOWED_CLIENT_IDS.
    if claims.get("token_use", "access") != "access":
        raise _unauthorized("Invalid token: not an access token")
    if str(claims.get("typ", "")).lower() in ("id", "refresh", "offline", "logout"):
        raise _unauthorized("Invalid token: not an access token")
    if settings.auth_allowed_client_ids:
        client = claims.get("azp") or claims.get("client_id")
        if client not in settings.auth_allowed_client_ids:
            raise _unauthorized("Invalid token: client not allowed")
    return claims


def _jwt_token(context: RequestContext, settings: Settings) -> str:
    token = _extract_bearer(_header(context, settings.auth_token_header))
    if not token:
        raise _unauthorized(f"Missing bearer token in header {settings.auth_token_header}")
    return token


def _principal(context: RequestContext, settings: Settings, claims: dict | None, token: str | None):
    claimed_user = _claimed_user_id(context)
    if settings.auth_mode == "none":
        principal = Principal(user_id=claimed_user or LOCAL_USER_ID)
    elif settings.auth_mode == "api_key":
        key = _header(context, settings.auth_api_key_header)
        if not key:
            raise _unauthorized(f"Missing API key in header {settings.auth_api_key_header}")
        digest = hashlib.sha256(key.strip().encode()).hexdigest()
        if not any(hmac.compare_digest(digest, h.lower()) for h in settings.auth_api_key_sha256):
            raise _unauthorized("Invalid API key")
        if not claimed_user:
            raise GreenNodeRequestError(
                "Header X-GreenNode-AgentBase-User-Id is required in api_key mode "
                "(memory is isolated per user)",
                status_code=400,
            )
        principal = Principal(user_id=claimed_user, claims={"auth": "api_key"})
    else:
        raw = (claims or {}).get(settings.auth_user_claim)
        if not raw:
            raise _unauthorized(f"Token missing claim '{settings.auth_user_claim}'")
        tenant = tenant_from_claims(claims or {}, settings)
        user_id = user_id_from_subject(str(raw), (claims or {}).get("iss"), tenant)
        accepted = (
            (user_id,) if tenant else (str(raw), user_id)
        )  # with tenants the raw sub is not an id
        if claimed_user and claimed_user not in accepted:
            raise _unauthorized("User-Id header does not match token subject", status=403)
        principal = Principal(
            user_id=user_id,
            token=token,
            claims=claims or {},
            tenant_id=tenant,
            roles=roles_from_claims(claims or {}, settings),
        )
    validate_user_id(principal.user_id)
    GreenNodeAgentBaseContext.set_user_id(principal.user_id)
    return principal


def authenticate(context: RequestContext, settings: Settings) -> Principal:
    """Sync version (tests, sync code). The server uses `authenticate_async` so the JWKS fetch doesn't block the loop."""
    if settings.auth_mode == "jwt":
        token = _jwt_token(context, settings)
        return _principal(context, settings, verify_jwt(token, settings), token)
    return _principal(context, settings, None, None)


async def authenticate_async(context: RequestContext, settings: Settings) -> Principal:
    """Used by /invocations AND /a2a. Runs before the request timeout starts ⇒ bounded here (AUTH_TIMEOUT_S)."""
    if settings.auth_mode == "jwt":
        token = _jwt_token(context, settings)
        try:
            async with asyncio.timeout(AUTH_TIMEOUT_S):  # the JWKS fetch is sync I/O ⇒ a thread
                claims = await asyncio.to_thread(verify_jwt, token, settings)
        except TimeoutError as e:
            raise _idp_unavailable() from e
        return _principal(context, settings, claims, token)
    return _principal(context, settings, None, None)


async def prefetch_jwks(settings: Settings) -> None:
    """Called at startup so the first request doesn't wait for the JWKS download (not blocking: requests will
    retry the fetch — but a failure here usually means a wrong AUTH_JWKS_URL, so it is logged as ERROR). Also what
    `GET /ready` calls to re-check a JWKS that failed (outcome ⇒ app.health)."""
    if settings.auth_mode == "jwt" and settings.auth_jwks_url:
        try:
            await asyncio.wait_for(
                asyncio.to_thread(_jwks_client(settings.auth_jwks_url).get_jwk_set), AUTH_TIMEOUT_S
            )
            health.record("jwks", True)
        except Exception:  # noqa: BLE001
            health.record("jwks", False, "JWKS prefetch failed")
            log.error(
                "JWKS prefetch from AUTH_JWKS_URL failed — every jwt request will get 503 until it works "
                "(the URL must be the final https URL: copy jwks_uri from the IdP's "
                "/.well-known/openid-configuration; redirects are not followed)",
                exc_info=True,
            )
