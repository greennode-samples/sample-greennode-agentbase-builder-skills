# Agent calling & authentication patterns

## A. Direct — app calls the runtime endpoint directly

```
Mobile app ──(Authorization: Bearer <user JWT>)──▶ AgentBase Runtime endpoint ──▶ agent verifies JWT (JWKS)
```

- Backend: `AUTH_MODE=jwt`, `AUTH_TOKEN_HEADER=Authorization`.
- Frontend: `EXPO_PUBLIC_AUTH_TOKEN_HEADER=Authorization`.
- Pros: simple, fewer hops. Cons: no dedicated rate-limit layer.
- Web (Expo web): set `CORS_ALLOW_ORIGINS='["https://app.example.com"]'`.

## B. BFF — Backend-for-Frontend holds IAM

```
Mobile app ──(Bearer <user JWT>)──▶ BFF (verify JWT, rate-limit)
                                     └─(Authorization: Bearer <IAM token>,
                                        X-GreenNode-AgentBase-Custom-User-Token: Bearer <user JWT>,
                                        X-GreenNode-AgentBase-User-Id: <sub>)──▶ Runtime endpoint ──▶ agent
```

- Agent backend: `AUTH_TOKEN_HEADER=X-GreenNode-AgentBase-Custom-User-Token` (the SDK only forwards `Authorization` and headers prefixed `X-GreenNode-AgentBase-Custom-`). The agent still verifies the user JWT ⇒ defense in depth.
- BFF gets an IAM token via client_credentials at `https://iam.api.vngcloud.vn/accounts-api/v2/auth/token`, cached per `expires_in`.
- BFF SSE streaming: proxy verbatim, disable buffering (`X-Accel-Buffering: no`).

## C. Service-to-service (no end-user)

- Service calls with the IdP's client-credentials JWT (claim `sub` = client id) ⇒ `user_id` = service; or use pattern B with a fixed user_id for the job.
- In-process internal jobs: `service.run_chat()` (skips inbound auth) — eval/batch only.

## Suggested IdP configuration

| IdP | JWKS URL | Notes |
|---|---|---|
| Keycloak | `https://<host>/realms/<realm>/protocol/openid-connect/certs` | issuer = `https://<host>/realms/<realm>` (the host the frontend sees, including behind a reverse proxy). Access token defaults to `aud=account` ⇒ add an *Audience* mapper (client scope) pointing to the agent's client, `AUTH_AUDIENCE=<that client>`. Frontend: public client, *Standard flow* + PKCE S256, redirect `<scheme>://auth` (and `http://localhost:8081` for web). `sub` is a UUID ⇒ valid as `user_id` directly |
| Auth0 | `https://<tenant>/.well-known/jwks.json` | use the API's `audience`; frontend passes `audience` |
| Cognito | `https://cognito-idp.<region>.amazonaws.com/<pool>/.well-known/jwks.json` | access token has no `aud` ⇒ leave `AUTH_AUDIENCE` empty, check `client_id` via claims |
| Azure AD / Entra | `https://login.microsoftonline.com/<tenant>/discovery/v2.0/keys` | issuer v2.0 |
