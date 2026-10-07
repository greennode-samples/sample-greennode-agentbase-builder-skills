# GreenNode AgentBase platform coverage ↔ skills

Maps each item in the AgentBase docs (docs.greennode.ai/ai-stack/agent-base, 2026-10) and the platform skill set `greennode-agentbase-skills` to the `agentbase-build-*` skills. ✅ run for real · 🧪 tested offline/locally · 📄 guidance only · ⛔ out of scope.

| AgentBase item | Platform skill (operations) | Build skill (code/architecture) | Status |
|---|---|---|---|
| Agent Runtime (create/update/version/endpoint) | `/agentbase-deploy` | `agentbase-build-deploy`, `-scaffold` (contract 8080, `/health`) | ✅ deployed v1→v6, rollback via version |
| Runtime Insight (logs, metrics, traces passthrough) | `/agentbase-monitor` (`runtime.sh logs/traces`) | `agentbase-build-tracing` (Langfuse for LLM level) | ✅ logs · 📄 traces backend params not documented yet |
| OpenClaw / Marketplace | `/agentbase-deploy` Part 3 | `agentbase-build` (when to use it instead of building) | ⛔ use the platform skill directly |
| Container Registry | `/agentbase-deploy` (`cr.sh`) | `-deploy` (amd64 image, no secrets in the image) | ✅ |
| Access Control / Identity (API key, OAuth2, delegated) | `/agentbase-identity` | `agentbase-build-auth` (outbound decorators), `-mcp-server` (OAuth) | 📄 decorators not run for real yet |
| MCP Gateway | `/agentbase-gateway` | `agentbase-build-mcp` | ✅ real calls through the gateway, IAM inbound |
| MCP Connectors (catalog + custom) | — (API `GET /gateway/api/v1/mcp-connectors` not yet in the platform skill) | `agentbase-build-mcp`, `-mcp-server` (custom connector) | ✅ API read + real connector calls |
| Policy Groups | `/agentbase-policy` | `agentbase-build-mcp` (principal `iam:<sub>`, action, deny guard) | ✅ real ALLOW/DENY · ⏳ policy not yet granted to the runtime principal |
| Memory (events, records, strategies) | `/agentbase-memory` | `agentbase-build-memory`, `-hitl` | ✅ short/long-term, isolation, HITL on real memory |
| AI Platform LLM (MaaS, API key, models) | `/agentbase-llm` | `agentbase-build-llm` (tiers, flows, fallback, routing) | ✅ 10 models, real fallback, prompt cache |
| Protect & Govern — Rate limit | console / `/agentbase-llm models rate-limit` | `-llm` (429 ⇒ fallback) | 📄 |
| Sidecar LLM Proxy (`localhost:18080`) | — | `-llm` (needs verification) | 📄 not verified |
| Private Networking (VPC mode) | `/agentbase-deploy` (vserver.sh) | `-deploy`, `-mcp` (PRIVATE gateway) | 📄 |
| Team & Permissions / Service Accounts | console / `/agentbase` | `-deploy` (dedicated SA for CI, least privilege) | 📄 |
| GreenNode CLI / GreenNode MCP | docs | — (alternative to scripts when needed) | ⛔ |
| Teardown | `/agentbase-teardown` | `-deploy` (cleanup after tests) | 📄 |
| (off-platform) Langfuse | — | `agentbase-build-tracing`, `-eval` | ✅ self-hosted server v4.49: trace tree, cost, cache, reasoning, TTFT, prompt version, scores v3, dataset + experiment |
| (off-platform) A2A | — (the platform has no A2A gateway) | `agentbase-build-a2a` | ✅ card, auth, isolation on runtime |
| (off-platform) RN frontend | — | `agentbase-build-frontend` | 🧪 tsc + Android bundle · ⏳ OIDC with a real IdP |

## Platform bugs/differences found during real runs (handled in the skills)

1. The Runtime endpoint has **no auth** ⇒ the agent must use `jwt`/`api_key`.
2. `greennode-agentbase` 1.0.3: `insert_memory_records_directly` needs a `MemoryRecordInsertDirectlyRequest` (the official sample passes a list ⇒ TypeError); `search` returns `list[dict]`.
3. `AgentBaseMemoryEvents`: reading state right after an interrupt may miss the `__interrupt__` write, and the bridge inserts a fake ToolMessage ⇒ take the interrupt from the graph output, treat `next=('approval',)` as pending approval.
4. SDK `IAMCredentials` falls back per field ⇒ mismatched ID/secret pair ⇒ 401 ⇒ `.greennode.json` is the only source locally.
5. Each MCP Connector has its own `connectUrl` (`<gateway>/<connector>`); the gateway root is unusable; policy principal = `iam:<sub>` (not client_id); a gateway without a Policy Group ⇒ 403 for everything; policy deny comes back as a ToolException ⇒ the LLM retries unless there is a guard.
6. GreenNode IAM does not publish JWKS ⇒ IAM tokens cannot be used as inbound JWT.
7. Memory API: search query ≤ 1000 characters (400 if longer); checkpoint reads sometimes hang ~7 minutes with the SDK's default timeout/retry ⇒ 10s timeout, 2 retries, `REQUEST_TIMEOUT_S`.
8. MaaS sometimes returns 503 (glm-5.3-flash) ⇒ a fallback model is mandatory for prod.
9. Memory API limits 10 concurrent requests per IAM account (429), shared across all replicas ⇒ `MEMORY_MAX_CONCURRENCY` + retry backoff.
10. Memory API concatenates `actorId`/`sessionId` straight into the path ⇒ a Session-Id containing `../` can point to another actor ⇒ the template validates Session-Id (`^[A-Za-z0-9][A-Za-z0-9-]{0,127}$`) at every entry point (invocations, A2A, eval).
11. Memory record search by namespace is an **exact match**, not a prefix match (tested: parent namespace `/actors/` and another actor's prefix return 0 results) ⇒ no LTM leakage via parent namespaces.

## Open items (need the user's environment/decisions)

- Grant a Policy Group to the runtime's principal so the agent can use real MCP tools.
- Langfuse for runtimes on AgentBase (needs a public/VPC URL; the local instance only serves locally running agents).
- A real IdP (Keycloak/Auth0…) to test the JWT flow + the frontend's OIDC PKCE.
- Gateway behavior for OAuth 3LO when the user has not consented yet (payload returns an authorization URL).
- `DatabaseTaskStore` for A2A when running multiple replicas.
