# Agent calling & authentication patterns

## A. Direct — app calls the runtime endpoint directly

```
Mobile app ──(Authorization: Bearer <user JWT>)──▶ AgentBase Runtime endpoint ──▶ agent verifies JWT (JWKS)
```

- Backend: `AUTH_MODE=jwt`, `AUTH_TOKEN_HEADER=Authorization`.
- Frontend: `EXPO_PUBLIC_AUTH_TOKEN_HEADER=Authorization`.
- Pros: simple, fewer hops. Cons: no dedicated rate-limit layer.
- Web (Expo web): set `CORS_ALLOW_ORIGINS='["https://app.example.com"]'`.
- Verify with **both**: no token ⇒ 401, **a valid user token ⇒ 200** (a misconfiguration that rejects every token also passes the first check).

## B. BFF — Backend-for-Frontend holds IAM

```
Mobile app ──(Bearer <user JWT>)──▶ BFF (verify JWT, rate-limit)
                                     └─(Authorization: Bearer <IAM token>,
                                        X-GreenNode-AgentBase-Custom-User-Token: Bearer <user JWT>,
                                        X-GreenNode-AgentBase-User-Id: <sub>)──▶ Runtime endpoint ──▶ agent
```

- The app **always** sends `Authorization: Bearer <user JWT>` to the BFF (the frontend doesn't know whether a BFF sits in between).
- Agent backend: `AUTH_TOKEN_HEADER=X-GreenNode-AgentBase-Custom-User-Token` (the SDK only forwards `Authorization` and headers prefixed `X-GreenNode-AgentBase-Custom-` — `Settings` refuses any other header outside local). The agent still verifies the user JWT ⇒ defense in depth.
- BFF gets an IAM token via client_credentials at `https://iam.api.vngcloud.vn/accounts-api/v2/auth/token`, cached per `expires_in`.
- The BFF keeps the `/invocations` contract **unchanged** (`/agentbase-build` → `references/api-contract.md`): forwards `X-GreenNode-AgentBase-Session-Id`, passes error bodies through as is — including **409 with `details.interrupt`** (the app re-shows the approval card from it) — and proxies SSE verbatim and unbuffered (`X-Accel-Buffering: no`, no response compression/aggregation).
- BFF with the agent in `AUTH_MODE=api_key` instead (no user JWT forwarded): send `X-GreenNode-AgentBase-User-Id: user_id_from_subject(sub, iss)` (`app.auth.inbound`) from the token the BFF verified — the id jwt mode would derive, so users keep their memory if a client later calls the agent directly.

## C. Service-to-service (no end-user)

- Service calls with the IdP's client-credentials JWT (claim `sub` = client id) ⇒ `user_id` = service; or use pattern B with a fixed user_id for the job.
- In-process internal jobs: `service.run_chat()` (skips inbound auth) — eval/batch only.

## Suggested IdP configuration

Always copy `issuer` and `jwks_uri` **verbatim** from `<issuer>/.well-known/openid-configuration` (paths differ per IdP; the JWKS URL must be the final `https://` URL — PyJWT ≥ 2.14 does not follow redirects). `AUTH_USER_CLAIM` must be immutable (never `email`/`upn`/`preferred_username`).

| IdP | JWKS URL (from discovery) | Notes |
|---|---|---|
| Keycloak | `https://<host>/realms/<realm>/protocol/openid-connect/certs` | issuer = `https://<host>/realms/<realm>` (the host the frontend sees, including behind a reverse proxy). Access token defaults to `aud=account` ⇒ add an *Audience* mapper (client scope) pointing to the agent's client, `AUTH_AUDIENCE=<that client>`. Frontend: public client, *Standard flow* + PKCE S256, *Valid redirect URIs* `<scheme>://auth` and `http://localhost:8081/auth` (web — the full path, not just the origin); *Valid post logout redirect URIs* for the app's end-session sign-out (`+` = same as the redirect URIs). `sub` is a UUID ⇒ valid as `user_id` directly. ID tokens (`typ: ID`) are rejected |
| Auth0 | `https://<tenant>/.well-known/jwks.json` | `AUTH_ISSUER=https://<tenant>/` **with the trailing slash** (it is in `iss`; without it every token is a 401). Use the API's `audience`; frontend passes `audience` |
| Cognito | `https://cognito-idp.<region>.amazonaws.com/<pool>/.well-known/jwks.json` | access token has no `aud` ⇒ leave `AUTH_AUDIENCE` empty, set `AUTH_ALLOW_NO_AUDIENCE=true` **and** `AUTH_ALLOWED_CLIENT_IDS=["<app client id>"]` (matched against the `client_id` claim; required outside local). ID tokens (`token_use=id`) are rejected |
| Azure AD / Entra | `https://login.microsoftonline.com/<tenant>/discovery/v2.0/keys` | `AUTH_ISSUER=https://login.microsoftonline.com/<tenant>/v2.0`. In the API app registration's manifest set **`accessTokenAcceptedVersion: 2`** (Microsoft Graph manifest format: `api.requestedAccessTokenVersion: 2`): v1 access tokens carry `iss=https://sts.windows.net/<tenant>/` (⇒ 401) and `appid` instead of `azp` (⇒ `AUTH_ALLOWED_CLIENT_IDS` never matches). `AUTH_AUDIENCE` = the API's application ID URI / client id as it appears in `aud`. `AUTH_USER_CLAIM=sub` or `oid` (the tenant is pinned by the issuer) |
| GreenNode IAM | — | publishes no JWKS ⇒ cannot be the agent's inbound IdP (use it only as the Runtime's IAM inbound auth + BFF, pattern B) |
