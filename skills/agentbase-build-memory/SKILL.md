---
name: agentbase-build-memory
description: "Memory design standard for LangGraph AI agents on GreenNode AgentBase: short-term memory (AgentBaseMemoryEvents checkpointer per session), long-term memory (AgentBase Memory records, auto-recall node + remember/recall_memory tools, per-user namespace), and context compression (rolling summary + hard trim that never splits a tool call pair). Use when the agent must remember the conversation, remember user info across sessions, handle long conversations exceeding the context window, or optimize tokens. Trigger: short-term memory, long-term memory, remember conversation, remember user preferences, context compression, summarize conversation, context window, trim messages, nhớ hội thoại, nhớ sở thích user, tóm tắt hội thoại. DO NOT use to create/delete memory stores on the platform — use /agentbase-memory for that."
---

# Memory for AgentBase agents

Three layers, one file each in `app/memory/` (assets of this skill):

| Layer | File | Stored in | Scope | Written when |
|---|---|---|---|---|
| Short-term | `short_term.py` | AgentBase Memory **events** (`AgentBaseMemoryEvents`) | 1 session of 1 user | Every graph step (checkpoint) |
| Long-term | `long_term.py` | AgentBase Memory **records** (semantic search) | 1 user, all sessions | `remember` tool + the strategy's auto extraction |
| Compression | `compression.py` | `state.summary` in the checkpoint | 1 session | When exceeding `CONTEXT_MAX_TOKENS` |

## Step 1 — Create the memory store (via `/agentbase-memory`)

