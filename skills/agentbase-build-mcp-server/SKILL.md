---
name: agentbase-build-mcp-server
description: "Standard for BUILDING an MCP server (Python, uv, FastMCP streamable HTTP) with Authentication/OAuth to deploy on GreenNode AgentBase Runtime and attach to the MCP Gateway as a Custom Connector: verify API key (Gateway outbound API Key 2LO) or OAuth 2.0 JWT (2LO M2M, 3LO user, Inbound forward) per the MCP Authorization spec (Resource Server, RFC 9728 protected resource metadata, RFC 8707 audience, scopes), user identity taken from the token to isolate data per user, /health for the runtime, end-to-end tests. Use when writing a new MCP server, protecting an MCP server, adding OAuth/scopes to tools, or exposing internal systems as agent tools. Trigger: build MCP server, write MCP server, MCP server authentication, OAuth for MCP, protect MCP server, custom connector, viết MCP server, OAuth cho MCP, bảo vệ MCP server. DO NOT use for connecting an agent to an existing MCP (use /agentbase-build-mcp)."
---

# Build an MCP Server with Authentication / OAuth

Verified template (`assets/mcp_server/`, 47 tests — e2e: real uvicorn server + real MCP client + a real local JWKS endpoint, incl. the local quickstart below). Runs locally out of the box (`.env`: `MCP_APP_ENV=local`, `MCP_AUTH_MODE=none`, listening on 127.0.0.1 only). Fails closed when deployed: with no/incomplete env (and in the Docker image, `MCP_APP_ENV=prod`) `none` is refused and the server does not start.

| File | Role |
|---|---|
| `server.py` | FastMCP app, `/health`, `require_scope()` / `current_user()`, **sample tools** (replace with yours) |
| `auth.py` · `settings.py` | Token verification (api_key / jwt) · env `MCP_*` with validation |
| `store.py` | Per-user data access (SQLite, **local only** — `open_store()` refuses it when deployed; every query filtered by owner) |
| `backend.py` | Client for the internal system: timeout + clean `ToolError` mapping |
| `scripts/call_tool.py` · `Makefile` | Call tools from the shell · `make setup/dev/test/lint/tools/call` |
| `scripts/set_secret.py` | `make set-secret`: the **user** sets secrets (`MCP_BACKEND_TOKEN`…) in their own terminal with hidden input — same script as the agent template (`/agentbase-build-scaffold`) |
| `tests/conftest.py` · `test_auth.py` · `test_tools.py` | Harness + fixtures · auth tests · **per-tool tests (copy per new tool)** |

## Standard architecture

```
agent ──(inbound IAM/JWT)──▶ MCP Gateway ──(connector outbound auth)──▶ MCP server (Runtime) ──▶ internal system
                              Policy Group                              token verified here
```

Two independent protection layers: **Gateway** (who may call which tool — Policy Group) and **MCP server** (is the token valid, sufficient scope, which user). The Runtime endpoint is public with no auth ⇒ the MCP server **must always verify itself**, even if only the Gateway calls it.

## Choosing the auth mode

| Connector outbound auth | Server receives | `MCP_AUTH_MODE` | End-user identity? |
|---|---|---|---|
| **API Key 2LO** | `Authorization: Bearer <key>` | `api_key` (SHA-256 compare; scopes granted = `MCP_API_KEY_SCOPES`) | ❌ — shared tools only |
| **OAuth 2LO** (client credentials) | Client's Bearer access token | `jwt` | ❌ — detected as a machine token ⇒ no user (below) |
| **OAuth 3LO** (user consent) | **User's** Bearer access token | `jwt` | ✅ `sub` |
| **Inbound forward** ⚠️ | The agent's own inbound credential, as sent to the Gateway | `jwt` (same IdP as gateway inbound JWT) — **JWT inbound only**, never IAM | ✅ |
| No authorization (local dev) | — | `none`: only with `MCP_APP_ENV=local` (refused by default and in the image), binds 127.0.0.1, logs a warning | ✅ fixed `MCP_DEV_USER` |

Rule: tools reading/writing **user-private data** must call `current_user()` (from `AccessToken.subject`); 2LO has no user ⇒ the tool refuses even when the API key / client is granted the scope (tested). **Never** accept `user_id` as a tool argument.

**OAuth 2LO tokens carry a `sub` too — the client's.** Accepting it as a user would put every user of the agent in **one** per-user bucket. `auth.py` `is_machine_token()` gives such tokens no user: Auth0 `gty=client-credentials`, Entra `idtyp=app` or `sub == oid`, `sub` equal to the client id (`azp`/`client_id`/`cid`/`appid` — Okta and most IdPs), Keycloak `preferred_username=service-account-*`. IdPs differ ⇒ **set `MCP_USER_TOKEN_CLAIM`** to a claim only end-user tokens carry at your IdP (`email`; Entra: `scp`): a token without it has no user. Defense in depth: **never grant per-user scopes (`notes.*`) to 2LO clients or API keys**. Entra app-only tokens carry their permissions in `roles` (no `scp`): mapped to scopes.

