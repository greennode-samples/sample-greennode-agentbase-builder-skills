# The web BFF — routes, sessions, IdP registration, deployment

The Next.js app is both the web UI and the **Backend-for-Frontend** of pattern B in `/agentbase-build-auth`
(`references/patterns.md`). Route handlers run on the server (Node.js runtime); everything under `src/web/src/server/`
is imported only by them.

## Routes

| Route | Does | Notes |
|---|---|---|
| `GET /api/auth/login` | New `state`, `nonce`, PKCE verifier (32 random bytes each) sealed into the `ab_oidc` cookie (10 min) → 303 to the IdP's `authorization_endpoint` (`code_challenge_method=S256`, `audience` when set) | Discovery from `<BFF_OIDC_ISSUER>/.well-known/openid-configuration`, cached 1 h; its `issuer` must equal `BFF_OIDC_ISSUER` (a trailing `/` is ignored) |
| `GET /api/auth/callback` | Checks `state` (constant time) against the cookie, redeems the code with the verifier, checks the ID token's `iss` / `aud` / `nonce` / `exp`, writes the session cookie → 303 to `/` | Errors redirect to `/?auth_error=<code>` (fixed codes the sign-in screen explains — never text from the URL) |
| `GET /api/auth/session` | `{authenticated, mode, user: {id, name}, csrfToken}` | Never a token. `BFF_AUTH_MODE=none`: creates a `local-user` session |
| `POST /api/auth/logout` | CSRF check → session cookie cleared → refresh token revoked (RFC 7009, best effort) → `{redirect}` = `end_session_endpoint` + `id_token_hint` + `post_logout_redirect_uri`, else `/` | The browser navigates there; the IdP session ends, so the next sign-in asks for credentials |
| `POST /api/invocations` | Session + CSRF check → session id / body validation → fresh access token → forward → stream back | See below |

### `POST /api/invocations`

1. No/invalid session ⇒ **401**; CSRF violation ⇒ **403** (reason logged, not returned); bad
   `X-GreenNode-AgentBase-Session-Id` (`^[A-Za-z0-9][A-Za-z0-9-]{0,127}$`) or body (not a JSON object with `type`
   chat / resume / feedback, or > 64 KiB) ⇒ **400**. Error bodies use the agent's format `{error, error_type, details}`.
2. Access token expiring within 30 s ⇒ refresh (single-flight per refresh token in this process; rotated refresh
   tokens are stored back into the cookie).
3. Forward to `<BFF_AGENT_URL>/invocations` with only: `Content-Type`, `Accept` (JSON or SSE), the session header, and
   - `BFF_RUNTIME_AUTH=jwt`: `Authorization: Bearer <user access token>`;
   - `BFF_RUNTIME_AUTH=iam`: `Authorization: Bearer <IAM token>` (client_credentials at
     `https://iam.api.vngcloud.vn/accounts-api/v2/auth/token`, cached for 90 % of `expires_in`, single-flight) +
     `X-GreenNode-AgentBase-Custom-User-Token: Bearer <user access token>`;
   - `BFF_AUTH_MODE=none` (local): `X-GreenNode-AgentBase-User-Id: local-user`, no token.
   In jwt mode no User-Id header is sent — the agent takes the user from `sub`.
4. Agent **401** ⇒ refresh the user token (and the IAM token) once and retry — 401 is answered before the turn
   runs, so the retry cannot run it twice. Still 401 ⇒ session cookie cleared, 401 passed to the browser (sign in).
5. The agent's status and body are passed through **unchanged** (409 + `details.interrupt` included); SSE is streamed
   as it arrives: `Cache-Control: no-store, no-transform` (Next.js does not gzip it), `X-Accel-Buffering: no`.
   Browser Stop / tab close aborts the agent call (`request.signal`).
6. Refresh token rejected (`invalid_grant`) ⇒ 401 + cookie cleared. IdP / IAM / agent unreachable ⇒ **503** (the
   client retries it with backoff, as the contract says for 503).

## Session cookie

- Value: `seal({sub, name, csrf, accessToken, accessTokenExp, refreshToken, idToken})` = AES-256-GCM with a key
  derived by HKDF-SHA256 from `BFF_SESSION_SECRET`; the cookie name is the AAD (an `ab_oidc` value cannot be replayed
  as a session) and an expiry is sealed inside. Tampered / foreign / expired ⇒ treated as signed out.
- Attributes: `HttpOnly; Secure; SameSite=Lax; Path=/`, name prefix `__Host-` on https (plain `ab_session` on
  http://localhost). Max-Age `BFF_SESSION_MAX_AGE_S` (default 8 h) — the absolute session length; access tokens are
  refreshed inside it.
- Size: split into `…ab_session.0`, `.1`, … of 3 800 chars (max 6); stale chunks are deleted. Too large even then ⇒
  the ID token is dropped (sign-out then works without `id_token_hint`).
- Stateless: any replica with the same secret reads it. Rotating `BFF_SESSION_SECRET` signs everyone out.
- Single-flight refresh is per process: with several replicas and refresh-token rotation, two parallel requests on
  different replicas may both redeem the same refresh token — the IdP's reuse detection may then end the session.
  Use sticky sessions or a refresh-token reuse interval at the IdP (Keycloak *Revoke Refresh Token* off, Auth0
  *Rotation overlap period*).

## IdP registration (one web client)

| Setting | Value |
|---|---|
| Client type | Confidential (client secret, `BFF_OIDC_CLIENT_SECRET`) — the BFF is a server. A public client (PKCE only) also works |
| Grant | Authorization Code + PKCE S256, refresh tokens |
| Redirect URI | `<BFF_APP_ORIGIN>/api/auth/callback` — exact match (`http://localhost:3000/api/auth/callback` in dev) |
| Post-logout redirect URI | `<BFF_APP_ORIGIN>/` |
| Web origins / CORS | none needed — the browser never calls the IdP with `fetch` |
| Audience | the agent's audience (`AUTH_AUDIENCE` of the backend) must be in the **access token** — Keycloak *Audience* mapper, Auth0 `BFF_OIDC_AUDIENCE`, Entra scope `api://<api>/<scope>` in `BFF_OIDC_SCOPES` |

Client authentication at the token / revocation endpoint: `client_secret_basic` when the discovery lists it (or
lists nothing), else `client_secret_post`.

## Deployment

- Any Node.js host (container, VM, Vercel, …): `npm ci && npx next build && npx next start` (Node ≥ 20.9). Set every
  `BFF_*` in the host's secret store / environment and `NEXT_PUBLIC_*` at **build** time.
- Serve over **https** only (`BFF_APP_ORIGIN=https://…`). A reverse proxy in front must not buffer
  `/api/invocations` (nginx: `proxy_buffering off;` or honor `X-Accel-Buffering`) and must not compress
  `text/event-stream`; its read timeout must exceed the backend's `REQUEST_TIMEOUT_S`.
- Runtime with IAM inbound auth: the BFF's service account needs permission to invoke that runtime only; add the
  BFF's egress IPs to the Runtime's IP Access Control when they are fixed.
- The backend's `CORS_ALLOW_ORIGINS` stays empty for this app: browser → BFF is same-origin, BFF → agent is
  server-to-server.
- Rate limiting / quotas per user belong in the BFF (before step 3) or in the platform in front of it.
