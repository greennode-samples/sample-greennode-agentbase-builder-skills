---
name: agentbase-build-auth
description: "Authentication standard for AI agents on GreenNode AgentBase: inbound auth (verify OIDC/JWKS JWTs from frontend/service, user_id = sub, prevent User-Id header spoofing, AUTH_MODE none only in local, client allowlist for IdPs without aud), api_key mode for trusted callers, Runtime inbound auth (IAM / JWT / IP access control), runtime IAM, and the Direct vs BFF pattern. Use when adding login to an agent, protecting an endpoint, or configuring an IdP (Keycloak, Auth0, Cognito, Entra ID). Trigger: authen, auth, authentication, JWT, OIDC, login, protect agent, token, inbound auth, BFF, xác thực, đăng nhập, bảo vệ agent. DO NOT use for calling external services with API keys/OAuth from tools (use /agentbase-build-identity), creating identities/providers on the platform (use /agentbase-identity), or creating IAM service accounts (/agentbase)."
---

# Auth for AgentBase agents

## 3 identity layers

| Layer | Who | Mechanism | Where |
|---|---|---|---|
| **Inbound** | End-user / service calling the agent | JWT (OIDC) verified via JWKS | `app/auth/inbound.py` (asset) |
| **Agent → platform** | The agent itself (Memory, Identity, Gateway IAM) | IAM service account injected by the runtime (`GREENNODE_CLIENT_ID/SECRET`) | SDK uses it automatically; local: `.greennode.json` |
| **Outbound** | Agent → external services | AgentBase Identity: API key / OAuth2 (M2M, 3LO) / delegated key | SDK decorator in the tool |

## Inbound — workflow

1. Ask the user for the IdP and its details: issuer, JWKS URL, audience, identity claim (default `sub`). Copy `issuer` and `jwks_uri` **verbatim from `<issuer>/.well-known/openid-configuration`** — the JWKS path differs per IdP (Keycloak `/protocol/openid-connect/certs`, Entra `/discovery/v2.0/keys`…), the issuer must match the token's `iss` byte for byte (Auth0 ends with `/`), and PyJWT ≥ 2.14 does **not** follow redirects (a redirected JWKS URL = 503 forever). Outside local both must be `https://` (enforced).
   - **`AUTH_USER_CLAIM` must be immutable and never reassigned** — it becomes the memory/identity key. `sub` (Keycloak/Auth0/Cognito); Entra: `sub` or `oid` (tenant pinned by `AUTH_ISSUER`). **Never `email` / `upn` / `preferred_username`**: they can be changed or recycled ⇒ a new person inherits someone else's memory and connected accounts.
2. Env (`.env.<env>`): `AUTH_MODE=jwt`, `AUTH_JWKS_URL`, `AUTH_ISSUER`, `AUTH_AUDIENCE`, `AUTH_USER_CLAIM`, `AUTH_TOKEN_HEADER`. Outside `local`, missing `AUTH_ISSUER`/`AUTH_AUDIENCE` ⇒ app refuses to start (IdP without `aud`, like Cognito ⇒ `AUTH_ALLOW_NO_AUDIENCE=true` **plus** `AUTH_ALLOWED_CLIENT_IDS=["<app client id>"]`, required outside local — checked against `azp`/`client_id`). ID/refresh tokens are rejected **when the IdP marks them**: `token_use` ≠ `access` (Cognito) or `typ` = `ID`/`Refresh`/`Offline`/`Logout` (Keycloak); IdPs that mark neither (Auth0, Entra…) rely on `AUTH_AUDIENCE` (an ID token's `aud` is the app's client id, not the API) or `AUTH_ALLOWED_CLIENT_IDS`. `AUTH_FORWARD_CLAIMS` = claims passed to tools via `RunnableConfig` (default empty). `sub` containing characters outside `[A-Za-z0-9._@+=-]` — or already shaped like a hashed id `u-<40 hex>` (no collision with another user) — ⇒ `user_id = u-<sha256(iss|sub)>` (stable, no traversal). JWKS is prefetched at startup (failure ⇒ ERROR log: usually a wrong URL); IdP unreachable, slow, redirected or serving no usable key (`[]`, `{"keys": []}`, HTML) ⇒ **503** (not 401, which would log everyone out) — only a token whose `kid` matches no key is a 401. Bounded: 5 s per JWKS fetch, 10 s for the whole authentication (`JWKS_TIMEOUT_S` / `AUTH_TIMEOUT_S` in `inbound.py`; PyJWKClient fetches under one process-wide lock, so an unbounded IdP outage would queue every request).
3. Frontend: OIDC Authorization Code + PKCE (public client, no client secret) — `/agentbase-build-frontend`.
4. Test: `tests/test_auth.py` — against a **real local JWKS endpoint** (kid lookup, key type, rotation): valid token (RSA + EC keys), missing token, expired / wrong audience / wrong or missing `iss` / missing `exp` / future `nbf` ⇒ 401, `alg: none` and an attacker's RS256 key reusing the IdP's `kid` ⇒ 401, 50 random-`kid` tokens ⇒ ≤ 1 JWKS fetch (refetch cooldown), spoofed user header ⇒ 403, `alg` not matching the key ⇒ 401, key removed from the JWKS ⇒ rejected once the 1h cache expires, JWKS down / slow / empty ⇒ 503, client allowlist / ID token (`token_use`, Keycloak `typ`), hashed-id collision, unsafe settings refused (http URLs, HS*/none, non-forwarded headers), `Principal` repr without the token, caller User-Id taken from the request, `APP_ENV=local` refused on the Runtime.