⚠️ **Inbound forward hands your MCP server the agent's credential.** With gateway inbound **IAM** that is the runtime service account's platform token (AgentBaseFullAccess): whoever controls or compromises the MCP server can call the AgentBase platform with the agent's full permissions ⇒ **never use Inbound forward with IAM inbound**. With **JWT** inbound, use it only towards MCP servers you own that validate the same IdP (`jwt`, `MCP_AUDIENCE` checked); for anything else use OAuth 2LO/3LO or API Key. Details: `references/oauth.md` §3.

## Workflow

1. **Scaffold** (separate directory, e.g. `src/mcp_servers/<name>/` or a separate repo):
   ```bash
   cp -R <skill-dir>/assets/mcp_server <dest> && cd <dest>
   sed -i '' 's/__SERVER_NAME__/<name>/g' pyproject.toml uv.lock settings.py .env.example Makefile   # Linux: sed -i
   make setup && make test      # uv sync + .env from .env.example · 47 passed (the shipped `uv.lock` is the tested dependency set; commit it)
   ```
2. **Write tools** in `server.py` — **read `references/tool-design.md` first**; copy the closest sample, then delete the samples you don't need (and their tests):
   - Shared data from an internal system ⇒ copy `get_product` + `backend.py` (`MCP_BACKEND_URL`, timeout, error mapping).
   - Per-user data ⇒ copy `add_note` / `list_notes` / `delete_note` + `store.py` (`current_user()`, every query `WHERE owner = ?`, `idempotency_key` on writes, pagination).
   - Every tool: async, docstring (the LLM reads it), inputs `Annotated[..., Field(...)]`, Pydantic return model (structured output), `ToolAnnotations`, `require_scope("<resource>.<action>")`.
   - Tests: add a block per tool in `tests/test_tools.py` (happy · invalid input · missing scope · isolation · upstream failures) ⇒ `make test` green before moving on.
3. **Run & try locally** (no IdP, no gateway needed). Running next to a local agent? The agent's `make dev` also listens on 8080 ⇒ use `make dev PORT=8081` here (sets `MCP_PORT` and `MCP_RESOURCE_URL`) and `PORT=8081` / `:8081` in the commands below:
   ```bash
   make dev                                   # .env: MCP_APP_ENV=local, MCP_AUTH_MODE=none → http://localhost:8080/mcp (127.0.0.1 only)
   curl -s localhost:8080/health              # {"status":"ok"}
   make tools                                 # list tools
   make call TOOL=add_note ARGS='{"text":"hi"}'   # prints structured output; exit 1 if isError
   ```
   - `none` mode: `current_user()` = `MCP_DEV_USER`, `require_scope()` always passes, the server logs `authentication is DISABLED` and listens on 127.0.0.1 only ⇒ restart with another `MCP_DEV_USER` to see per-user isolation by hand. Scopes, 401 and real isolation are covered by `uv run pytest` (JWT mode with a test-signed token) — add a test per new tool.
   - Test a real auth mode locally: generate a key with the `.env.example` one-liner (raw key → `~/.<name>-mcp-api-key`, only the hash printed), set `MCP_AUTH_MODE=api_key` + hash in `.env` (+ `MCP_API_KEY_SCOPES` for the tools the key may call), then `make call TOOL=… TOKEN="$(cat ~/.<name>-mcp-api-key)"` (the key never appears in the command or the chat). With auth on, the server listens on 0.0.0.0.
   - Docker locally: `make docker-build docker-run` (publishes on 127.0.0.1:8080 — `PORT=8081` to change — and sets `MCP_HOST=0.0.0.0` inside the container so `none` stays usable for local tests, plus `MCP_DB_PATH=/tmp/mcp.db`: the image keeps `/app` read-only for the server user).
   - Internal system locally: point `MCP_BACKEND_URL` at a dev/mock instance (+ `MCP_BACKEND_TOKEN`: the user runs `make set-secret NAME=MCP_BACKEND_TOKEN` in their own terminal — never pasted into the chat); empty ⇒ those tools answer "not configured".
   - Interactive UI (optional): `make inspector` → Streamable HTTP, URL `http://localhost:8080/mcp`.
