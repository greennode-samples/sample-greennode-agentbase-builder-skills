# MCP Governance — designing Gateway, Connector, Policy for multiple agents

## Recommended layout

| Level | Recommendation |
|---|---|
| Gateway | 1 gateway / environment / domain group (e.g. `search-gw-prod`, `corp-gw-prod`). Never share a gateway between dev & prod |
| Connector | 1 connector / upstream (`tavily`, `github`, `hrmcp`). Short, stable names matching `[a-z0-9]{3,50}` — the name appears in `connectUrl` **and** in policy actions `<connector>__<tool>`, which accept only letters, digits and `_`; gateway target names allow `[a-z0-9-]` ⇒ a dash (`hr-mcp`) makes per-tool actions impossible (verify once on the real API) |
| Policy Group | 1 group / gateway; each agent (principal) gets 1 ALLOW policy listing exactly the actions it needs (least privilege). **Keep policies per group few** (review effort, not a hard limit: `/agentbase-policy` says 10, the Console docs say 20; live 2026-11-09 one account accepted 100 per group and answered 429 `errors.quota.policy` at the 101st — quotas differ per account, so rely on that error, not on a number) ⇒ more agents ⇒ more gateways, or group agents that need the same actions |
| Policy design | Order-independent (the Console docs say first match wins, `/agentbase-policy` says deny wins, deny-wins was observed live 2026-11-09 — see SKILL step 4): ALLOW lists only; no catch-all DENY (no match is already DENY); a carve-out DENY goes first |
| Principal | Agent on Runtime: `iam:<sub>` of the runtime service account. User via JWT: `jwt:<sub>` or a `principal.<claim>` condition. Match-all: the official *Principal and Wildcard Rules* list bare `iam` / bare `jwt`, but **the API rejected both with 400 `errors.bad_request` at policy creation** (verified live 2026-11-09) — use `iam:*` (allowed), `jwt:*` (accepted at creation; not exercised) or `*` (allowed); *All* in the Console = everyone. `iam:<client_id>` ⇒ DENY: the principal is the `sub`. `jwt:abc*` is a literal id |
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

E2E result with the agent running as `<sub-demo-sa>`: `stock_stock_quote` ⇒ real data; `tavily_tavily_search` ⇒ `Request denied by policy` ⇒ guard returns `POLICY_DENIED`, agent does not retry (denials are per tool: the `stock_*` tools keep working in the same turn). Live 2026-11-09 (one account, one gateway) the deny was HTTP 200 with an MCP tool result `isError: true`, text `Request denied by policy.` — HTTP 403 was not observed; a bound group with zero policies and a bound-but-deleted group also denied, and **no group bound allowed the call**. The documented / earlier-run deny shape — HTTP 403 `No policy allows this request` — gets the same treatment: the guard recognizes the 403 status inside the adapter's exception group (covered offline by a fake gateway in `tests/test_mcp.py`).

## Common conditions (examples — verify before use)

The two sources disagree here too. The Console docs ([policy-groups](https://docs.greennode.ai/ai-stack/agent-base/mcp-governance/policy-groups), *Supported operators*) list 22 operators in five groups — equality `equals`/`notEquals`; comparison `lessThan`/`lessThanOrEqual`/`greaterThan`/`greaterThanOrEqual`; string `like`/`contains`/`containsAll`/`containsAny`/`startsWith`/`endsWith`; membership `in`/`has`/`hasTag`/`is`/`memberOf`; network `ipInRange`/`isIpv4`/`isIpv6`/`isLoopback`/`isMulticast` — and show keys such as `request.client_ip` / `request.timestamp.hour`, with `in` taking `"Admin,Editor"`. `/agentbase-policy` (API reference) lists **9 operators** (`equals`, `notEquals`, `in`, `like`, `contains`, `lessThan`, `lessThanOrEqual`, `greaterThan`, `greaterThanOrEqual`), keys **`context.ip`**, **`context.input.<arg>`**, **`principal.<claim>`** only, and `in` takes a **string list**. Check the live catalog `GET https://agentbase.api.vngcloud.vn/policy/api/v1/policies/condition-operators` and read `/agentbase-policy` (`references/policy-statement.md`) before creating; the API answers 400 for an unsupported operator.

| Purpose | Condition (statement JSON, API form) | Note |
|---|---|---|
| Company email only | `{"contains": {"principal.email": "@vng.com.vn"}}` | `principal.*` = JWT claims ⇒ gateway inbound JWT only |
| By role | `{"in": {"principal.role": ["Admin", "Editor"]}}` | list value, not `"Admin,Editor"` |
| Specific client IPs | `{"in": {"context.ip": ["203.0.113.10", "203.0.113.11"]}}` | CIDR ranges (`ipInRange`) appear only in the Console docs — verify |
| Limit a tool argument | `{"lessThan": {"context.input.qty": 100}}` | `tools/call` arguments; a missing argument ⇒ deny |
| Business hours | Console docs: `request.timestamp.hour`; not in the API key list | verify before relying on it. The docs' example is two conditions (`greaterThan` 9 **and** `lessThan` 17) |

Multiple conditions in one policy are combined with **AND** (all must be true for the policy to match — [policy-groups](https://docs.greennode.ai/ai-stack/agent-base/mcp-governance/policy-groups), *Conditions*). For an OR, write one policy per branch.

## Governance checklist before prod

- [ ] Prod gateway has a Policy Group **bound before any caller can reach it** (none bound = every valid IAM caller can call every tool, verified live 2026-11-09); no ALLOW policy with a match-all principal (*All* / `"*"`, `iam:*`, `jwt:*`) + `actions: ["*"]`.
- [ ] No connector on an IAM-inbound gateway uses outbound **Inbound forward**; on JWT-inbound gateways only for your own MCP servers validating the same IdP.
- [ ] `mcp_servers.json`: `auth: iam` / `user_jwt` only point at `*.agentbase-gateway.aiplatform.vngcloud.vn` (no non-gateway WARNING in the startup log, except your own JWT-validating server).
- [ ] Each prod agent has its own principal (dedicated runtime service account) and only the actions it needs.
- [ ] Side-effect tools are both restricted by Policy and go through `HITL_TOOLS`; on OAuth **3LO** connectors (consent of whoever clicked *Authorize*, shared by the project) every side-effect tool is in `HITL_TOOLS` and the OAuth scopes are minimal.
- [ ] Connector names match `[a-z0-9]{3,50}`; each `allow_tools` list equals the Policy actions of that connector for this agent.
- [ ] Policies give the same result under first-match and deny-wins (deny-wins observed; ALLOW lists, no catch-all DENY, carve-out DENY first); few per group (documented 10–20, see above).
- [ ] Upstream secrets live in Identity (Managed/Custom provider), rotated periodically.
- [ ] Traces show `mcp.policy_denied` = 0 on the main flow (if > 0 ⇒ missing policy or the prompt calls the wrong tool).
- [ ] Inbound NONE only in labs.
