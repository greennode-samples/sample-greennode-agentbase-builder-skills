"""Authenticate requests to the MCP server (Resource Server per the MCP Authorization / OAuth 2.1 spec).

Who calls the MCP server? In the AgentBase standard it is the **MCP Gateway** (connector outbound auth):

| Connector outbound auth | Gateway sends to server              | MCP_AUTH_MODE on server |
|-------------------------|--------------------------------------|-------------------------|
| API Key 2LO             | `Authorization: Bearer <api key>`    | api_key                 |
| OAuth 2LO (M2M)         | `Bearer <client access token>`       | jwt (or introspect)     |
| OAuth 3LO               | `Bearer <USER access token>`         | jwt — has user `sub`    |
| Inbound forward         | the same JWT agent/user sent to GW   | jwt (same IdP) — JWT    |
|                         |                                      | inbound only, see below |
| No authorization        | —                                    | none (MCP_APP_ENV=local |
|                         |                                      | only, binds 127.0.0.1)  |

Inbound forward hands the MCP server the agent's own inbound credential. With gateway inbound IAM that is
the runtime service account's platform token (AgentBaseFullAccess) ⇒ never use it with IAM inbound; with
JWT inbound, only towards servers you own that validate the same IdP (see references/oauth.md §3).

Set Header key = `Authorization`, Header value prefix = `Bearer ` when configuring the connector.

The server NEVER trusts a `user_id` tool parameter: end-user identity comes from the verified token
(`AccessToken.subject`) via `get_access_token()` — see server.py.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
from functools import lru_cache

import jwt
from mcp.server.auth.provider import AccessToken, TokenVerifier

from settings import Settings


class ApiKeyVerifier(TokenVerifier):
    """Static API key (Gateway outbound API Key 2LO). Env holds only the key's SHA-256.

    The key carries no scopes: the caller is granted `scopes` (MCP_REQUIRED_SCOPES + MCP_API_KEY_SCOPES).
    """

    def __init__(self, sha256_hashes: list[str], scopes: list[str]):
        self._hashes = [h.lower() for h in sha256_hashes]
        self._scopes = scopes

    async def verify_token(self, token: str) -> AccessToken | None:
        digest = hashlib.sha256(token.strip().encode()).hexdigest()
        if not any(hmac.compare_digest(digest, h) for h in self._hashes):
            return None
        # No end-user ⇒ subject=None: tools needing per-user data must refuse (see server.py)
        return AccessToken(token=token, client_id="api-key", scopes=self._scopes, subject=None)


@lru_cache(maxsize=4)
def _jwks(url: str) -> jwt.PyJWKClient:
    # The JWK SET is cached 1h; an unknown `kid` forces a refetch (PyJWT rate-limits it). cache_keys stays
    # False: that per-kid LRU never expires ⇒ a key the IdP removed (rotated out / compromised) would stay
    # trusted until restart.
    return jwt.PyJWKClient(url, cache_keys=False, lifespan=3600)


class JwtVerifier(TokenVerifier):
    """OAuth 2.0 access token as JWT (OIDC IdP: Keycloak, Auth0, Entra, Google...).

    Always checks signature, `exp`, `iss` and `aud` (= MCP_AUDIENCE, RFC 8707): a token minted for
    another API of the same IdP, or with no `aud`, is rejected (401).
    """

    def __init__(self, settings: Settings):
        self.s = settings

    async def verify_token(self, token: str) -> AccessToken | None:
        s = self.s
        if not (s.audience and s.issuer):  # Settings requires both; fail closed if bypassed
            return None
        try:
            # A JWKS cache miss is a blocking HTTP fetch (up to PyJWT's 30s timeout): run it in a thread so
            # it never stalls the event loop (every other request and /health). Decoding stays in-loop.
            key = await asyncio.to_thread(_jwks(s.jwks_url).get_signing_key_from_jwt, token)
            claims = jwt.decode(
                token,
                # The PyJWK (not key.key) binds the alg to the key (JWKS `alg`, else RS256 for RSA / the
                # curve's ES* for EC) ⇒ a header alg that doesn't fit the key is rejected, not a crash.
                key,
                algorithms=s.jwt_algorithms,
                issuer=s.issuer,
                audience=s.audience,  # always verified (RFC 8707)
                options={"require": ["exp", "iss", "aud"]},
                leeway=30,
            )
        # TypeError/ValueError: malformed key or token PyJWT doesn't wrap ⇒ still 401, never an
        # unauthenticated 500.
        except (jwt.PyJWTError, TypeError, ValueError):
            return None
        scope = claims.get("scope") or claims.get("scp") or []
        scopes = scope.split() if isinstance(scope, str) else list(scope)
        return AccessToken(
            token=token,
            client_id=str(claims.get("azp") or claims.get("client_id") or ""),
            scopes=scopes,
            expires_at=claims.get("exp"),
            resource=s.audience,  # verified above
            subject=claims.get(s.user_claim),
        )


def build_verifier(settings: Settings) -> TokenVerifier | None:
    if settings.auth_mode == "api_key":
        scopes = list(dict.fromkeys(settings.required_scopes + settings.api_key_scopes))
        return ApiKeyVerifier(settings.api_key_sha256, scopes)
    if settings.auth_mode == "jwt":
        return JwtVerifier(settings)
    return None  # none: no auth — MCP_APP_ENV=local only, bound to 127.0.0.1 (see settings.py)
