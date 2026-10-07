---
name: agentbase-build-mcp-server
description: "Standard for BUILDING an MCP server (Python, uv, FastMCP streamable HTTP) with Authentication/OAuth to deploy on GreenNode AgentBase Runtime and attach to the MCP Gateway as a Custom Connector: verify API key (Gateway outbound API Key 2LO) or OAuth 2.0 JWT (2LO M2M, 3LO user, Inbound forward) per the MCP Authorization spec (Resource Server, RFC 9728 protected resource metadata, RFC 8707 audience, scopes), user identity taken from the token to isolate data per user, /health for the runtime, end-to-end tests. Use when writing a new MCP server, protecting an MCP server, adding OAuth/scopes to tools, or exposing internal systems as agent tools. Trigger: build MCP server, write MCP server, MCP server authentication, OAuth for MCP, protect MCP server, custom connector, viết MCP server, OAuth cho MCP, bảo vệ MCP server. DO NOT use for connecting an agent to an existing MCP (use /agentbase-build-mcp)."
---

# Build an MCP Server with Authentication / OAuth

Verified template (`assets/mcp_server/`, 21 e2e tests: real uvicorn server + real MCP client, incl. the local quickstart below). Runs locally out of the box (`MCP_AUTH_MODE=none`).

| File | Role |
|---|---|
| `server.py` | FastMCP app, `/health`, `require_scope()` / `current_user()`, **sample tools** (replace with yours) |
| `auth.py` · `settings.py` | Token verification (api_key / jwt) · env `MCP_*` with validation |
| `store.py` | Per-user data access (SQLite locally; every query filtered by owner) |
| `backend.py` | Client for the internal system: timeout + clean `ToolError` mapping |
| `scripts/call_tool.py` · `Makefile` | Call tools from the shell · `make setup/dev/test/lint/tools/call` |
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
| **API Key 2LO** | `Authorization: Bearer <key>` | `api_key` (SHA-256 compare) | ❌ — shared tools only |
| **OAuth 2LO** (client credentials) | Client's Bearer access token | `jwt` | ❌ (subject = client) |
| **OAuth 3LO** (user consent) | **User's** Bearer access token | `jwt` | ✅ `sub` |
| **Inbound forward** | JWT the agent/user sent to the Gateway | `jwt` (same IdP as gateway inbound JWT) | ✅ |
| No authorization (local dev) | — | `none` (default; Settings blocks it outside `MCP_APP_ENV=local`) | ✅ fixed `MCP_DEV_USER` |

Rule: tools reading/writing **user-private data** must call `current_user()` (from `AccessToken.subject`); 2LO has no user ⇒ the tool refuses (tested). **Never** accept `user_id` as a tool argument.

## Workflow

1. **Scaffold** (separate directory, e.g. `src/mcp_servers/<name>/` or a separate repo):
   ```bash
   cp -R <skill-dir>/assets/mcp_server <dest> && cd <dest>
   sed -i '' 's/__SERVER_NAME__/<name>/g' pyproject.toml settings.py .env.example Makefile   # Linux: sed -i
   make setup && make test      # uv sync + .env from .env.example · 21 passed
   ```
2. **Write tools** in `server.py` — **read `references/tool-design.md` first**; copy the closest sample, then delete the samples you don't need (and their tests):
   - Shared data from an internal system ⇒ copy `get_product` + `backend.py` (`MCP_BACKEND_URL`, timeout, error mapping).
   - Per-user data ⇒ copy `add_note` / `list_notes` / `delete_note` + `store.py` (`current_user()`, every query `WHERE owner = ?`, `idempotency_key` on writes, pagination).
   - Every tool: async, docstring (the LLM reads it), inputs `Annotated[..., Field(...)]`, Pydantic return model (structured output), `ToolAnnotations`, `require_scope("<resource>.<action>")`.
   - Tests: add a block per tool in `tests/test_tools.py` (happy · invalid input · missing scope · isolation · upstream failures) ⇒ `make test` green before moving on.
3. **Run & try locally** (no IdP, no gateway needed):
   ```bash
   make dev                                   # .env: MCP_APP_ENV=local, MCP_AUTH_MODE=none → http://localhost:8080/mcp
   curl -s localhost:8080/health              # {"status":"ok"}
   make tools                                 # list tools
   make call TOOL=add_note ARGS='{"text":"hi"}'   # prints structured output; exit 1 if isError
   ```
   - `none` mode: `current_user()` = `MCP_DEV_USER`, `require_scope()` always passes ⇒ restart with another `MCP_DEV_USER` to see per-user isolation by hand. Scopes, 401 and real isolation are covered by `uv run pytest` (JWT mode with a test-signed token) — add a test per new tool.
   - Test a real auth mode locally: set `MCP_AUTH_MODE=api_key` + hash in `.env`, then `make call TOOL=… TOKEN=<raw key>`.
   - Internal system locally: point `MCP_BACKEND_URL` at a dev/mock instance (+ `MCP_BACKEND_TOKEN`); empty ⇒ those tools answer "not configured".
   - Interactive UI (optional): `make inspector` → Streamable HTTP, URL `http://localhost:8080/mcp`.
