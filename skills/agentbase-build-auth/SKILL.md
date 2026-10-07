---
name: agentbase-build-auth
description: "Authentication standard for AI agents on GreenNode AgentBase: inbound auth (verify OIDC/JWKS JWTs from frontend/service, user_id = sub, prevent User-Id header spoofing, AUTH_MODE none only in local, client allowlist for IdPs without aud), api_key mode for trusted callers, Runtime inbound auth (IAM / JWT / IP access control), runtime IAM, and the Direct vs BFF pattern. Use when adding login to an agent, protecting an endpoint, or configuring an IdP (Keycloak, Auth0, Cognito, VNG IAM). Trigger: authen, auth, authentication, JWT, OIDC, login, protect agent, token, inbound auth, BFF, xác thực, đăng nhập, bảo vệ agent. DO NOT use for calling external services with API keys/OAuth from tools (use /agentbase-build-identity), creating identities/providers on the platform (use /agentbase-identity), or creating IAM service accounts (/agentbase)."
---

# Auth for AgentBase agents

## 3 identity layers

| Layer | Who | Mechanism | Where |
|---|---|---|---|
| **Inbound** | End-user / service calling the agent | JWT (OIDC) verified via JWKS | `app/auth/inbound.py` (asset) |
| **Agent → platform** | The agent itself (Memory, Identity, Gateway IAM) | IAM service account injected by the runtime (`GREENNODE_CLIENT_ID/SECRET`) | SDK uses it automatically; local: `.greennode.json` |
| **Outbound** | Agent → external services | AgentBase Identity: API key / OAuth2 (M2M, 3LO) / delegated key | SDK decorator in the tool |

## Inbound — workflow

1. Ask the user for the IdP and its details: issuer, JWKS URL (usually `<issuer>/.well-known/jwks.json` or from discovery), audience, identity claim (default `sub`).
2. Env (`.env.<env>`): `AUTH_MODE=jwt`, `AUTH_JWKS_URL`, `AUTH_ISSUER`, `AUTH_AUDIENCE`, `AUTH_USER_CLAIM`, `AUTH_TOKEN_HEADER`. Outside `local`, missing `AUTH_ISSUER`/`AUTH_AUDIENCE` ⇒ app refuses to start (IdP without `aud`, like Cognito ⇒ `AUTH_ALLOW_NO_AUDIENCE=true` **plus** `AUTH_ALLOWED_CLIENT_IDS=["<app client id>"]`, required outside local — checked against `azp`/`client_id`; tokens with `token_use` ≠ `access` (ID tokens) are always rejected). `AUTH_FORWARD_CLAIMS` = claims passed to tools via `RunnableConfig` (default empty). `sub` containing characters outside `[A-Za-z0-9._@+=-]` ⇒ `user_id = u-<sha256(iss|sub)>` (stable, no traversal). JWKS is prefetched at startup; IdP unreachable ⇒ 503 (not 401).
3. Frontend: OIDC Authorization Code + PKCE (public client, no client secret) — `/agentbase-build-frontend`.
4. Test: `tests/test_auth.py` (valid token, missing token, expired, wrong audience, spoofed user header, client allowlist / ID token without `aud`, `APP_ENV=local` refused on the Runtime).

`authenticate()` behavior:
- Missing/invalid token ⇒ 401; `X-GreenNode-AgentBase-User-Id` differs from `sub` ⇒ 403.
- `Principal(user_id, token, claims)`; overrides `GreenNodeAgentBaseContext.set_user_id()` so Identity decorators (USER_FEDERATION) use the correct user.
- `AUTH_MODE=none` is blocked by `Settings` when `APP_ENV != local`.
- JWKS cached 1h (`PyJWKClient`), 30s leeway, `exp` and `iat` required.

Role-based access (RBAC): read `principal.claims` (e.g. `roles`, `groups`) in `service`/tools to reject early; per-tool permissions at the Gateway layer use a Policy Group (`/agentbase-policy`, principal `jwt:<sub>` or `principal.<claim>`).

## AgentBase Runtime endpoint security

