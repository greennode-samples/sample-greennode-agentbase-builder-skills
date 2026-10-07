---
name: agentbase-build-memory
description: "Memory design standard for LangGraph AI agents on GreenNode AgentBase: short-term memory (AgentBaseMemoryEvents checkpointer per session), long-term memory (AgentBase Memory records, auto-recall node + remember/recall_memory tools, per-user namespace), and context compression (rolling summary + hard trim that never splits a tool call pair). Use when the agent must remember the conversation, remember user info across sessions, handle long conversations exceeding the context window, or optimize tokens. Trigger: short-term memory, long-term memory, remember conversation, remember user preferences, context compression, summarize conversation, context window, trim messages, nhớ hội thoại, nhớ sở thích user, tóm tắt hội thoại. DO NOT use to create/delete memory stores on the platform — use /agentbase-memory for that."
---

# Memory for AgentBase agents

Three layers, one file each in `app/memory/` (assets of this skill):

| Layer | File | Stored in | Scope | Written when |
|---|---|---|---|---|
| Short-term | `short_term.py` | AgentBase Memory **events** (`AgentBaseMemoryEvents`) | 1 session of 1 user | Every graph step (checkpoint) |
| Long-term | `long_term.py` | AgentBase Memory **records** (semantic search) | 1 user, all sessions | `remember` tool. Platform auto-extraction only if verified on dev (Step 3) |
| Compression | `compression.py` | `state.summary` in the checkpoint | 1 session | When exceeding `CONTEXT_MAX_TOKENS` |

## Step 1 — Create the memory store (via `/agentbase-memory`)

