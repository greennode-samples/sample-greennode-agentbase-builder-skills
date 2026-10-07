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
