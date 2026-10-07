---
name: agentbase-build-mcp
description: "Standard for connecting tools to LangGraph AI agents on GreenNode AgentBase via MCP Governance: local tools, MCP Connectors (GitHub/Slack/M365 catalog or Custom, one connectUrl per connector), MCP Gateway (inbound IAM/JWT, outbound OAuth/API Key/Inbound forward from Identity) and Policy Group (required — without one, everything is 403; principal iam:<sub>/jwt:<sub>, action target__tool). Includes mcp_servers.json, auto-refreshing IAM token, policy-deny guard, tracing. Use when adding a tool, connecting SaaS/internal systems, configuring connector/gateway/policy for an agent, or debugging tools getting 403/denied. Trigger: add tool, MCP, MCP connector, MCP gateway, policy group, denied by policy, connect GitHub/Slack/M365, thêm tool, nối GitHub/Slack/M365. DO NOT use for gateway CRUD (use /agentbase-gateway) or writing detailed policy statements (use /agentbase-policy)."
---

# Tools, MCP Connectors, MCP Gateway & Policy Group

## Model (verified end-to-end on AgentBase 2026-10)

```
agent (app/tools/mcp.py)
  │  Authorization: Bearer <agent IAM token>         (gateway inboundAuth = IAM)
  │  or Bearer <user JWT>                            (inboundAuth = JWT)
  ▼
MCP Gateway  https://gw-<gateway>-<account>.agentbase-gateway.aiplatform.vngcloud.vn
  ├─ /tavily   ← MCP Connector "tavily"  (outbound API Key 2LO, providerName in Identity)
  ├─ /stock    ← MCP Connector "stock"
  └─ /github   ← MCP Connector "github"  (outbound OAuth 3LO)
        │  Policy Group bound to the gateway: tools/call → ALLOW | DENY / no match ⇒ HTTP 403
        │  ("No policy allows this request"; also seen as MCP error "Request denied by policy")
        ▼
   MCP Server → Provider (Tavily, GitHub, …)
```

| Concept | What it really is | Remember |
|---|---|---|
| **MCP Gateway** | Proxy (Kong) — inbound auth + Policy enforcement | 1 gateway binds at most 1 Policy Group |
| **MCP Connector** | A **target** of the gateway; its URL is auto-generated from the gateway (observed `<gateway endpoint>/<connector name>`) | Copy the connector's **Endpoint** from the Console *Connected* tab (or `connectUrl` from the API) exactly — some include a `/mcp` suffix. The agent points at **each** connector URL; the gateway root ⇒ error |
| **Policy Group** | Set of ALLOW/DENY rules for `tools/call` | **Not bound ⇒ every tools/call is 403**; `tools/list` is always allowed |

## Choosing the tool type

| Situation | Approach |
|---|---|
| Small logic, used only by this agent | **Local tool** `app/tools/local_tools.py` |
| SaaS available in the catalog (GitHub, Slack, M365 VNG…) | **MCP Connector** from the Catalog |
| Internal system / self-written MCP server | **Custom Connector** (or target) on the gateway |
| Needs the end-user's own credentials | Connector outbound **OAuth 3LO** + gateway inbound **JWT** (`auth: user_jwt`) |

## MCP connection procedure (Coding Agent follows this order; every mutating step needs user confirmation)

