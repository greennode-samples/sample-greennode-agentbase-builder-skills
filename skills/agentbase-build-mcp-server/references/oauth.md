# OAuth for MCP servers on AgentBase

The MCP server acts as an **OAuth 2.0 Resource Server** (MCP Authorization spec): it only *verifies* access tokens, it does not issue them. Tokens are issued by the IdP (Authorization Server); Gateway/Identity obtains the token and attaches it to the request.

## 1. OAuth 2LO — machine-to-machine (client credentials)

```
Identity (OAuth2 provider: client_id/secret, token URL) ──▶ Gateway requests token (M2M) ──▶ Bearer → MCP server
```
- IdP: create an **API/Resource** (audience, e.g. `https://<runtime>/mcp` or `api://hr-mcp`) + scopes (`hr.read`, `hr.write`); create a client for the Gateway, grant the needed scopes.
- Server: `MCP_AUTH_MODE=jwt`, `MCP_AUDIENCE=<audience>`, `MCP_ISSUER`, `MCP_JWKS_URL` — all required (startup error otherwise). `aud` is always verified (RFC 8707): a token the same IdP minted for another API, or one with no `aud`, gets 401.
- No end-user ⇒ use for shared-data tools; per-user tools will refuse.

## 2. OAuth 3LO — the user's own data (user federation)

```
user ─▶ agent ─▶ Gateway: no user token yet ⇒ authorization URL (consent) ─▶ user consents
      ─▶ Identity stores the token per user ─▶ subsequently Gateway attaches Bearer <user's token> ─▶ MCP server
```
- Connector config: OAuth **3LO**, scopes, **Return URL** (must be in the agent identity's `allowedReturnUrls` — `/agentbase-identity`).
- The Gateway needs to know the user ⇒ gateway inbound **JWT** (principal claim `sub`), agent forwards the user JWT (`auth: user_jwt` in `mcp_servers.json`).
- Server: `current_user()` = `sub` of the token **issued by the provider** (differs from the app JWT's `sub` if the IdP differs) ⇒ when mapping internal data, keep a mapping table `(issuer, sub) → internal user`.
- The frontend must handle the agent returning an authorize link (display it, user opens it, returns to chat).
- The Gateway's exact behavior when the user hasn't consented (error code / payload carrying the authorization URL) must be **verified on a real environment** before building the UI.

## 3. Inbound forward

- The Gateway forwards the agent's inbound credential **as-is** (user JWT or IAM token) to the MCP server. The MCP server then holds a credential that is valid far beyond it — treat this mode as handing over that credential.
- ⚠️ **Never with gateway inbound IAM.** The forwarded token is the agent runtime's service-account platform token (AgentBaseFullAccess): whoever controls or compromises the MCP server (or reads a logged header) can call the AgentBase platform with the agent's full permissions. It also cannot be verified by the server (GreenNode IAM **does not publish a JWKS**). Use OAuth 2LO / API Key instead.
- **Gateway inbound JWT** ⇒ only towards MCP servers **you own** that validate the same IdP (`jwt`, same `MCP_ISSUER`, `MCP_AUDIENCE` checked). Then it is the simplest way for the MCP server to know the end-user when the app & internal systems share an IdP. Never forward to third-party or shared MCP servers — use OAuth 3LO so they get a token scoped to them.
- The user JWT's `aud` must include the MCP server's `MCP_AUDIENCE` (configure the IdP to issue it), otherwise the server rejects it with 401.

## 4. Opaque tokens (not JWT)

Write an `IntrospectionVerifier(TokenVerifier)` that calls the IdP's introspection endpoint (RFC 7662) with the server's client credentials, caches results by `exp`, and maps `active/sub/scope/aud` to `AccessToken`. Wire it into `build_verifier()` with a new mode `introspect`.

## 5. Scope design

- Format `<resource>.<action>`: `orders.read`, `orders.write`, `orders.refund`.
- Read tools ⇒ `*.read`; write tools ⇒ `*.write`; sensitive actions get a separate scope (`orders.refund`) and are **also** added to `HITL_TOOLS` in the agent + Policy Group.
- `MCP_REQUIRED_SCOPES` for the minimum scope on every request (e.g. `mcp.invoke`); per-tool scopes use `require_scope()`.
- API Key 2LO: the key carries no scopes ⇒ the server grants `MCP_REQUIRED_SCOPES` + `MCP_API_KEY_SCOPES`. List only shared-data scopes (e.g. `catalog.read`); per-user tools refuse API keys regardless (no user).

## 6. Credential rotation

- API key: add the new hash to `MCP_API_KEY_SHA256` (list) → deploy → update the key in the Identity provider → remove the old hash.
- JWT: when the IdP rotates signing keys ⇒ `PyJWKClient` fetches the new JWKS (JWK set cached 1h; an unknown `kid` triggers a refetch, rate-limited by PyJWT). A key the IdP **removes** (rotated out / compromised) is rejected once that 1h cache expires — keep `cache_keys=False` in `auth.py`: PyJWT's per-`kid` cache never expires and would trust a removed key until restart. Need faster revocation ⇒ lower `lifespan` in `_jwks()` or restart the server.
