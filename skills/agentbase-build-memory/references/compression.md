# Choosing context compression thresholds

`count_tokens_approximately` estimates ~4 characters/token (Vietnamese with diacritics usually costs more tokens ⇒ leave a safety margin).

| Model context window | `CONTEXT_MAX_TOKENS` (start summarizing) | `CONTEXT_HARD_LIMIT_TOKENS` (hard trim) | `CONTEXT_KEEP_LAST` |
|---|---|---|---|
| 8K | 3 000 | 5 500 | 6 |
| 32K | 12 000 (default) | 24 000 (default) | 8 (default) |
| 128K | 40 000 | 90 000 | 12 |

Principles:
- `HARD_LIMIT` ≤ context window − `LLM_MAX_TOKENS` (output) − ~10% (system prompt + tool schemas).
- `MAX_TOKENS` ≈ 40–50% of `HARD_LIMIT` to summarize early and keep per-turn cost stable.
- `KEEP_LAST` large enough to keep the last 1–2 tool call rounds intact.
- Tools returning large data ⇒ trim/summarize **inside the tool** (return only what's needed); don't rely on hard trim.

Monitor on Langfuse: filter observation `context.compress` with `decision = summarized` to see frequency; `compression_ratio` too high (> 0.8) ⇒ `KEEP_LAST` too large or the latest messages too long. Frequent `context.hard_trim` events ⇒ raise thresholds or reduce tool output.

Customizing the summary prompt: edit `SUMMARY_PROMPT` in `compression.py` to keep mandatory business fields (order ID, amount, customer ID...).

## What compression does NOT do: Memory read cost

Compression shrinks the **prompt** and each **new** checkpoint. It does not shrink what the checkpointer reads. `AgentBaseMemoryEvents` lists **every event of the session on every turn**, and old events, including the large pre-compression checkpoints, stay until `eventExpiryDuration`. Per-turn read volume therefore still grows with the number of turns:

| Offline measurement (real bridge, fake Memory client, ~1 KB messages, no tools) | Turn 2 | Turn 10 | Turn 20 | Turn 40 |
|---|---|---|---|---|
| Events read per turn | 46 | ~415 | ~880 | ~1,800 |
| KB read per turn, no compression | 43 | 828 | 2,780 | 9,918 |
| KB read per turn, compression at 1,500 tokens | 43 | 724 | 1,672 | 3,570 |

The template writes about 23 events per tool-less turn. The events API pages with an offset (`from`) of at most 5000, so after roughly 200 such turns a session can no longer be fully read. The remedy is **session rotation**: a new `session_id` seeded with the old `summary`. A shorter `eventExpiryDuration` only helps idle sessions. See SKILL.md Step 4, "Long sessions".