1. **Upstream credential** — Secret Providers live in **Access Control** (`/agentbase-build-identity` explains the provider types; create them with `/agentbase-identity`): create an API key / OAuth2 provider (Secret Provider). Catalog connectors can use GreenNode's **Managed** secret; Custom requires creating your own OAuth App and declaring the provider. Never let the user paste secrets into chat.
2. **Gateway** — `/agentbase-gateway create` or reuse an existing gateway (`GET /gateway/api/v1/gateways`):
   - `inboundAuth`: **IAM** (agent calls with the runtime's IAM — recommended default) | **JWT** (forwards end-user identity; Policy uses `jwt:<sub>`, `principal.<claim>`) | NONE (lab only).
   - Network: **Public**, or **Private** (VPC, Subnet, Route CIDRs, Flavor, Replicas; needs VPC Peering) when the MCP servers live in your VPC or data center — `/agentbase-build` `references/private-networking.md`.
3. **Connector** — Console *AgentBase → MCP Connectors → Catalog → Connect* (or *Add Custom Connector*): pick the gateway and auth mode:

   | Auth mode | Credential | Note |
   |---|---|---|
   | OAuth 2LO / 3LO | Secret Provider (Managed / Custom) in Access Control | 3LO needs a Return URL + user consent |
   | API Key 2LO / 3LO | Secret Provider (the user enters the key in the Console/Identity — never in chat) | |
   | Inbound forward | The agent's own inbound credential | Gateway inbound ≠ NONE. ⚠ See below |
   | No authorization | — | |

   **Inbound forward** sends the MCP server the **same credential the agent used on the gateway** ([connect-a-connector](https://docs.greennode.ai/ai-stack/agent-base/mcp-connectors/connect-a-connector)). With gateway inbound **IAM** that is the agent's platform IAM token (runtime service account; locally your developer service account with `AgentBaseFullAccess`) — the server could call AgentBase APIs as your agent ⇒ **never use Inbound forward with IAM inbound**. With inbound **JWT** it forwards the end user's JWT ⇒ only towards **your own** MCP server that validates the same IdP (`/agentbase-build-mcp-server`), never a third-party server.

   List connectors + `connectUrl` via API (read): `GET https://agentbase.api.vngcloud.vn/gateway/api/v1/mcp-connectors?page=1&pageSize=50` (IAM token via `get_token.sh`). Item: `name`, `template.id` (`custom`, …), `connectUrl`, `outboundAuth{type, flow, provider}`, `gateway{name, endpoint, inboundAuth, state}`.
4. **Policy Group** — `/agentbase-policy` (REQUIRED, otherwise the agent gets 403 for every tool):
   - `principal`: **`iam:<sub>`** where `sub` = the `sub` claim in the agent's IAM token (the service account's user id — **NOT the client_id**). The agent logs this value at startup (log line starting with `IAM principal`, ending in `iam:…`). On Runtime the service account is platform-managed ⇒ deploy, read the logs (`/agentbase-monitor`) to get the principal, then add the policy.
   - `actions`: **exactly** `"<connector>__<tool>"` (2 underscores) or `"*"`. Partial wildcards are not supported (`tavily__*` ❌).
   - `resources`: `["gateway:<gateway-name>"]`.
   - Real example:
     ```json
     {"effect": "allow", "principal": "iam:<agent-sub>",
      "actions": ["tavily__tavily_search", "tavily__tavily_extract", "stock__stock_quote"],
      "resources": ["gateway:sample-mcp-gw"]}
     ```
   - Evaluation order ([policy-groups](https://docs.greennode.ai/ai-stack/agent-base/mcp-governance/policy-groups)): policies are evaluated **top-to-bottom by `order`, the first match decides** ALLOW/DENY, no match ⇒ DENY, inactive policies are skipped. Put specific DENY rules before broad ALLOW rules. (Older notes saying "deny wins within a group" are wrong.)
   - Limits: ≤ **20 policies per group**; group name 5–50 chars `[A-Za-z0-9_]` starting with a letter; 1 gateway ↔ at most 1 group (attaching another replaces it); changes apply within ~30s. Match-all principals ([policy-groups](https://docs.greennode.ai/ai-stack/agent-base/mcp-governance/policy-groups) *Principal and Wildcard Rules*): bare **`iam`** = all IAM identities, bare **`jwt`** (or `jwt:*`) = all JWT users; Console principal *All* = everyone. `iam:*` is not in the official table (`/agentbase-policy` uses it) — prefer bare `iam`. `jwt:abc*` is **not** a prefix match, it is a literal id.
5. **Declare in the agent** — `mcp_servers.json`, **one entry per connector**, URL = `connectUrl`:

```json
{
  "servers": {
    "tavily": {"transport": "streamable_http", "url": "${MCP_GATEWAY_URL}/tavily", "auth": "iam",
               "envs": ["dev", "staging", "prod"], "allow_tools": ["tavily_search"]},
    "github": {"transport": "streamable_http", "url": "${MCP_GATEWAY_URL}/github", "auth": "user_jwt"}
  }
}
```

| Field | Meaning |
|---|---|
| `url` | The connector's `connectUrl` (`${MCP_GATEWAY_URL}` = gateway endpoint, set in `.env.<env>`) |
| `auth` | **Required, no default** (an entry without it is skipped with an error log). `iam` (agent IAM token, auto-refresh — `IAMBearerAuth`) · `user_jwt` (forwards the end user's JWT, per request) · `none`. `iam` and `user_jwt` are meant for the AgentBase MCP Gateway (`*.agentbase-gateway.aiplatform.vngcloud.vn`): any other host logs a WARNING because the credential is sent there — `user_jwt` direct to a server is only OK for your own MCP server validating the same IdP |
| `headers` | Static headers, `${ENV}` expanded. A static key for a self-built MCP server: `"auth": "none", "headers": {"Authorization": "Bearer ${MY_KEY}"}` — `Authorization` together with `iam`/`user_jwt` is rejected (it would be overwritten) |
| `allow_tools` | Whitelist of the MCP server's **original** tool names (e.g. `tavily_search`). Empty = all |
| `envs`, `enabled` | Load per `APP_ENV`, temporarily disable |
| `transport` | `streamable_http` (default) or `stdio` (local process: `command`, `args`, `env`; no `auth` needed) |

`${VAR}` (braces only; `$VAR` stays literal) is expanded **after** parsing the JSON, inside string values only — any value is safe (quotes/backslashes can't break the file or inject keys) and substituted values are not re-expanded. An **unset or empty** variable makes that server a config error: it is skipped with an ERROR log naming the variable (never sent as a literal `${VAR}`/empty key); entries filtered out by `envs`/`enabled: false` are not expanded. `user_jwt` servers need the caller's JWT: in `AUTH_MODE=api_key` (incl. A2A callers using an API key) there is none ⇒ that server is skipped (`tools.collect.output.mcp_errors`); A2A calls authenticated with a user JWT keep it (the verified principal is passed through `/a2a`).

6. **Naming & HITL** — tool name in the agent = `<server>_<tool>` (e.g. `tavily_tavily_search`); policy action = `<connector>__<tool>` (e.g. `tavily__tavily_search`). Side-effect tools (GitHub create issue, Slack post, M365 send mail…) ⇒ add to `HITL_TOOLS` using the agent-side name (`github_create_issue`).
7. **Verify** — `make dev`, trace `tools.collect` lists all tools, send 1 message needing an ALLOWed tool (succeeds) and 1 for a disallowed tool (agent says it has no permission, **no retry**).

## Managing connectors after they're connected

Console *AgentBase → MCP Connectors* ([browse-connector-catalog](https://docs.greennode.ai/ai-stack/agent-base/mcp-connectors/browse-connector-catalog), [manage-connected-connectors](https://docs.greennode.ai/ai-stack/agent-base/mcp-connectors/manage-connected-connectors)):

- **Catalog** tab: search or filter, then *Connect*; *Add Custom Connector* for your own MCP server (`/agentbase-build-mcp-server`).
- **Connected** tab: name, tool count, gateway, auth method and **Endpoint**. Copy that Endpoint into `mcp_servers.json` exactly as shown.
- Detail page, *Tools & Permissions*: the tools the connector exposes. Click **Sync tools** after the MCP server adds, renames or removes tools; the agent picks up the change after its 5-min tool-list cache (or a restart).
- **After any tool change**, update in the same change set:
  - `allow_tools` in `mcp_servers.json`;
  - `HITL_TOOLS` (the agent-side name `<server>_<tool>`; unmatched patterns log a WARNING);
  - the Policy Group actions `<connector>__<tool>`.
- **Edit** to change auth or settings. **Delete cannot be undone**: first check which agents use it (their `mcp_servers.json`) and remove its policy actions.
- **Roles:** viewing is open to Members. Edit and delete need **Admin/Editor**; the role matrix lists *Tools & Integrations* create/edit/delete as Root/Admin (see `/agentbase-build` `references/iam-permissions.md`).

## Runtime behavior (asset `app/tools/mcp.py`)

- **Policy-deny guard**: a deny reaches the agent either as **HTTP 403** on `tools/call` (the documented gateway behavior, `No policy allows this request` — the adapter raises `ExceptionGroup[httpx.HTTPStatusError]` and the body never reaches the exception) or as MCP error `Request denied by policy.` (`ToolException`, which LangChain turns into text) ⇒ either way the LLM assumes a transient error and **retries many times** (observed: 3 times). `_guard_policy()` walks nested exception groups and treats a 403 status or a deny marker in the error text as a deny ⇒ `POLICY_DENIED: …` (telling the LLM not to retry) + trace event `mcp.policy_denied` (WARNING). With the guard: 1 call. Successful results are never scanned for deny text (prompt-injection safe). Any 403 on `tools/call` counts — a self-hosted server answering 403 is treated the same way. Additionally, once a connector has been denied, **every other tool of that connector is short-circuited for the current turn** without sending a request to the gateway (`reset_policy_denials()` per request; the event has `short_circuit=true`). Reason: a reasoning model was observed trying 8 denied tools in turn, taking 84s.
- Tool lists of `iam`/`none` servers are cached for 5 minutes; `user_jwt` is fetched per request.
- Connector error/timeout ⇒ that connector is skipped, span `mcp.list_tools` WARNING, `tools.collect.output.mcp_errors`. The error shown is the real (innermost) one, e.g. `HTTPStatusError: Client error '401 Unauthorized' for url …`, not `unhandled errors in a TaskGroup`. Shared (`iam`/`none`) servers that fail with a non-auth error are skipped for `MCP_FAILURE_TTL_S`; `user_jwt` servers and 401/403 are never negatively cached (one user's failure must not hide the server from others).
- Secrets in traces: span `mcp.list_tools` records the URL as `scheme://host/path` (no query string or userinfo — e.g. `?tavilyApiKey=…` is dropped) and query-string values in error messages/logs are masked to `***`. Headers are never traced.
- `IAMBearerAuth`: client_credentials to `https://iam.api.vngcloud.vn/accounts-api/v2/auth/token`, cached per `expires_in`, logs principal `iam:<sub>`.

## Troubleshooting (seen in practice)

| Symptom | Cause | Fix |
|---|---|---|
| `401` at `iam.api.vngcloud.vn/.../auth/token` | Mismatched ID/secret pair: the shell has `GREENNODE_CLIENT_ID` of another account while the secret comes from `.greennode.json` (SDK falls back per field) | `env \| grep GREENNODE_`; locally use only `.greennode.json`; `normalize_iam_env()` drops stray variables |
| `Request denied by policy` / 403 `No policy allows this request` / trace `mcp.policy_denied` | Gateway has no Policy Group bound, or principal/action mismatch | Bind a PG; principal = `iam:<sub>` (see agent log), exact action `<connector>__<tool>` |
| `MCP server 'x' skipped — environment variable(s) … unset or empty` | `${VAR}` in `mcp_servers.json` not set in the env (e.g. `MCP_GATEWAY_URL=` left empty) | Set it in `.env` / `.env.<env>` / runtime env |
| Error when pointing at the gateway root | Gateway routes by connector path | Use each connector's `connectUrl` |
| `MCP server 'x' unavailable` | IAM 401, wrong URL, connector not ACTIVE | Check span `mcp.list_tools` (status_message), `GET /mcp-connectors` |
| Tool shows in the UI but the agent doesn't see it | Filtered out by `allow_tools`, or 5-minute cache | Check the original name; restart / wait for TTL; *Sync tools* on the connector |

## Local tool — rules

- `@tool`, docstring states clearly when to use it; async if I/O; compact output.
- Never take `user_id` as a parameter. Declare `config: RunnableConfig` (LangChain injects it, the LLM doesn't see it):
  `config["configurable"]["actor_id"]` = authenticated user; `config["configurable"]["user_claims"]` = only the claims in `AUTH_FORWARD_CLAIMS` (e.g. `["email","department"]`). Sample: `whoami` in `local_tools.py`.
- RAG POC: drop `.md/.txt` into `app/knowledge/` (`KNOWLEDGE_DIR`) ⇒ `search_knowledge` registers itself, keyword search, cites `[Source: file — heading]`, span `knowledge.search`. Prod ⇒ MCP server + vector store, keep the tool name.
- POC mocks for systems without an API yet: use the final tool name, block in prod via a setting.
- External service secrets: the `app/identity.py` helpers on an inner function (`/agentbase-build-identity`), never in prod `.env`, never a parameter of the `@tool`.
- Wrap important I/O with `tracing.step("<domain>.<action>")`; side-effect ⇒ `HITL_TOOLS`; add unit tests.

Governance details & per-agent policy design examples: `references/governance.md`.

## Official docs

- [mcp-gateway](https://docs.greennode.ai/ai-stack/agent-base/mcp-governance/mcp-gateway) — gateway inbound modes (IAM / JWT / None), evaluation pipeline
- [manage-mcp-gateway](https://docs.greennode.ai/ai-stack/agent-base/mcp-governance/mcp-gateway/manage-mcp-gateway) — gateway form fields and limits
- [mcp-connectors](https://docs.greennode.ai/ai-stack/agent-base/mcp-connectors) — catalog / custom connectors
- [connect-a-connector](https://docs.greennode.ai/ai-stack/agent-base/mcp-connectors/connect-a-connector) — outbound auth: OAuth 2LO/3LO, API Key, Inbound forward, No auth; Managed vs Custom secret
- [policy-groups](https://docs.greennode.ai/ai-stack/agent-base/mcp-governance/policy-groups) — principal/action formats, conditions, first-match evaluation
- [manage-policy-groups](https://docs.greennode.ai/ai-stack/agent-base/mcp-governance/policy-groups/manage-policy-groups) — limits, attach/detach behavior
- [private-networking](https://docs.greennode.ai/ai-stack/agent-base/private-networking) — Private MCP Gateway for MCP servers in your VPC / data center