4. **Connect a local agent** (optional, agent built with `/agentbase-build-mcp`) — call the server directly, bypassing the gateway, in the agent's `mcp_servers.json`:
   ```json
   "notes": {"transport": "streamable_http", "url": "http://localhost:8080/mcp", "auth": "none", "envs": ["local"]}
   ```
   For `api_key` mode add `"headers": {"Authorization": "Bearer ${NOTES_MCP_KEY}"}` (`${…}` is expanded from the agent's env). Tool names in the agent = `<server>_<tool>` (e.g. `notes_add_note`). Policy Group does not apply without the gateway.
5. **Configure auth** (`.env.example` → deploy env file):
   - `api_key`: generate a key, env holds only `MCP_API_KEY_SHA256=["<sha256>"]`; **store the raw key in AgentBase Identity** (API key provider via `/agentbase-identity`) for the connector to use — not in the repo.
   - `jwt`: `MCP_ISSUER`, `MCP_JWKS_URL`, `MCP_AUDIENCE` (API identifier registered at the IdP, ideally = `MCP_RESOURCE_URL`), `MCP_REQUIRED_SCOPES` if every request needs a scope.
   - `MCP_RESOURCE_URL` = `<runtime endpoint>/mcp` — appears in `WWW-Authenticate` and `/.well-known/oauth-protected-resource` so OAuth clients can discover the authorization server.
6. **Deploy to Runtime** — `/agentbase-build-deploy` (Docker linux/amd64, port 8080, `/health`). Suggested runtime name `<name>-mcp`.
7. **Create a Custom Connector** on the MCP Gateway (Console → MCP Connectors → *Add Custom Connector*, or a target via `/agentbase-gateway`):
   - MCP URL = `https://<runtime endpoint>/mcp`.
   - Outbound auth per the table above; **Header key `Authorization`, prefix `Bearer `**.
   - OAuth / API key: the connector's secret is a provider in **Access Control** — provider types and rules in `/agentbase-build-identity`, created with `/agentbase-identity` (Managed if available, otherwise Custom with your own OAuth App at the IdP); declare scopes; 3LO needs the Return URL in `allowedReturnUrls`.
   - The server only **receives** the token the Gateway attaches — it never calls Identity for it. If the server itself must call another service with a stored secret, follow the same SDK pattern as the agent (`/agentbase-build-identity`; its `app/identity.py` depends on the agent's `app.config`/`app.observability`, so adapt it — it is not part of this template); when deployed on a Runtime it has its **own** identity (every Runtime is bound to one).
   - Server in your VPC or data center ⇒ **Private MCP Gateway** (VPC Peering, Route CIDRs; VPN for on-prem) — see `/agentbase-build` `references/private-networking.md` and the sample [sample-onprem-mcp-vpn](https://github.com/greennode-samples/sample-onprem-mcp-vpn).
8. **Policy Group** — grant `actions: ["<connector>__<tool>", ...]` to the agent's principal (`/agentbase-build-mcp`).
9. **Verify**: `curl <endpoint>/health` 200 · `POST /mcp` without token ⇒ 401 · agent calls the tool via the gateway successfully · 2 different users can't see each other's data.

## Detailed OAuth design

Read `references/oauth.md` — 2LO / 3LO / inbound forward flows, registering the API at the IdP, scope design, token introspection for opaque tokens, key rotation.

## Mandatory security

- `stateless_http=True` (enabled): no session kept in RAM ⇒ safe to scale to multiple replicas; user state lives in the DB (SQLite is local-only — switch `store.py` to Postgres before running > 1 replica).
- Don't log tokens (`AccessToken.token`); log `subject`, `client_id`, tool, latency.
- Rate limit / payload size at the internal system or reverse proxy if tools are resource-heavy.
- Business errors return clear messages (the LLM reads them); permission errors ⇒ `Forbidden` (MCP `isError`), without leaking internal details.
- `MCP_AUTH_MODE=none` local only.

## Tests (included in the template)

- `tests/test_auth.py`: public health · 401 + `WWW-Authenticate resource_metadata` · wrong key · API key has no user identity · missing scope · invalid JWT (wrong audience / issuer / expired) · protected resource metadata · settings defaults (`local`/`none`, refused elsewhere) · **local quickstart** (`.env.example` + `python server.py` + `scripts/call_tool.py` as a real process).
- `tests/test_tools.py`: per-user isolation · idempotent write · pagination · can't delete another user's note · internal system OK / 404 / timeout / 500 without leaking details (fake backend via `httpx.MockTransport`) · input validation · scope · annotations & output schema.
- Tests run in a temp dir (own SQLite file, no developer `.env`). Fixtures are documented at the top of `tests/conftest.py`.

## Official docs

- [connect-a-connector](https://docs.greennode.ai/ai-stack/agent-base/mcp-connectors/connect-a-connector) — adding a Custom Connector and its outbound auth (what your server receives)
- [runtime-reference](https://docs.greennode.ai/ai-stack/agent-base/agent-runtime/runtime-reference) — runtime contract when deploying the MCP server
- [private-networking](https://docs.greennode.ai/ai-stack/agent-base/private-networking) — reaching internal systems / private MCP servers
