# Decision guide — when to enable which component

Principle: **build minimal by default; every enabled component must have a reason recorded in the Agent Spec.** The template ships code for every component, but optional components are all disabled via env. Every added layer brings latency, cost, attack surface and operational work.

## 0. Always present (no decision needed)

| Component | Reason |
|---|---|
| Runtime contract, `service.py`, standard graph | Common skeleton |
| Inbound auth (`jwt` / `api_key`) | The Runtime endpoint is public, with no platform auth |
| Short-term memory (checkpointer) | Multi-turn conversations; HITL also needs a checkpointer |
| Compression | Insurance for long conversations; costs nothing until the threshold is exceeded |
| Langfuse tracing (can be sampled) | An agent cannot be debugged without traces |
| Offline eval (`make eval`) | Without eval you can't tell whether a change is better or worse |

## 1. Long-term memory — `LTM_ENABLED` (default **off**)

**Enable when** the value comes from *remembering the user across sessions*:
- Personal assistant, customer support with returning-customer profiles, coach/tutor tracking progress, sales assistant remembering customer needs.
- Users are really logged in (stable JWT `sub`). Do not use for anonymous users: an unstable `user_id` produces junk memory.

**Do not enable when**:
- FAQ / document lookup: answers do not depend on the user.
- One-off tasks (batch, stateless API), or an agent shared by a whole department under one account.
- There is no data policy yet: you need user consent, a retention period (`eventExpiryDuration`), a right to deletion, and a list of data that must never be stored (health, finance, passwords…). State it explicitly in the system prompt.

**Choosing a strategy**: `USER_PREFERENCE` (preferences, form of address, language) → `SEMANTIC` (events/facts) → `CUSTOM` (only when specific business fields must be extracted, with a prompt).
**Auto-recall vs tool**: `LTM_AUTO_RECALL=true` when every turn needs personalization. If it is only needed occasionally, disable auto-recall and let the LLM call `recall_memory` itself, saving 1 search per turn.

## 2. Loops

| Loop | Default | When to adjust |
|---|---|---|
| **Tool loop** (agent ⇄ tools) | `MAX_TOOL_ROUNDS=8` | Increase for multi-step investigative agents (ops, research). Decrease (2–3) for simple bots to stop costly loops |
| **Self-eval / reflection** (`REFLECTION_ENABLED`) | **off** | See criteria below |
| **Offline eval loop** (`make eval`) | always on | Run on every PR / before deploy; add items from 👎 traces |
| **Online eval** (`user_feedback`, judge sampling) | feedback always on | Enable managed LLM judge on Langfuse once there is real traffic |

**Enable reflection when all of the following hold**:
1. Mistakes are costly (financial figures, regulation citations, generated SQL/code that actually runs).
2. "Correct" is checkable with clear criteria (matches tool output, all fields present, right format).
3. 1–2 extra LLM calls and roughly 1–3 seconds of extra latency per turn are acceptable.
4. Measured with `make eval`, reflection noticeably increases `pass_rate` compared to off.

**Do not enable when**: plain chat or small talk; HITL already reviews the output (the reviewer is the check); the UX needs fast streaming (`reset` discards text already displayed); there is no eval to prove its value. Keep `REFLECTION_MAX_RETRIES` at 1; 2 is rarely needed.

## 2b. Model selection per flow (tier) & fallback

| Decision | Default | When to change |
|---|---|---|
| One model for everything | ✔ (`LLM_MODEL`) | POC, low traffic |
| Separate small tier for `summarize`/`router` | Recommended when compression/routing is used | Cuts cost with almost no quality impact |
| `agent` uses reasoning | ✖ | Every question needs reasoning (financial analysis, legal, SQL) |
| Adaptive routing | ✖ | Mixed traffic: many simple questions + a share of hard ones; measured with eval |
| Fallback model | **Recommended for prod** | Always have at least 1 fallback from a different model family for the `large` tier |

Details and measurements: `/agentbase-build-llm`.

## 3. Human-in-the-loop — `HITL_TOOLS` (default empty)

**Mandatory** for tools with any of these traits:

| Trait | Examples |
|---|---|
| Irreversible | Delete data, cancel orders, send email/messages, publish |
| Financial | Payments, refunds, placing orders, creating invoices |
| Permissions / security | Granting access, changing prod config, rotating secrets |
| Acting for the user with third parties | Booking meetings with customers, commenting on GitHub/Slack |
| High cost | Running heavy jobs, calling pay-per-call APIs |

**Not needed**: read-only tools (search, lookup, report); writes to drafts the user will review; `remember` (usually only for demos; enable only when the data policy requires user confirmation before storing).

**Tuning**: use dynamic thresholds instead of always asking (e.g. only ask when the amount > X, or when sending to > N recipients), edit `requires_approval()`. If the approver is not the requester (e.g. a manager approves), a separate approval flow is needed (see `/agentbase-build-hitl`). HITL **complements** the Policy Group, it does not replace it: Policy statically blocks "who may call which tool", HITL decides each individual call.