4. **Connect a local agent** (optional, agent built with `/agentbase-build-mcp`) — the agent's `make dev` binds 8080, so run this server with **`make dev PORT=8081`**, then call it directly, bypassing the gateway, in the agent's `mcp_servers.json`:
   ```json
   "notes": {"transport": "streamable_http", "url": "http://localhost:8081/mcp", "auth": "none", "envs": ["local"]}
   ```
   For `api_key` mode add `"headers": {"Authorization": "Bearer ${NOTES_MCP_KEY}"}` (`${…}` is expanded from the agent's env). Tool names in the agent = `<server>_<tool>` (e.g. `notes_add_note`). Policy Group does not apply without the gateway.
5. **Configure auth** (`.env.example` → deploy env file; it **must** set `MCP_AUTH_MODE=api_key|jwt` and must **not** set `MCP_APP_ENV=local` (the image defaults to `prod`) — a missing or incomplete env makes the server refuse to start, never serve unauthenticated; leave `MCP_HOST` empty):
   - `api_key`: generate a key with the `.env.example` one-liner (raw key → a `0600` file outside the repo, only the hash printed — never print the raw key where a coding agent reads it), env holds only `MCP_API_KEY_SHA256=["<sha256>"]`; **the user stores the raw key in AgentBase Identity** (API key provider via `/agentbase-identity`) for the connector to use — not in the repo. The key carries no scopes: `MCP_API_KEY_SCOPES` lists the scopes it is granted (e.g. `["catalog.read"]` for `get_product`; only shared-data tools — per-user tools refuse API keys anyway).
   - `jwt`: `MCP_ISSUER`, `MCP_JWKS_URL`, `MCP_AUDIENCE` — all **required** (startup error otherwise); `aud` is always verified, so tokens minted for another API or without `aud` get 401. `MCP_AUDIENCE` = API identifier registered at the IdP, ideally = `MCP_RESOURCE_URL`. `MCP_REQUIRED_SCOPES` if every request needs a scope.
   - `jwt`: `MCP_USER_TOKEN_CLAIM` (recommended, see the 2LO rule above).
   - `MCP_RESOURCE_URL` = `https://<runtime endpoint>/mcp` — **jwt mode only**: appears in `WWW-Authenticate` and `/.well-known/oauth-protected-resource` so OAuth clients can discover the authorization server; must be `https://` unless `MCP_APP_ENV=local` (startup error otherwise — the default `http://localhost:8080/mcp` would be advertised). `api_key` mode publishes no protected-resource metadata (there is no authorization server).
   - Per-user data: switch `store.py` to **Postgres before deploying — even with 1 replica**: the Runtime filesystem is wiped on every redeploy and autoscaled replicas don't share it, so `open_store()` refuses SQLite outside `MCP_APP_ENV=local` (`MCP_ALLOW_SQLITE=true` only for a throwaway demo — logs an ERROR, data is lost on redeploy).
6. **Deploy to Runtime** — `/agentbase-build-deploy` (Docker linux/amd64, port 8080, `/health`; the image sets `MCP_APP_ENV=prod`). Suggested runtime name `<name>-mcp`.
7. **Create a Custom Connector** on the MCP Gateway (Console → MCP Connectors → *Add Custom Connector*, or a target via `/agentbase-gateway`):
   - MCP URL = `https://<runtime endpoint>/mcp`. Connector name `[a-z0-9]{3,50}`, **no dash** (`hrmcp`, not `hr-mcp`): it prefixes the policy actions `<connector>__<tool>`, which accept only letters, digits and `_` (`references/tool-design.md` §1).
   - Outbound auth per the table above; **Header key `Authorization`, prefix `Bearer `**. Inbound forward only with gateway inbound JWT (see the warning above).
   - OAuth / API key: the connector's secret is a provider in **Access Control** — provider types and rules in `/agentbase-build-identity`, created with `/agentbase-identity` (Managed if available, otherwise Custom with your own OAuth App at the IdP); declare **minimal** scopes; 3LO needs the Return URL in `allowedReturnUrls`. ⚠ Console 3LO consent is given at connect time by whoever clicks *Authorize* and shared by the project unless verified otherwise (`references/oauth.md` §2) ⇒ side-effect tools of a 3LO connector go in the agent's `HITL_TOOLS`.
   - The server only **receives** the token the Gateway attaches — it never calls Identity for it. If the server itself must call another service with a stored secret, follow the same SDK pattern as the agent (`/agentbase-build-identity`; its `app/identity.py` depends on the agent's `app.config`/`app.observability`, so adapt it — it is not part of this template); when deployed on a Runtime it has its **own** identity (every Runtime is bound to one).
   - Server in your VPC or data center ⇒ **Private MCP Gateway** (VPC Peering, Route CIDRs; VPN for on-prem) — see `/agentbase-build` `references/private-networking.md` and the sample [sample-onprem-mcp-vpn](https://github.com/greennode-samples/sample-onprem-mcp-vpn).
8. **Policy Group** — grant `actions: ["<connector>__<tool>", ...]` to the agent's principal (`/agentbase-build-mcp`).
9. **Verify**: `curl <endpoint>/health` 200 · `POST /mcp` without token ⇒ 401 · agent calls the tool via the gateway successfully · 2 different users can't see each other's data · a 2LO (client-credentials) token is refused by per-user tools.

## Detailed OAuth design

Read `references/oauth.md` — 2LO / 3LO / inbound forward flows, registering the API at the IdP, scope design, token introspection for opaque tokens, key rotation.

## Mandatory security

- `stateless_http=True` (enabled): no session kept in RAM ⇒ safe to scale to multiple replicas; user state lives in the DB. **Deployed ⇒ Postgres, even with 1 replica** (Runtime filesystem is not persistent): `open_store()` refuses SQLite outside `MCP_APP_ENV=local` unless `MCP_ALLOW_SQLITE=true`.
- Don't log tokens (`AccessToken.token`); log `subject`, `client_id`, tool, latency.
- Rate limit / payload size at the internal system or reverse proxy if tools are resource-heavy.
- Business errors return clear messages (the LLM reads them); permission errors ⇒ `Forbidden` (MCP `isError`), without leaking internal details. `require_scope()` fails closed: with auth on, no verified token ⇒ refused.
- Upstream responses are validated (`backend.get_model(Model, path)`): non-JSON bodies and contract drift ⇒ `Internal system returned an unexpected response.` (pydantic errors embed the raw upstream record — never return them to the LLM).
- Per-user tools refuse OAuth 2LO / API-key callers (no user) — see the 2LO rule; set `MCP_USER_TOKEN_CLAIM`.
- `MCP_AUTH_MODE=none` local only: needs explicit `MCP_APP_ENV=local` (default and image are `prod`), binds 127.0.0.1, logs a warning.
- JWT: `aud` always checked (`MCP_AUDIENCE` required); the signing key's own alg is enforced (PyJWK, not raw key) and any verifier error is a 401, never a 500; no per-`kid` key cache, so a key the IdP removes stops working within 1h; the JWKS fetch runs in a thread (`asyncio.to_thread`), so a slow IdP never stalls the event loop. API keys get only `MCP_REQUIRED_SCOPES` + `MCP_API_KEY_SCOPES`.
- Idempotent writes must be atomic (`INSERT … ON CONFLICT DO NOTHING` + read back in one transaction) and return the stored row — see `references/tool-design.md` §2.

## Tests (included in the template)

- `tests/test_auth.py`: public health · 401 + `WWW-Authenticate resource_metadata` (https `MCP_RESOURCE_URL`; none in api_key mode) · wrong key · API key has no user identity (even with the notes scopes granted) · OAuth 2LO tokens have no user (Auth0 / Okta / Keycloak / Entra variants, `MCP_USER_TOKEN_CLAIM`) · Entra `roles` as scopes · JWKS unreachable ⇒ 401 · `require_scope` fails closed · SQLite refused outside local · missing scope · invalid JWT (wrong / missing audience, wrong issuer, expired) · `MCP_AUDIENCE` required + verifier never skips `aud` · `MCP_JWT_ALGORITHMS` asymmetric only (HS*/none refused at startup, like the agent's `AUTH_ALGORITHMS`) · alg/key-type mismatch (ES256 header on an RSA kid) ⇒ 401 not 500 · EC key from JWKS accepted · key removed from the JWKS ⇒ 401 once the 1h JWK-set cache expires · slow JWKS fetch doesn't block other requests · protected resource metadata · fail closed (no env ⇒ `none` refused; Dockerfile env refuses `none`) · `none` binds 127.0.0.1 and warns · **local quickstart** (`.env.example` + `python server.py` + `scripts/call_tool.py` as a real process).
- `tests/test_tools.py`: per-user isolation · idempotent write (replay returns the stored note) · 8 concurrent retries ⇒ one note, no SQL error · pagination · can't delete another user's note · internal system OK / 404 / timeout / 500 / non-JSON / contract drift without leaking details (fake backend via `httpx.MockTransport`) · `get_product` with an API key (`MCP_API_KEY_SCOPES`) · input validation · scope · annotations & output schema.
- `tests/test_set_secret.py`: `make set-secret` writes `MCP_BACKEND_TOKEN` with hidden input, keeps the rest of `.env`, mode 600, never prints it, refuses without a TTY (in-depth tests live in the agent template).
- Tests run in a temp dir (own SQLite file, no developer `.env`). Fixtures are documented at the top of `tests/conftest.py`.

## Official docs

- [connect-a-connector](https://docs.greennode.ai/ai-stack/agent-base/mcp-connectors/connect-a-connector) — adding a Custom Connector and its outbound auth (what your server receives)
- [runtime-reference](https://docs.greennode.ai/ai-stack/agent-base/agent-runtime/runtime-reference) — runtime contract when deploying the MCP server
- [private-networking](https://docs.greennode.ai/ai-stack/agent-base/private-networking) — reaching internal systems / private MCP servers
