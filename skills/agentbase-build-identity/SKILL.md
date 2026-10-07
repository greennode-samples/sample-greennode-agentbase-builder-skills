---
name: agentbase-build-identity
description: "Outbound credentials for AI agents on GreenNode AgentBase via Access Control (Identity): agent identity, Static API Key / Delegated API Key / OAuth2 providers, used in tool code agent-wide (M2M) or per end user (OAuth2 3LO consent, delegated key) without blocking the request, per-user isolation, no secrets in tool schemas, traces or chat. Asset app/identity.py (agent_api_key, agent_access_token, user_access_token, user_api_key) + tests. Use when a tool calls an external service with a key or OAuth, when users connect their own account, or when credential retrieval fails. Trigger: access control, identity, API key for external service, OAuth for agent, 3LO, connect Google/Slack account, delegated key, requires_api_key, requires_access_token, thêm API key dịch vụ ngoài, OAuth cho agent, user kết nối tài khoản. DO NOT use for inbound auth (/agentbase-build-auth), MCP connector credentials (/agentbase-build-mcp), the platform LLM key (/agentbase-build-llm), or identity/provider CRUD only (/agentbase-identity)."
---

# Outbound credentials — AgentBase Access Control (Identity)

Asset `app/identity.py` (+ `tests/test_identity.py`, 8 tests against a fake Identity API that keeps consent per user, written against the greennode-agentbase 1.0.3 source; the consent round-trip itself is not yet verified on a live environment).

## Model

```
Identity "my-order-agent"  (persistent, org-unique; shared by the staging + prod runtimes)
  ├─ Static API Key provider   "weather-key"   → one key for the whole agent
  ├─ OAuth2 provider           "google-oauth"  → M2M token (agent) or 3LO token (per end user, after consent)
  └─ Delegated API Key provider "user-openai"  → each end user supplies their own key once
Runtime injects GREENNODE_CLIENT_ID / _SECRET / GREENNODE_AGENT_IDENTITY ⇒ the SDK fetches credentials at call time.
Secrets live in the platform vault — never in .env, code, the image, traces, or the chat.
```

## Choose the helper

| The tool needs… | Provider (create with `/agentbase-identity`) | Helper in `app/identity.py` |
|---|---|---|
| One key for the whole agent (OpenAI, weather, internal API) | Static API Key | `@agent_api_key("weather-key")` |
| An OAuth token as the agent itself (client credentials) | OAuth2, flow M2M | `@agent_access_token("crm", scopes=[...])` |
| The **end user's** account (their Google Calendar, their Slack) | OAuth2, flow 3LO / USER_FEDERATION | `@user_access_token("google-oauth", scopes=[...])` |
| The **end user's** own API key | Delegated API Key | `@user_api_key("user-openai")` |
| Tools behind the MCP Gateway | Connector outbound auth — no agent code | `/agentbase-build-mcp` |

## Where Identity connects