Call `/agentbase-memory` to create it (following that skill's HARD GATE). Suggested standard parameters, **user confirms**:

- `name`: `<project>-memory`
- `eventExpiryDuration`: 30 (days of conversation retention; per data policy)
- Strategy: `SEMANTIC` (general facts) and/or `USER_PREFERENCE`; `CUSTOM` + `customFactExtractionPrompt` when the business needs its own extraction.
- `namespaceTemplate`: **`/strategies/{memoryStrategyId}/actors/{actorId}`** — MUST match `AgentBaseLTM.namespace()` in `long_term.py`.
- `enableAutomaticMemoryRecordGeneration`: true (the platform extracts facts from events).

Write into each environment's `.env`: `MEMORY_BACKEND=agentbase`, `MEMORY_ID=<id>`, `MEMORY_STRATEGY_ID=<strategy id>`; write `memory_id` into `.agentbase-state.json`.

## Step 2 — Short-term memory

- `get_checkpointer()` returns `AgentBaseMemoryEvents(memory_id)` (dev/staging/prod) or `InMemorySaver` (local/test).
- Every invoke uses `thread_config(settings, session_id=..., user_id=...)` ⇒ `{"thread_id", "actor_id"}`. **Never build the config by hand.**
- Isolation: AgentBase keys by (thread_id, actor_id); the in-memory version joins `user::session` so another user with the same session_id can't read it (test `test_sessions_and_users_are_isolated`).
- The checkpointer is a shared singleton; building the graph per request doesn't affect it.
- HITL `interrupt()` needs a checkpointer — already provided. Note on `AgentBaseMemoryEvents`: reading state right after an interrupt may not yet see the `__interrupt__` write, and the bridge inserts a fake ToolMessage for "orphan" tool_calls ⇒ the service takes the interrupt from the graph output (see `/agentbase-build-hitl`).

## Step 3 — Long-term memory

- **Auto-recall** (node `recall`): searches by the latest question, `LTM_RECALL_LIMIT`, `LTM_MIN_SCORE`; results go into the system prompt section "Known facts about the user". Memory errors ⇒ log WARNING + trace, **never** break the chat turn.
- **Tools** `remember(fact)` / `recall_memory(query)`: the LLM saves/looks up proactively. `actor_id` comes from `RunnableConfig`, **never** a tool parameter (prevents cross-user reads & hallucination).
- **LTM is off by default** (`LTM_ENABLED=false`) — enable when the Agent Spec needs to remember users across sessions (`/agentbase-build` decision-guide §1). To disable only auto-recall: `LTM_AUTO_RECALL=false`. With LTM off: `remember`/`recall_memory` aren't registered, node `recall` is a no-op, the system prompt must **not** promise "I will remember", and dataset `evals/datasets/ltm.jsonl` doesn't run (only when enabled).
- Memory search: the query is truncated to the last `MEMORY_QUERY_MAX_CHARS` (1000, API limit) characters. Namespace search does **not** match by prefix (actually tested: parent namespace `/actors/` and another actor's prefix both return 0) ⇒ isolation relies on the exact `actor_id`.
- Platform limit of 10 concurrent requests / IAM account: the shared client (`memory_client()`) has a `MEMORY_MAX_CONCURRENCY` semaphore (default 6, divide by replica count), timeout `MEMORY_TIMEOUT_S`, backoff retry for 429/5xx.
- Right to erasure ("forget me"): delete records by user namespace + events by actor via `/agentbase-memory`.
- Adjust the system prompt: state clearly when to call `remember` (only durable info the user volunteers; don't store sensitive data if the spec forbids it).

## Step 4 — Context compression

Read `references/compression.md` to pick thresholds. Mechanism:

1. Node `compress` (start of each turn): estimated tokens > `CONTEXT_MAX_TOKENS` ⇒ summarize old messages (except the last `CONTEXT_KEEP_LAST` messages) with `get_llm("summarize")` (summarize flow, small tier by default), merge into `state.summary`, remove old messages from the checkpoint via `RemoveMessage` ⇒ small checkpoint, fast Memory reads/writes.
2. `fit_to_budget()` right before the LLM call: still above `CONTEXT_HARD_LIMIT_TOKENS` (e.g. huge tool output within the same turn) ⇒ `trim_messages(strategy="last", start_on="human")`, trace event `context.hard_trim` at WARNING.
3. The cut point **never** separates an `AIMessage(tool_calls)` from its `ToolMessage` (`_safe_cut_index`, tested).

## Checks

- `make test` (isolation, summarize, cut index).
- On dev with real memory: chat 2 turns in the same session ⇒ turn 2 remembers turn 1; switch session ⇒ no history but **still** recalls facts saved via `remember`.
- On Langfuse: span `memory.recall` has `namespace`, `facts`, `count`; `context.compress` has `tokens_before/after`, `decision`.

## SDK bugs hit during real deploys (greennode-agentbase 1.0.3) — fixed in assets

| Bug | Symptom | Correct |
|---|---|---|
| `insert_memory_records_directly_async(request=[fact])` (sample in the official skill/wizard) | `TypeError: ... argument after ** must be a mapping, not list` — `remember` tool fails, no HTTP request sent | `request=MemoryRecordInsertDirectlyRequest(memory_records=[fact])` |
| Reading search results via `r.memory` | `AttributeError: 'dict' object has no attribute 'memory'` — auto-recall swallows the error ⇒ silently remembers nothing | Results are `list[dict]` `{id, memory, score, created_at}` |

`tests/test_memory_contract.py` locks this contract. Offline tests use `InMemoryLTM` so they do **not** catch SDK bugs — always run a smoke test with real memory before deploying.

## AgentBase Memory limits & resilience (measured 2026-10)

| Limit | Behavior | Template handling |
|---|---|---|
| **10 concurrent requests / IAM account** — shared across all runtime replicas | 429 `Too many concurrent streaming requests for this user. Limit: 10` | `MEMORY_MAX_CONCURRENCY` (process-level semaphore for both the bridge's sync calls and LTM's async calls) + backoff retry |
| Search query ≤ 1000 characters | 400 | truncated to `MEMORY_QUERY_MAX_CHARS` (keeps the tail) |
| Checkpoint reads occasionally very slow (seen ~7 minutes with default timeout/retry) | request hangs | `MEMORY_TIMEOUT_S=10`, `REQUEST_TIMEOUT_S`, span `memory.checkpoint_load` |

Measured 12 parallel conversations (24 turns) on real Memory: limit 8 → 14 retries on 429, 18s; limit 6 → 9 retries, 24s; limit 4 → 2 retries, 53s. No turn failed thanks to retries. The server-side counter lags, so bursts still get 429 ⇒ **need both the limit and retries**. Scaling to many replicas: set `MEMORY_MAX_CONCURRENCY ≈ 10 / replica count`; heavy traffic ⇒ request a quota increase or split service accounts.

## Anti-patterns (forbidden)

- Storing history in a separate Redis/DB alongside the checkpointer.
- Putting the entire history into long-term memory.
- Passing `user_id`/`namespace` from the client or from the LLM into tools.
- Summarizing with the expensive main model when a secondary model is available.