`authenticate()` behavior:
- Missing/invalid token ⇒ 401; `X-GreenNode-AgentBase-User-Id` differs from `sub` ⇒ 403.
- The caller's `X-GreenNode-AgentBase-User-Id` is read from the **HTTP request**, never from `context.user_id`: the SDK fills that from `agent_user_id` in `.greennode.json`/env when the header is absent (a config value is not a caller — it used to satisfy api_key mode's mandatory User-Id).
- `Principal(user_id, token, claims)` — `repr` hides `token` and `claims`; overrides `GreenNodeAgentBaseContext.set_user_id()` so Identity decorators (USER_FEDERATION) use the correct user.
- `AUTH_MODE=none` is blocked by `Settings` when `APP_ENV != local`.
- JWK **set** cached 1h (`PyJWKClient`, `cache_keys=False` — its per-kid cache never expires, so a key the IdP removed would stay trusted until restart); a removed key stops working when the set is refetched (≤ 1h), an unknown `kid` triggers a refetch (at most every 30s, PyJWT cooldown). 30s leeway, `exp` and `iat` required.
- The token's `alg` must be in `AUTH_ALGORITHMS` **and** match its JWKS key (the key's `alg`, else RS256 for RSA / ES* by curve) — mismatch ⇒ 401. An IdP signing RS384/RS512/PS* whose JWKS omits `alg` therefore needs the `alg` published. `AUTH_ALGORITHMS` accepts asymmetric algorithms only (RS*/PS*/ES*/EdDSA — `Settings` refuses HS*/none: the JWKS is public).
- Outside local, `Settings` also refuses an `AUTH_TOKEN_HEADER` / `AUTH_API_KEY_HEADER` other than `Authorization` or `X-GreenNode-AgentBase-Custom-*` — the SDK forwards no other header, so every request would be a 401.

Role-based access (RBAC): put `roles`/`groups` in `AUTH_FORWARD_CLAIMS` and check them **in the tools** (`config["configurable"]["user_claims"]`) or in `service._Run._prepare` — the path shared by `/invocations`, `/a2a` (which gets the same verified claims + token) and `run_chat`. A check only in `service.handle` is bypassed by A2A, which never goes through `handle`. Per-tool permissions at the Gateway layer use a Policy Group (`/agentbase-policy`, principal `jwt:<sub>` or `principal.<claim>`).

## AgentBase Runtime endpoint security

