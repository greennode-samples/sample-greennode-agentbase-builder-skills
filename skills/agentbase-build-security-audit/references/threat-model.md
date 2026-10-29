# Threat model of an AgentBase Build agent

Input for Phase 1 (reconnaissance) of the `security-audit` skill. It describes the **template**; recon verifies each
item against the project's source and records differences (renamed modules, extra tools, disabled layers).

## Assets

| Asset | Where it lives | Who may reach it |
|---|---|---|
| Conversation history, HITL state | AgentBase Memory events (`app/memory/short_term.py`), keyed by (session, user) | That user only |
| Long-term facts | Memory records, namespace per actor (`app/memory/long_term.py`) | That user only |
| A2A tasks | Task store by owner (`app/a2a/server.py`) | The calling user only |
| Side effects | Write tools: local, MCP connectors, `ask_<agent>` A2A tools | The user, after approval (`app/hitl.py`) |
| Outbound credentials | AgentBase Identity providers, per-agent or per-user (`app/identity.py`) | The agent for that user |
| Agent secrets | Runtime env file: `LLM_API_KEY`, Langfuse keys, `FEEDBACK_SIGNING_KEY`, inbound key hashes | The runtime only |
| Traces, scores, datasets | Langfuse (`app/observability/tracing.py`) | The team; masked |
| LLM spend | MaaS key with a Rate Limit (`app/llm`) | The agent, bounded |

## Trust boundaries (lower trust → higher trust)

| # | Boundary | Entry in the template | Control to verify |
|---|---|---|---|
| T1 | Caller → Runtime `/invocations` | `main.py`, `app/service.py`, `app/auth/inbound.py` | `AUTH_MODE` jwt / api_key (`none` refused outside local), issuer / audience / `AUTH_ALLOWED_CLIENT_IDS`, User-Id header must equal `sub`, `validate_user_id` / `validate_session_id`, constant-time key hash compare, `MAX_MESSAGE_CHARS` |
| T2 | Model output → tool execution | `app/tools/__init__.py`, `app/graph/builder.py`, `app/hitl.py` | `HITL_TOOLS` globs, approval bound to `interrupt_id` + (user, session) lock, `MAX_TOOL_ROUNDS`, edited arguments re-checked |
| T3 | Tool / document / web / memory content → model context | `app/tools/local_tools.py` (knowledge), `app/tools/mcp.py`, `app/memory/long_term.py`, `app/prompts/system.md` | "content is DATA" rule, output truncation, no secrets or other users' data in context |
| T4 | Agent → MCP Gateway → connectors | `app/tools/mcp.py`, `mcp_servers.json` | explicit `auth` per entry, one connectUrl per connector, Policy Group least privilege, policy-deny guard, no secrets in traced URLs |
| T5 | Caller of a self-built MCP server → its tools | `src/mcp_servers/<name>/` from `agentbase-build-mcp-server` (`auth.py`, `server.py`, `store.py`) | fail-closed verifier, audience, scopes, user from token, machine vs user tokens |
| T6 | A2A peer ↔ agent | `app/a2a/server.py`, `app/a2a/client.py`, `app/a2a/decisions.py` | auth on `POST /a2a`, task ownership, the client never relays approve words unless a human approved, peer replies treated as data |
| T7 | Agent → external services | `app/identity.py` | credentials fetched per call from Identity, per-user tokens isolated, consent link instead of blocking, nothing in tool schemas / traces |
| T8 | Frontend ↔ backend | `src/frontend` (`agentbase-build-frontend`), `CORS_ALLOW_ORIGINS` | PKCE, tokens in SecureStore, no secrets in `EXPO_PUBLIC_*`, rendered links from model output |
| T9 | Feedback endpoint | `app/service.py` (HMAC token from `FEEDBACK_SIGNING_KEY`) | token bound to trace and user, not forgeable or replayable for another user |
| T10 | Source → image → Runtime | `Dockerfile`, `.dockerignore`, `ci.yml`, `deploy/render_runtime_spec.py`, `deploy/env_file.py` | non-root image, no secrets baked in, pinned actions, per-environment secrets, settings preserved across updates |

## Seed coverage units

One unit ≈ one hunter assignment. Hypotheses are starting points, not findings.