Call `/agentbase-memory` to create it (following that skill's HARD GATE). Suggested standard parameters, **user confirms**:

- `name`: `<project>-memory`
- `eventExpiryDuration`: 30 (days of conversation retention, allowed 1–365; per data policy). Name ≤ 50 chars `^[a-zA-Z0-9._-]*$`. This does not bound a session that stays active, see "Long sessions" (Step 4).
- Strategy: **one per agent** with this template: `SEMANTIC` (general facts), **or** `USER_PREFERENCE`, **or** `CUSTOM` + `customFactExtractionPrompt` when the business needs its own extraction. The app reads and writes a single namespace, `/strategies/{MEMORY_STRATEGY_ID}/actors/{actorId}`, so records produced by a second strategy on the same store are **never recalled**. If you need two, extend `AgentBaseLTM.search` to query each strategy's namespace and merge by score (the strategy id becomes a list in config), and decide which namespace `remember` writes to. The template does not do this.
- `namespaceTemplate`: **`/strategies/{memoryStrategyId}/actors/{actorId}`** — MUST match `AgentBaseLTM.namespace()` in `long_term.py`.
- `enableAutomaticMemoryRecordGeneration`: true. The memory docs describe extraction from **conversational** events, but this template writes only binary checkpoint events. Verify it on dev (Step 3).

Write into each environment's `.env`: `MEMORY_BACKEND=agentbase`, `MEMORY_ID=<id>`, `MEMORY_STRATEGY_ID=<strategy id>` (the id returned by `GET /memories/{memoryId}/long-term-memory-strategies` — see [memory docs](https://docs.greennode.ai/ai-stack/agent-base/memory); the app **refuses to start** with `LTM_ENABLED=true` and `MEMORY_STRATEGY_ID` empty/`default`, because a wrong id makes recall silently return nothing); write `memory_id` into `.agentbase-state.json`.

## Step 2 — Short-term memory

- `get_checkpointer()` returns `AgentBaseMemoryEvents(memory_id)` (dev/staging/prod) or `InMemorySaver` (local/test).
- Every invoke uses `thread_config(settings, session_id=..., user_id=...)` ⇒ `{"thread_id", "actor_id"}`. **Never build the config by hand.**
- Isolation: AgentBase keys by (thread_id, actor_id); the in-memory version joins `user::session` so another user with the same session_id can't read it (test `test_sessions_and_users_are_isolated`).
- The checkpointer is a shared singleton; building the graph per request doesn't affect it.
- HITL `interrupt()` needs a checkpointer — already provided. Note on `AgentBaseMemoryEvents`: reading state right after an interrupt may not yet see the `__interrupt__` write, and the bridge inserts a fake ToolMessage for "orphan" tool_calls ⇒ the service takes the interrupt from the graph output (see `/agentbase-build-hitl`).

## Step 3 — Long-term memory

- **Auto-recall** (node `recall`): searches by the latest question, `LTM_RECALL_LIMIT`, `LTM_MIN_SCORE`; results go into the system prompt section "Known facts about the user". Memory errors ⇒ log WARNING + trace, **never** break the chat turn. The search API accepts `limit` 5–200: `LTM_RECALL_LIMIT` outside that range is refused at startup (`Settings`); `AgentBaseLTM.search` also clamps defensively for direct callers and cuts results to the requested limit.
- **Automatic extraction is NOT verified for this template.** The checkpointer (`AgentBaseMemoryEvents`) stores only **binary** events (serialized checkpoints). The memory docs describe facts as extracted from **conversational** events (`type: conversational`, `role`, `message`), and whether binary events are used is not documented. Do not promise automatic extraction until you have checked it. On dev, chat a few turns that state a durable fact without the LLM calling `remember`, wait a few minutes, then list the records of `/strategies/<id>/actors/<user>` with `/agentbase-memory`. If no records appear, `remember` is the only writer. When the spec needs automatic extraction, also write each completed turn (not a HITL interrupt) as conversational events, for example in `app/service.py` after the graph returns:

  ```python
  from greennode_agentbase.memory.models import EventCreateRequest, EventPayload
  from app.memory.long_term import with_retry
  from app.memory.short_term import memory_client

  client = memory_client(settings)  # shared client: same concurrency limiter as the checkpointer
  try:
      for role, text in (("user", user_text), ("assistant", answer_text)):
          req = EventCreateRequest(payload=EventPayload(type="conversational", role=role, message=text[:100_000]))
          await with_retry(
              lambda r=req: client.create_event_async(
                  id=settings.memory_id, actorId=user_id, sessionId=session_id, request=r
              ),
              max_retries=settings.memory_max_retries, backoff_s=settings.memory_retry_backoff_s,
              what="memory.conversation_event",
          )
  except Exception:  # noqa: BLE001 — extraction input must never fail the turn
      log.warning("conversational event write failed", exc_info=True)
  ```

  The bridge skips non-binary events when it loads checkpoints, but they still add 2 events per turn to the session (see "Long sessions"). Alternatives: `memory-records:generate-from-session` or `generate-from-content` (see `/agentbase-memory`). Re-run the dev check afterwards.
- **Tools** `remember(fact)` / `recall_memory(query)`: the LLM saves/looks up proactively. `actor_id` comes from `RunnableConfig`, **never** a tool parameter (prevents cross-user reads & hallucination).
- **LTM is off by default** (`LTM_ENABLED=false`) — enable when the Agent Spec needs to remember users across sessions (`/agentbase-build` decision-guide §1). To disable only auto-recall: `LTM_AUTO_RECALL=false`. With LTM off: `remember`/`recall_memory` aren't registered, node `recall` is a no-op, the system prompt must **not** promise "I will remember", and dataset `evals/datasets/ltm.jsonl` doesn't run (only when enabled).
- Memory search: the query is truncated to the last `MEMORY_QUERY_MAX_CHARS` (1000, API limit) characters. Namespace search does **not** match by prefix (actually tested: parent namespace `/actors/` and another actor's prefix both return 0) ⇒ isolation relies on the exact `actor_id`.
- Platform limit of 10 concurrent requests / IAM account: the shared client (`memory_client()`) has a `MEMORY_MAX_CONCURRENCY` limiter (default 6; divide by the replica count) and timeout `MEMORY_TIMEOUT_S`. The limiter is one **FIFO** queue shared by the bridge's sync calls (executor threads) and LTM's async calls. Each released permit goes to the oldest waiter, so an async recall waits its turn, not the whole burst. It is cancellation-safe. 429/5xx are retried with exponential backoff: the bridge does this for checkpoint calls, and `with_retry` does it for LTM search/save (`MEMORY_MAX_RETRIES`, `MEMORY_RETRY_BACKOFF_S`, capped at 2 s, with jitter). The backoff stops at once if the request is cancelled. Other errors (400/401/403/404, network) are not retried.
- Right to erasure ("forget me"): delete records by user namespace + events by actor via `/agentbase-memory`.
- Adjust the system prompt: state clearly when to call `remember` (only durable info the user volunteers; don't store sensitive data if the spec forbids it).

## Step 4 — Context compression

Read `references/compression.md` to pick thresholds. Mechanism:

1. Node `compress` (start of each turn): estimated tokens > `CONTEXT_MAX_TOKENS` ⇒ summarize old messages (except the last `CONTEXT_KEEP_LAST` messages) with `get_llm("summarize")` (summarize flow, small tier by default), merge into `state.summary`, remove old messages from the checkpoint via `RemoveMessage`. This keeps the **prompt** and each **new** checkpoint small. It does **not** make Memory reads cheap, see "Long sessions" below.
2. `fit_to_budget()` right before the LLM call: still above `CONTEXT_HARD_LIMIT_TOKENS` (e.g. huge tool output within the same turn) ⇒ `trim_messages(strategy="last", start_on="human")`, trace event `context.hard_trim` at WARNING.
3. The cut point **never** separates an `AIMessage(tool_calls)` from its `ToolMessage` (`_safe_cut_index`, tested).
4. If the old part is too big for the summarizer (`CONTEXT_HARD_LIMIT_TOKENS`), only the **oldest prefix that fits** is summarized and removed; the rest stays and is compressed on a later turn — messages are never deleted without being summarized (sweep test over message sizes).

### Long sessions (event growth): rotate sessions

- The bridge writes every graph step as binary events: about **23 events per turn** without tools, more with tool rounds and HITL. On **every turn** it lists **all** events of the session (pages of 100 per request; the template lists them twice per turn). `RemoveMessage` does not delete events; old events stay until `eventExpiryDuration`. Read volume and latency therefore grow linearly with the session's length, with or without compression.
- Measured offline (real bridge + a fake Memory client, 40 turns, about 1 KB per message): turn 2 reads 46 events (43 KB). Turn 40 reads about 1,800 events in about 20 list requests: **3.6 MB with compression, 9.9 MB without**. A review on real Memory measured 207 KB → 2.6 MB per turn over 40 turns. Compression only slows the growth.
- **Ceiling:** the events API pages with an offset (`from`) of at most **5000**, so a session with more than about 5,000 events cannot be fully read. That is roughly 200 tool-less turns, fewer with tools. Past it, checkpoint loads fail or miss older channel data. The exact behaviour has not been verified on the platform, so don't get close to the ceiling.
- **Mitigation — session rotation:** start a new `session_id` well before the ceiling, for example every 50–100 turns, per day, or per topic. Carry the context over by seeding the new thread with the old `summary` (`await graph.aupdate_state(thread_config(..., session_id=new_id), {"summary": old_summary})`), or rely on LTM for cross-session facts. Rotation is usually driven by the client, which owns `session_id` (frontend: "new conversation").
- A shorter `eventExpiryDuration` only bounds sessions that go idle. The bridge writes a channel's value only when it changes, so in a session that stays active longer than the expiry, values not rewritten since (e.g. `summary`) can expire under the latest checkpoint. Prefer rotation, and verify on dev before relying on expiry.

## Checks

- `make test` (isolation, summarize, cut index, Memory limiter fairness/cancellation, LTM 429/5xx retry, search limit range).
- On dev with real memory: chat 2 turns in the same session ⇒ turn 2 remembers turn 1; switch session ⇒ no history but **still** recalls facts saved via `remember`.
- On dev: check whether records appear **without** `remember` (Step 3, automatic extraction). Report the result, and don't promise it in the system prompt until it is confirmed.
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
| **10 concurrent requests / IAM account** — shared across all runtime replicas | 429 `Too many concurrent streaming requests for this user. Limit: 10` | `MEMORY_MAX_CONCURRENCY` (process-level FIFO limiter shared by the bridge's sync calls and LTM's async calls) + backoff retry (bridge for checkpoints, `with_retry` for LTM search/save) |
| Search query ≤ 1000 characters | 400 | truncated to `MEMORY_QUERY_MAX_CHARS` (keeps the tail) |
| Search `limit` 5–200 | outside the documented range | `LTM_RECALL_LIMIT` validated 5–200 at startup; `AgentBaseLTM.search` clamps defensively, results cut to the requested limit |
| Events per session: pagination offset (`from`) ≤ 5000 | a longer session can't be fully loaded | session rotation (Step 4, "Long sessions") |
| Checkpoint reads occasionally very slow (seen ~7 minutes with default timeout/retry) | request hangs | `MEMORY_TIMEOUT_S=10`, `REQUEST_TIMEOUT_S`, span `memory.checkpoint_load` |

Measured 12 parallel conversations (24 turns) on real Memory: limit 8 → 14 retries on 429, 18s; limit 6 → 9 retries, 24s; limit 4 → 2 retries, 53s. No turn failed thanks to retries. The server-side counter lags, so bursts still get 429 ⇒ **need both the limit and retries**. Scaling to many replicas: set `MEMORY_MAX_CONCURRENCY ≈ 10 / replica count`; heavy traffic ⇒ request a quota increase or split service accounts.

## Anti-patterns (forbidden)

- Storing history in a separate Redis/DB alongside the checkpointer.
- Putting the entire history into long-term memory.
- Passing `user_id`/`namespace` from the client or from the LLM into tools.
- Summarizing with the expensive main model when a secondary model is available.

## Official docs

- [memory](https://docs.greennode.ai/ai-stack/agent-base/memory) — memory stores, strategies (SEMANTIC / USER_PREFERENCE / CUSTOM), namespace template, LangGraph bridge, limits
- [reference](https://docs.greennode.ai/ai-stack/agent-base/reference) — memory REST paths and pagination
