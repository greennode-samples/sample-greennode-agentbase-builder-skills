# MCP Governance — designing Gateway, Connector, Policy for multiple agents

## Recommended layout

| Level | Recommendation |
|---|---|
| Gateway | 1 gateway / environment / domain group (e.g. `search-gw-prod`, `corp-gw-prod`). Never share a gateway between dev & prod |
| Connector | 1 connector / upstream (tavily, github, hr-mcp). Short, stable names — the name appears in `connectUrl` **and** in policy actions |
| Policy Group | 1 group / gateway; each agent (principal) gets 1 ALLOW policy listing exactly the actions it needs (least privilege) |
| Principal | Agent on Runtime: `iam:<sub>` of the runtime service account. User via JWT: `jwt:<sub>` or a `principal.<claim>` condition. Match-all (official *Principal and Wildcard Rules*): bare `iam` = every IAM identity, bare `jwt` / `jwt:*` = every JWT user, *All* = everyone; `iam:*` is not in the official table (prefer `iam`), `jwt:abc*` is a literal id |
| Connector outbound auth | Prefer API Key / OAuth from a Secret Provider. **Inbound forward** re-sends the agent's gateway credential to the MCP server: never on an IAM-inbound gateway (that is the agent's platform IAM token); on a JWT-inbound gateway only towards your own server validating the same IdP |

## Example (real observed structure, IDs replaced with placeholders)

Gateway `sample-mcp-gw` (inbound IAM) has 2 connectors `tavily`, `stock` (outbound API Key 2LO). Policy group `sample-gw-policy` — described as "deny by default", containing these policies:

```json
[
  {"name": "allow-travel-tavily",
   "statement": {"effect": "allow", "principal": "iam:<sub-travel-agent>",
                 "actions": ["tavily__tavily_search", "tavily__tavily_extract", "tavily__tavily_crawl",
                             "tavily__tavily_map", "tavily__tavily_research"],
                 "resources": ["gateway:sample-mcp-gw"]}},
  {"name": "allow-stock-demo",
   "statement": {"effect": "allow", "principal": "iam:<sub-demo-sa>",
                 "actions": ["stock__market_top_stocks", "stock__top_gainers", "stock__top_losers",
                             "stock__most_active", "stock__stock_quote"],
                 "resources": ["gateway:sample-mcp-gw"]}}
]
```

E2E result with the agent running as `<sub-demo-sa>`: `stock_stock_quote` ⇒ real data; `tavily_tavily_search` ⇒ `Request denied by policy` ⇒ guard returns `POLICY_DENIED`, agent does not retry. The documented deny shape — HTTP 403 `No policy allows this request` (no rule matches, or no Policy Group bound) — gets the same treatment: the guard recognizes the 403 status inside the adapter's exception group (covered offline by a fake gateway in `tests/test_mcp.py`).

## Common conditions

| Purpose | Condition |
|---|---|
| Company email only | `contains principal.email "@vng.com.vn"` (gateway inbound JWT) |
| Business hours only | `greaterThan request.timestamp.hour 9` AND `lessThan request.timestamp.hour 17` |
| Internal IPs only | `ipInRange request.client_ip 10.0.0.0/8` |
| By role | `in principal.role "Admin,Editor"` |

Exact keys/operators: read `/agentbase-policy` (`references/policy-statement.md`) before creating.

## Governance checklist before prod

- [ ] Prod gateway has a Policy Group; no ALLOW policy with a match-all principal (*All* / `"*"`, `iam`, `jwt`, `jwt:*`) + `actions: ["*"]`.
- [ ] No connector on an IAM-inbound gateway uses outbound **Inbound forward**; on JWT-inbound gateways only for your own MCP servers validating the same IdP.
- [ ] `mcp_servers.json`: `auth: iam` / `user_jwt` only point at `*.agentbase-gateway.aiplatform.vngcloud.vn` (no non-gateway WARNING in the startup log, except your own JWT-validating server).
- [ ] Each prod agent has its own principal (dedicated runtime service account) and only the actions it needs.
- [ ] Side-effect tools are both restricted by Policy and go through `HITL_TOOLS`.
- [ ] Upstream secrets live in Identity (Managed/Custom provider), rotated periodically.
- [ ] Traces show `mcp.policy_denied` = 0 on the main flow (if > 0 ⇒ missing policy or the prompt calls the wrong tool).
- [ ] Inbound NONE only in labs.