| Component | Link to Identity | Skill |
|---|---|---|
| **Agent Runtime** | Every runtime is created **bound to an identity** (an identity must exist first; staging/prod may share it); the runtime injects `GREENNODE_AGENT_IDENTITY` + its service account. Pick the identity that holds the providers. An identity with runtimes cannot be deleted | `/agentbase-build-deploy` → `/agentbase-deploy` |
| **Agent code** (local tools) | `app/identity.py` fetches credentials from that identity's providers | this skill |
| **MCP Gateway connectors** | The connector's outbound OAuth / API key uses a **Secret Provider from Access Control** (Managed or Custom) — no agent code | `/agentbase-build-mcp` |
| **Your own MCP server** | Receives the token the Gateway attaches (it doesn't call Identity). If deployed on a Runtime it has its own identity; to call further services with stored secrets it follows the same SDK pattern (adapt `app/identity.py`) | `/agentbase-build-mcp-server` |
| **Frontend** | Shows the `AUTHORIZATION_REQUIRED` consent link; the page at `IDENTITY_CALLBACK_URL` is where users land after consent | `/agentbase-build-frontend` |

## Workflow (every mutating platform step needs user confirmation)

1. **Identity** — one per agent, via `/agentbase-identity`. Name 3–50 chars `^[a-zA-Z0-9_-]+$`, unique in the org. Runtimes (staging/prod) share it.
2. **Provider** — via `/agentbase-identity` (Portal / REST / SDK). The **user** enters the secret in the Console or the tool's flow — never ask for it in chat.
   - OAuth2: the create response contains the provider's **`callbackUrl`** ⇒ register it as a redirect URI in the OAuth app at the IdP (Google, Slack…).
   - Least-privilege scopes; separate providers for read and write when possible.
3. **Per-user flows only** (3LO, delegated):
   - Add the app page that users land on after consent to the identity's **`allowedReturnUrls`**.
   - Set `IDENTITY_CALLBACK_URL` to that page in `.env.<env>`. It can be a simple "Done — go back to the chat" page: per the SDK flow the platform's own `callbackUrl` completes the exchange (verify once on your environment).
4. **Local dev** — add `"agent_identity": "<name>"` to `.greennode.json` next to the IAM pair (the `.example` omits it on purpose: projects without Identity providers don't need it), then `make check-creds` must show `OK  Agent identity`.
   - Without it the SDK would silently **create a new identity** that has none of your providers. `app/identity.py` refuses instead.
   - The SDK caches `.greennode.json` for the whole process ⇒ restart `make dev` after editing it.
5. **Code** — decorate an **inner** function; the `@tool` calls it and turns `AuthorizationRequired` into the tool result:

```python
from langchain_core.tools import tool

from app.identity import AuthorizationRequired, agent_api_key, user_access_token


@agent_api_key("weather-key")  # Static API Key provider
async def _weather(city: str, *, api_key: str) -> dict:
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get("https://api.weather.example/v1", params={"q": city}, headers={"X-Key": api_key})
        r.raise_for_status()
        return r.json()


@user_access_token("google-oauth", scopes=["https://www.googleapis.com/auth/calendar.readonly"])
async def _events(day: str, *, access_token: str) -> list[dict]:
    ...  # call Google with Authorization: Bearer <access_token>


@tool
async def list_my_events(day: str) -> str:
    """List the user's Google Calendar events for a day (YYYY-MM-DD)."""
    try:
        return json.dumps(await _events(day))
    except AuthorizationRequired as e:
        return e.tool_message  # "AUTHORIZATION_REQUIRED: … open <link> … then send the request again"
```

   Register the tool in `get_local_tools()`. Tools with side effects using the user's account (send mail, post…) ⇒ `HITL_TOOLS` (`/agentbase-build-hitl`).
6. **Test** — copy the `FakeIdentityAPI` pattern from `tests/test_identity.py`:
   - first call ⇒ `AuthorizationRequired` with the link;
   - after consent ⇒ the credential;
   - another user ⇒ their own link (isolation);
   - `tool.args` never contains the credential.
7. **Verify** on a real environment:
   - `make check-creds`;
   - call the tool once; for 3LO, open the link, consent, and ask again;
   - the trace shows `identity.<kind>` spans with status only, never the secret.

## How per-user (3LO / delegated) works — and why it doesn't block

The SDK's default behavior after it gets an authorization URL is to **poll for up to 600 s**, waiting for the user to consent. That is longer than `REQUEST_TIMEOUT_S` (180 s), and the user cannot even see the link while the request is still running. `user_access_token` / `user_api_key` replace the poller so the request never waits:

```
turn 1: tool → SDK: token for (provider, user)? → platform: not yet, authorization_url
        → AuthorizationRequired → tool returns "AUTHORIZATION_REQUIRED … <link>" → agent shows the link
user  : opens link → consents at the IdP → IdP → platform callbackUrl (token stored per user) → IDENTITY_CALLBACK_URL page
turn 2: same tool → SDK → platform returns the user's token immediately → tool runs
```

- The frontend shows `https://` links as tappable (`MessageBubble`), and only `https://`.
- The credential is keyed by the **verified** user id. `app/service.py` sets `GreenNodeAgentBaseContext.set_user_id(principal.user_id)` on every path (HTTP, A2A, `run_chat`).
- No user in context, or no `IDENTITY_CALLBACK_URL` ⇒ the helper refuses before calling the platform (fail closed).

## Security rules

- **Never decorate the `@tool` itself.** The injected `api_key` / `access_token` would become a tool argument: visible in the schema the LLM sees, and in every tool-call trace.
- Never return, log or trace the credential. `identity.*` spans record the provider and status only, and tracing masks `*_token` / `api_key` keys anyway.
- Never take a user id as a tool argument for per-user credentials. It comes from the verified principal only.
- A credential of user A must never serve user B. Don't cache user credentials in module globals or shared caches; fetch through the helper on each call (the platform stores consented tokens per user).
- `AUTHORIZATION_REQUIRED` means the agent must tell the user and wait. It must not retry in a loop (the tool message says so). `str(e)` is the same message, so a tool without `try/except` still surfaces the link through ToolNode's error handling.
- **Consent links are bearer links.** Whoever opens one binds THEIR account to the user who requested it — tell users to open only links shown in their own chat, never forwarded ones. If your IdP/platform echoes `custom_state` to the return page, check it against the logged-in user there.
- Tools sharing a provider should request the **same scopes**: scopes are part of the token request, so different scopes may ask the user to consent again.

## Gotchas (verified against greennode-agentbase 1.0.3)

| Symptom | Cause | Fix |
|---|---|---|
| `TypeError: … unexpected keyword 'api_key'` / missing arg | The SDK injects into `into=` (default `api_key` / `access_token`). The official docs sample uses `openai_key` without `into=` | Name the parameter `api_key`/`access_token`, or pass `into=` |
| `TypeError` on `requires_access_token` | `scopes` and `auth_flow` are required keyword arguments (the docs sample omits them) | Use the helpers above |
| Request hangs ~10 min, then times out | Raw SDK decorator with `USER_FEDERATION` polls for 600 s | Use `user_access_token` / `user_api_key` |
| A new identity appears in the Console | `GREENNODE_AGENT_IDENTITY` unset locally ⇒ the SDK auto-creates one | `agent_identity` in `.greennode.json` (helpers refuse without it) |
| Changed `.greennode.json` has no effect | SDK caches the file per process | Restart the app |
| "Agent can't retrieve credential" | Identity name missing / wrong in the runtime | Check `GREENNODE_AGENT_IDENTITY` (injected on Runtime) |
| 403 from Identity API | Service account lacks permission | Attach `AgentBaseFullAccess` (IAM console) |
| 404 provider | Provider name typo / other identity | `list` providers via `/agentbase-identity` |
| 3LO redirect error at the IdP | Provider `callbackUrl` not registered in the OAuth app, or `IDENTITY_CALLBACK_URL` not in `allowedReturnUrls` | Register both |

## Official docs

- [access-control](https://docs.greennode.ai/ai-stack/agent-base/access-control) — identities, Static / Delegated API Key and OAuth2 providers (Portal, REST, SDK), retrieving credentials at runtime, troubleshooting
- [runtime-reference](https://docs.greennode.ai/ai-stack/agent-base/agent-runtime/runtime-reference) — env vars the Runtime injects (`GREENNODE_AGENT_IDENTITY`…)
- [manage-service-accounts](https://docs.greennode.ai/ai-stack/agent-base/team-permissions/manage-service-accounts) — the runtime's service account and permissions