The Runtime has **Security Settings** ([create-runtime](https://docs.greennode.ai/ai-stack/agent-base/agent-runtime/create-runtime)): **IP Access Control** (allowed source CIDRs) and **Inbound Auth type** — `IAM Permissions` (GreenNode IAM token), `JSON Web Tokens` (Discovery URL or inline JWKS), or `No authorization` (public: anyone with the URL reaches the container — what we observed on runtimes created before this setting, 2026-10).

- The agent **still authenticates itself** (`jwt`/`api_key`) whatever the Runtime setting — defense in depth, and `Settings` blocks `AUTH_MODE=none` outside local.
- Runtime `JWT` with the same IdP as `AUTH_*` ⇒ bad tokens are rejected before the container (cheaper), and pattern **A. Direct** works.
- Runtime `IAM Permissions` ⇒ a mobile/web app cannot call it directly ⇒ pattern **B. BFF**.
- Add IP Access Control for server-to-server callers (BFF, other agents) when their egress IPs are fixed.
- GreenNode IAM does not publish a JWKS (404) ⇒ IAM tokens cannot be used as the agent's inbound JWTs.

### `AUTH_MODE=api_key` (trusted callers: server, BFF, job, test)

- Generate a key: `uv run python -c "import secrets,hashlib;k=secrets.token_urlsafe(32);print(k);print(hashlib.sha256(k.encode()).hexdigest())"`.
- Env holds only the hash: `AUTH_API_KEY_SHA256=["<sha256>"]` (multiple hashes ⇒ zero-downtime rotation). Store the raw key outside the repo/in a secret manager.
- Caller sends `X-GreenNode-AgentBase-Custom-Api-Key: <key>` + **mandatory** `X-GreenNode-AgentBase-User-Id` (no shared bucket — missing ⇒ 400; the trusted caller is responsible for this value).
- **Never** embed the key in a mobile/web app ⇒ user-facing apps use `jwt` or go through a BFF.

## Calling the runtime endpoint: 2 patterns

Read `references/patterns.md`. Summary:
- **A. Direct (default):** app → runtime endpoint with the user JWT in `Authorization`. Use when the endpoint does not require IAM in `Authorization`.
- **B. BFF:** app → your BFF (verifies JWT) → runtime endpoint with an IAM token; the user JWT goes in a custom header (`AUTH_TOKEN_HEADER=X-GreenNode-AgentBase-Custom-User-Token`). Use when the endpoint requires IAM, or you need your own rate-limiting/billing. **Never** embed IAM credentials in a mobile app.

Choose the pattern from the Runtime's **Inbound Auth type** (above); verify by calling `<endpoint>/health` and `/invocations` without a token.

## Outbound — calling external services

Moved to **`/agentbase-build-identity`** (Access Control): Static / Delegated API Key and OAuth2 providers, `app/identity.py` helpers (`agent_api_key`, `agent_access_token`, `user_access_token`, `user_api_key`), non-blocking 3LO consent links, per-user isolation, tests. MCP via Gateway: credentials live in the connector's outbound auth (`/agentbase-build-mcp`). **Do not** let the user paste secrets into chat.

## Per-user isolation (verified on runtime)

- The only `user_id` source is `Principal` (JWT `sub` / api_key caller's header) ⇒ `validate_user_id()` (`^[A-Za-z0-9][A-Za-z0-9._@+=-]{0,127}$`, `..` forbidden) prevents namespace injection since user_id is part of the memory path.
- Applies to: checkpointer (session, user), LTM namespace, HITL resume (different user ⇒ 409), feedback (`feedback_token` HMAC of user+trace ⇒ different user 403), A2A task store (owner = user), MCP server (`current_user()` from token), A2A client (passes user to the target agent).
- `tests/test_isolation.py` — rerun whenever any data path changes.

## Local IAM credential trap (seen in practice)

- SDK `IAMCredentials` falls back **per field**: env var first, then `.greennode.json`. If the shell accidentally exports another account's `GREENNODE_CLIENT_ID` ⇒ wrong ID + correct secret ⇒ IAM **401** for Memory/Identity/Gateway (`get_token.sh` is unaffected because it reads the pair together).
- Standard: in local dev, put IAM **only** in `.greennode.json`; do not put `GREENNODE_CLIENT_*` in `.env`; check `env | grep GREENNODE_`. `app/config.py::normalize_iam_env()` drops orphan variables (only ID or only SECRET) and logs a WARNING.
- The agent's principal when calling the MCP Gateway (inbound IAM) = `iam:<sub>` of the IAM token (the service account's user id), **not** the client_id — the agent logs this value when fetching the token.

## Forbidden

- Trusting `user_id` from the body/tool arguments/LLM.
- Logging/tracing tokens (already masked in tracing; do not `print` headers).
- Hardcoding secrets or committing `.env`/`.greennode.json`.
- Putting `GREENNODE_CLIENT_*` in deploy env files (the runtime injects them).

## Official docs

- [create-runtime](https://docs.greennode.ai/ai-stack/agent-base/agent-runtime/create-runtime) — Runtime Security Settings: Inbound Auth (IAM / JWT / None) and IP Access Control
- [access-control](https://docs.greennode.ai/ai-stack/agent-base/access-control) — agent identities, API key / OAuth2 providers, 3LO `allowedReturnUrls`, `requires_api_key` / `requires_access_token`
- [manage-service-accounts](https://docs.greennode.ai/ai-stack/agent-base/team-permissions/manage-service-accounts) — auto-created runtime service account, client secret shown once