The Runtime has **Security Settings** ([create-runtime](https://docs.greennode.ai/ai-stack/agent-base/agent-runtime/create-runtime)): **IP Access Control** (allowed source CIDRs) and **Inbound Auth type** — `IAM Permissions` (GreenNode IAM token), `JSON Web Tokens` (Discovery URL or inline JWKS), or `No authorization` (public: anyone with the URL reaches the container — what we observed on runtimes created before this setting, 2026-10).

- The agent **still authenticates itself** (`jwt`/`api_key`) whatever the Runtime setting — defense in depth, and `Settings` blocks `AUTH_MODE=none` outside local.
- Runtime `JWT` with the same IdP as `AUTH_*` ⇒ bad tokens are rejected before the container (cheaper), and pattern **A. Direct** works. It validates the **key source** (discovery/JWKS) only — it **never replaces `AUTH_AUDIENCE` / `AUTH_ALLOWED_CLIENT_IDS`**: a token the same IdP issued to another app passes it, the agent's own checks stop it.
- Runtime `IAM Permissions` ⇒ a mobile/web app cannot call it directly ⇒ pattern **B. BFF**.
- Add IP Access Control for server-to-server callers (BFF, other agents) when their egress IPs are fixed.
- GreenNode IAM does not publish a JWKS (404) ⇒ IAM tokens cannot be used as the agent's inbound JWTs.

### `AUTH_MODE=api_key` (trusted callers: server, BFF, job, test)

- Generate a key **without printing it** (it would land in the terminal scrollback / the coding agent's context): the raw key goes to a `0600` file outside the repo, only the hash is printed — or let the user run it:
  `uv run python -c "import secrets,hashlib,os,pathlib;k=secrets.token_urlsafe(32);p=pathlib.Path.home()/'.my-agent-api-key';os.write(os.open(p,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),k.encode());print(hashlib.sha256(k.encode()).hexdigest())"` — then the user moves the key into the caller's secret store and deletes the file.
- Env holds only the hash: `AUTH_API_KEY_SHA256=["<sha256>"]` (multiple hashes ⇒ zero-downtime rotation).
- Caller sends `X-GreenNode-AgentBase-Custom-Api-Key: <key>` + **mandatory** `X-GreenNode-AgentBase-User-Id` (no shared bucket — missing ⇒ 400; the trusted caller is responsible for this value). **An api_key holder can act as ANY user** (memory, sessions, and connected accounts if `IDENTITY_TRUST_ASSERTED_USER=true`).
- A BFF that verified the user's IdP token sends `User-Id = user_id_from_subject(sub, iss)` (`app.auth.inbound`) — the same id jwt mode derives, so switching a client between BFF and direct jwt keeps the user's memory.
- **Never** embed the key in a mobile/web app ⇒ user-facing apps use `jwt` or go through a BFF.

## Calling the runtime endpoint: 2 patterns

Read `references/patterns.md`. Summary:
- **A. Direct (default):** app → runtime endpoint with the user JWT in `Authorization`. Use when the endpoint does not require IAM in `Authorization`.
- **B. BFF:** app → your BFF (verifies JWT) → runtime endpoint with an IAM token; the user JWT goes in a custom header (`AUTH_TOKEN_HEADER=X-GreenNode-AgentBase-Custom-User-Token`). Use when the endpoint requires IAM, or you need your own rate-limiting/billing. **Never** embed IAM credentials in a mobile app.

Choose the pattern from the Runtime's **Inbound Auth type** (above); verify: `/invocations` **without** a token ⇒ 401 **and with a valid user token ⇒ 200** (a setup that rejects everyone also passes the first check).

## Outbound — calling external services

Moved to **`/agentbase-build-identity`** (Access Control): Static / Delegated API Key and OAuth2 providers, `app/identity.py` helpers (`agent_api_key`, `agent_access_token`, `user_access_token`, `user_api_key`), non-blocking 3LO consent links, per-user isolation, tests. MCP via Gateway: credentials live in the connector's outbound auth (`/agentbase-build-mcp`). **Do not** let the user paste secrets into chat.

## Per-user isolation (verified on runtime)

- The only `user_id` source is `Principal` (JWT `sub` / api_key caller's header) ⇒ `validate_user_id()` (`^[A-Za-z0-9][A-Za-z0-9._@+=-]{0,127}$`, `..` forbidden) prevents namespace injection since user_id is part of the memory path.
- Applies to: checkpointer (session, user), LTM namespace, HITL resume (different user ⇒ 409), feedback (`feedback_token` HMAC of user+trace ⇒ different user 403), A2A task store (owner = user), MCP server (`current_user()` from token), A2A client (passes user to the target agent).
- `tests/test_isolation.py` — rerun whenever any data path changes.

## Local IAM credential trap (seen in practice)

- SDK `IAMCredentials` falls back **per field**: env var first, then `.greennode.json`. If the shell accidentally exports another account's `GREENNODE_CLIENT_ID` ⇒ wrong ID + correct secret ⇒ IAM **401** for Memory/Identity/Gateway (`get_token.sh` is unaffected because it reads the pair together).
- Standard: in local dev, put IAM **only** in `.greennode.json`; do not put `GREENNODE_CLIENT_*` in `.env`; check `env | grep GREENNODE_`. `app/config.py::normalize_iam_env()` drops orphan variables (only ID or only SECRET) and logs a WARNING.
- The agent's principal when calling the MCP Gateway (inbound IAM) = `iam:<sub>` of the IAM token (the service account's user id), **not** the client_id. The agent logs it (`IAM principal of the agent (use in Policy Group): iam:…`) at startup when an enabled `auth: iam` MCP server is configured (`log_iam_principal()`), else on its first IAM token fetch; `make check-creds` prints it too.

## Forbidden

- Trusting `user_id` from the body/tool arguments/LLM.
- Using a mutable claim (`email`, `upn`, `preferred_username`) as `AUTH_USER_CLAIM`.
- Token passthrough: forwarding the user's JWT to another service (A2A `user_jwt`, MCP) that accepts the **same audience** — whoever receives it can replay it against this agent. Give each service its own audience and use the IdP's token exchange (RFC 8693) / on-behalf-of flow, or `api_key` + `User-Id` to trusted services.
- Logging/tracing tokens (already masked in tracing; do not `print` headers).
- Hardcoding secrets or committing `.env`/`.greennode.json`.
- Putting `GREENNODE_CLIENT_*` in deploy env files (the runtime injects them).

## Official docs

- [create-runtime](https://docs.greennode.ai/ai-stack/agent-base/agent-runtime/create-runtime) — Runtime Security Settings: Inbound Auth (IAM / JWT / None) and IP Access Control
- [access-control](https://docs.greennode.ai/ai-stack/agent-base/access-control) — agent identities, API key / OAuth2 providers, 3LO `allowedReturnUrls`, `requires_api_key` / `requires_access_token`
- [manage-service-accounts](https://docs.greennode.ai/ai-stack/agent-base/team-permissions/manage-service-accounts) — auto-created runtime service account, client secret shown once