| Unit | Companion | Hypotheses to test |
|---|---|---|
| U1 cross-user / cross-tenant data | `DATA-ISOLATION-AND-LIFECYCLE.md` | With `AUTH_TENANT_CLAIM`: a tool queries its backend without `tenant_id`, a role-gated tool reachable through HITL edit or A2A; a new tool, node or MCP call takes `user_id` / actor / namespace from arguments or body; a session id reused by user B reaches user A's checkpoint; a long-term recall query without the actor namespace; an A2A `GetTask` across owners; feedback token for another user's trace; right-to-erasure leaves data (`references/security.md`) |
| U2 inbound auth | `WEB-PROTOCOL-AND-AUTH.md` | `AUTH_MODE=none` or `api_key` reachable from untrusted callers; `AUTH_ALLOW_NO_AUDIENCE` / `IDENTITY_TRUST_ASSERTED_USER` enabled for a public endpoint; algorithm confusion (`AUTH_ALGORITHMS`), JWKS fetched from a token-controlled URL; header spoofing when the Runtime adds or forwards headers |
| U3 excessive agency | `AI-AND-LLM.md` | A write tool outside `HITL_TOOLS` (pattern typo, renamed MCP tool, new `ask_*` peer); approval replayed or resumed in another session; edited arguments widen the action; a loop of read tools that ends in a write without approval |
| U4 indirect prompt injection | `AI-AND-LLM.md` | Document / web / MCP / A2A content makes the agent call a side-effect tool, disclose another context's data, or exfiltrate through a URL, an email or a rendered markdown link; long-term memory poisoning (`remember` stores attacker text, auto-recall replays it in later sessions); the Langfuse-managed prompt drifts from `app/prompts/system.md` |
| U5 MCP trust | `AI-AND-LLM.md`, `PROTOCOLS-RPC-AND-MESSAGING.md` | Tool descriptions carrying instructions (tool poisoning); duplicate tool names shadowing a local tool; `${VAR}` expansion in `mcp_servers.json` leaking an env value into a URL or trace; an entry with weak or missing `auth`; a Policy Group broader than the spec |
| U6 self-built MCP server | `WEB-PROTOCOL-AND-AUTH.md`, `DATA-ISOLATION-AND-LIFECYCLE.md` | Auth disabled through config; machine token accepted for a per-user tool; scope check missing on a new tool; idempotency key shared across users |
| U7 A2A | `PROTOCOLS-RPC-AND-MESSAGING.md`, `AI-AND-LLM.md` | Peer reply treated as an instruction or as an approval; one key shared by several peers; timeouts letting a slow peer hold workers |
| U8 outbound credentials | `CLOUD-AND-DEPLOYMENT.md` | A third-party key in env, code or a tool parameter; a per-user token used for another user; callback URL not in `allowedReturnUrls`; credentials in traces or errors |
| U9 secrets and telemetry | `SUPPLY-CHAIN-AND-RELEASE.md` | Secrets in git history, image layers, `EXPO_PUBLIC_*`, `.agentbase-state.json`, error bodies, logs; PII unmasked in traces (`TRACE_MASK_PII`, business patterns in `tracing._mask_text`) |
| U10 cost and availability | `RESOURCE-EXHAUSTION-AND-AVAILABILITY.md` | Unbounded tool output, recursion through tools or A2A, raised `MAX_TOOL_ROUNDS` / `REQUEST_TIMEOUT_S`, no Rate Limit on the LLM key, background reflection without bounds |
| U11 build and deploy | `SUPPLY-CHAIN-AND-RELEASE.md`, `CLOUD-AND-DEPLOYMENT.md` | Unpinned actions or images, dependencies outside `uv.lock`, deploy from unprotected branches, a person's credentials in CI, Inbound Auth / IP Access Control / VPC lost on update |
| U12 frontend (if any) | `CLIENT-SIDE.md`, `DESKTOP-MOBILE-AND-LOCAL-IPC.md` | Model output rendered as active links or HTML; deep links carrying tokens; tokens outside SecureStore; `CORS_ALLOW_ORIGINS` too broad on Expo web |

## Facts outside the repository

Keep a finding `needs_validation` until one of these confirms the fact; never call the deployed endpoint to find out.

| Fact | Read it with |
|---|---|
| Runtime network mode, IP Access Control, Inbound Auth (IAM / JWT) of each version | `/agentbase-deploy` (runtime get), `.agentbase-state.json` |
| Policy Group statements and principals of the MCP Gateway | `/agentbase-policy`, `/agentbase-gateway` |
| Identity providers, `allowedReturnUrls`, which runtime is bound to which identity | `/agentbase-identity` |
| Memory store namespace template and event expiry | `/agentbase-memory` |
| LLM key Rate Limit | `/agentbase-llm` |
| CI environment protection rules and secret scopes | the user (repository settings) |

## Companions

Always: `AI-AND-LLM.md`, `DATA-ISOLATION-AND-LIFECYCLE.md`, `WEB-PROTOCOL-AND-AUTH.md`, `SUPPLY-CHAIN-AND-RELEASE.md`,
`CLOUD-AND-DEPLOYMENT.md`, `RESOURCE-EXHAUSTION-AND-AVAILABILITY.md`. With A2A or an MCP server: add
`PROTOCOLS-RPC-AND-MESSAGING.md`. With `src/frontend`: add `CLIENT-SIDE.md` (Expo web) and
`DESKTOP-MOBILE-AND-LOCAL-IPC.md` (native). Not needed for the template: `MEMORY-SAFETY-AND-BINARY.md`.