## 4. Tools: local vs MCP vs building an MCP server

| Question | Choice |
|---|---|
| Pure logic, used only by this agent, no secrets needed | Local tool |
| SaaS available in the connector catalog (GitHub, Slack, M365) | MCP Connector (no code) |
| Internal system, shared by many agents, needs policy/audit | **Build an MCP server** (`/agentbase-build-mcp-server`) + Custom Connector |
| Needs per-user data in the target system | MCP server using OAuth 3LO or inbound JWT forwarding so `current_user()` comes from the token |
| Internal document lookup (RAG), POC / ≤ a few dozen files | Local `search_knowledge`: drop `.md/.txt` into `app/knowledge/` (auto-registered when files exist) |
| Prod RAG: many documents, semantic search, frequent updates | MCP server with a vector store, **keep the tool name `search_knowledge`** and the `[Source: …]` format |
| POC while the internal system has no API yet | Local mock adapter, **use the final tool name** (keeps `HITL_TOOLS`/eval), blocked in prod via a setting |
| The system is only reachable inside your VPC or data center | MCP server + **Private MCP Gateway** (VPC Peering; VPN Site-to-Site for on-prem) — `references/private-networking.md`. Private Runtime only if agent code itself must call the internal API |

## 5. A2A — `A2A_ENABLED` (default **off**)

Only build the A2A layer when **at least one** of these holds:
1. **Different owner**: the target agent is developed and operated by another team/unit, with its own roadmap and releases.
2. **Different security/data boundary**: the target agent holds data the calling agent may not access directly (HR, finance), so only results are exchanged.
3. **Reuse**: several products or agents need the same capability (e.g. a legal agent shared by both customer support and sales).
4. **Independent scaling/deploy is needed**, or long-running (minutes) tasks must run separately from the chatting agent.
5. Interaction with agents **outside the organization** or on **another framework** that supports A2A.

**Do not use A2A when**: same team, same repo (use a node/subgraph — faster and easier to debug); the job is just a function with a clear schema (use an MCP tool); latency matters (each A2A hop adds a full LLM round at the target agent); there is no real need yet ("might be needed later" is not a reason).

**Only expose the server** (`A2A_ENABLED=true`) when there is at least one concrete consumer. **Only add a client** (`a2a_agents.json`) when there is a concrete target agent. The two are independent.

## 6. React Native frontend

Include it when end users interact directly via an app/web. Not needed when the agent is called by another service (API, A2A, chat-channel webhook); use `api_key` or a BFF then.

## 7. Auth mode

| Caller | Mode |
|---|---|
| End-user app | `jwt` (the organization's IdP) |
| Internal service / BFF / other agent calling via A2A | `api_key` (one key per caller) |
| Local dev | `none` |

## 8. Use-case patterns

| Use case | ST mem | LT mem | Compression | Reflection | HITL | Tools | A2A | UI |
|---|---|---|---|---|---|---|---|---|
| FAQ / internal document lookup | ✔ | ✖ | ✔ | Consider (regulation citations) | ✖ | Retriever (MCP/local) | ✖ | Optional |
| Personal assistant / returning-customer support | ✔ | ✔ `USER_PREFERENCE` | ✔ | ✖ | Send/book tools | Connector (M365, CRM) | ✖ | ✔ |
| Ops agent (tickets, config, deploy) | ✔ | ✖ | ✔ (large tool output) | ✖ (HITL already) | **✔ every write tool** | Internal MCP server + Policy | ✖ | Optional |
| Data analysis / SQL generation | ✔ | Optional | ✔ | **✔** | For write queries | Read-only DB MCP | ✖ | ✔ |
| Orchestrating many domains owned by many teams | ✔ | Optional | ✔ | ✖ | Per domain | Few | **✔ client** | ✔ |
| Shared expert capability (legal, pricing) | ✔ | ✖ | ✔ | Optional | ✖ | Per domain | **✔ server** | ✖ |
| Batch / background job (no chat) | New session per job | ✖ | ✔ | Optional | Async approval or ✖ | Per job | ✖ | ✖ |

## 9. Cost of each layer (to explain to the user)

| Layer | Latency per turn | Cost | Operations |
|---|---|---|---|
| LTM auto-recall | +1 memory search (~100–300ms) | Memory API | Data policy, handling deletion requests |
| Adaptive routing | +1 small call (~1–3s on MaaS) | Low | Tune the router prompt per domain |
| Fallback model | 0 when the primary is healthy; +1 attempt on error | Only on error | Keep the fallback enabled & evaluated |
| Compression (when triggered) | +1 LLM call (secondary model) | Low with a cheap model | Tune the threshold |
| Reflection | +1–2 LLM calls | ×1.5–3 tokens | Write criteria, measure with eval |
| HITL | Waiting for the approver | — | Approval UI, timeout |
| MCP via Gateway | +1 network hop per tool call | Gateway flavor | Policy, connectors, secrets |
| A2A (per target agent) | +1 full agent round | Target agent's LLM | Cross-team contract, versioning, auth keys |
