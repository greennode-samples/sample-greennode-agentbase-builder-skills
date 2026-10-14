# Detailed trace schema & how to use it for debugging

## Observation types (Langfuse v4)

Pick the most specific type ([Langfuse observation types](https://langfuse.com/docs/observability/features/observation-types)): evaluators filter on it and every type except `span`/`event`/`generation` makes Langfuse treat the trace as agentic (Agent Graph).

| as_type | Used for | Set by |
|---|---|---|
| `agent` | Root `agent.invoke`; remote A2A agents `a2a.<agent>`; in-graph sub-agents `subagent.<role>` — nothing else | `trace_request`, `step(as_type="agent")`, `_CHAIN_TYPES` |
| `chain` | LangGraph root `agent.graph`, nodes (`agent`, `tools`, `recall`…), `llm.<task>` wrappers | `_AgentBaseCallbackHandler` (the SDK alone would type every chain named "*agent*" as `agent`) |
| `generation` | Each LLM call `llm.<task>.generate` (model, usage, cost, TTFT, thinking) | CallbackHandler |
| `tool` | Each tool call | CallbackHandler |
| `retriever` | Read-only lookups: `memory.recall`, `knowledge.search` | `step(as_type="retriever")` |
| `evaluator` | LLM judges: `reflection.judge`, `eval.judge` | `_CHAIN_TYPES` |
| `span` | Other custom steps (`tracing.step`): `tools.collect`, `mcp.list_tools`, `memory.save`, `context.compress`… | `tracing.step` |
| `event` | Instant milestones (`tracing.event`) | `tracing.event` |
| `embedding`, `guardrail` | Not used by the template — use them if the agent embeds itself or runs a guardrail model | `step(as_type=…)` |

## Trace-level attributes (via `propagate_attributes`)

| Attribute | Value | Used for |
|---|---|---|
| `user_id` | JWT `sub` | Users tab, cost per user |
| `session_id` | Session-Id header | Sessions tab, conversation replay |
| `tags` | `[AGENT_NAME, APP_ENV, type:<chat/resume>, stream?]` | Quick filtering |
| `version` | `AGENT_VERSION` | Before/after release comparison |
| `trace_name` | `<agent>.invoke` | |
| `metadata` | request_id, llm_model, memory_backend, memory_id, auth_mode, hitl_tools, reflection_enabled | Filter by configuration |
| `environment` / `release` | `APP_ENV` / `AGENT_VERSION` (constructor) | Separate dev/prod |

Root observation `agent.invoke` (the trace's input/output — shown in the tracing table, read by evaluators, compared by dataset experiments): input = the user message (resume turn: `[Approval decision] - Approved|Rejected|Edited <tool>(<args>)…`), output = the answer **text** (waiting for approval: `[Awaiting approval] - <tool>(<args>)`; failure: `{"error", "status"}` — exactly what the caller received), metadata `tools_used`, `interrupt`, `resume`. It is the conversation line of the session page's **Conversation** view (`Is Root Observation = true`, see SKILL.md → Session view).

## Debug playbook

| Question | Where to look |
|---|---|
| "Why did the agent answer wrong?" | Last generation: input messages (does the system prompt have the right memories/summary?), prior tool outputs, its thinking (`output.thinking`, the trace view's thinking block) |
| "Why did it pick that tool / those args?" | Thinking of the generation that requested the call, then the tool's input |
| "Why didn't it call the tool?" | Generation: does the input include tool schemas? Does `tools.collect.output` include that tool? Did `mcp.list_tools` fail? |
| "Why did it forget information?" | `memory.recall` (query, namespace, count), `context.compress` (did summarization lose details?) |
| "Why is it slow?" | Timeline: generation TTFT, `mcp.list_tools` latency (cache hit?), `memory.recall`, tool spans |
| "Why is it expensive?" | Usage per generation; does `context.summarize` run too often? Low cache_read? |
| "Which action did the user reject?" | Event `hitl.decision` |
| "MCP tool got 403/denied?" | Event `mcp.policy_denied` (server, tool) ⇒ fix the Policy Group (`/agentbase-build-mcp`) |
| "Quality this week?" | Scores `user_feedback`, `self_eval`; Dataset runs from `/agentbase-build-eval` |

## Suggested dashboards (Langfuse Custom Dashboards)

1. Latency p50/p95 by `trace_name`, split by `version`.
2. Cost per day, top 10 by `user_id`; cost and latency per **model** — filter generations by the `model` attribute (never by name: names are per task, `llm.<task>.generate`).
3. Rate of `level=ERROR` / `WARNING` observations by `name` (`mcp.list_tools`, tool spans).
4. Distribution of `user_feedback`, `self_eval`.
5. Count of `context.compress` with decision `summarized` / total traces.

## Sampling & tracing cost

- `LANGFUSE_SAMPLE_RATE=1.0` for dev/staging; high-load prod can use 0.2–0.5. Feedback on a sampled-out turn is still sent as a score but has no trace to open.
- Large input/output (tools returning big JSON): trim inside the tool before returning, or mask in `_mask`. Thinking text is capped at 20k characters per generation (`_REASONING_MAX_CHARS`).

## Masking (what leaves the process)

`app/observability/tracing.py` — tests `tests/test_tracing_mask.py` (rules) and `tests/test_tracing_export.py` (what an in-memory OTel exporter actually receives).

| Channel | Masked by |
|---|---|
| observation input / output / metadata (incl. the generation's thinking text) | `Langfuse(mask=_mask)` — the SDK calls it with RAW objects (its media pass `model_dump()`s pydantic models unless `LANGFUSE_MEDIA_UPLOAD_ENABLED=false`, never dataclasses); `_mask` converts dataclasses and pydantic models (LangChain messages) like the SDK serializer (`asdict`/`model_dump`) before masking |
| `status_message` (exception text) | NOT passed through `mask` by the SDK ⇒ `step`/`_Step.set`/`trace_request` mask it; `langchain_callbacks()` returns `_AgentBaseCallbackHandler` (LLM/tool/chain errors and `ToolMessage(status="error")`); `Langfuse(mask_otel_spans=_mask_otel_spans)` re-masks the `langfuse.observation.status_message` attribute on every exported span |
| OTel span status description / `exception` event | Same text as status_message: masked at the source by the helpers and the CallbackHandler subclass; exceptions are re-raised **outside** the span context manager so OpenTelemetry never records an `exception` event (message + stacktrace). `mask_otel_spans` can only patch attributes, not status/events |
| score comments | `score_trace` / `record_feedback` mask them |
| trace `user_id` | `trace_user_id` hashes email-shaped ids |
| trace metadata (`trace_request(metadata=…)`, propagated to every span) | **not masked** — keep it to config values (models, backend, request id) |

**Secret keys** (dict keys, and the first item of a 2-item list/tuple such as `["X-API-Key", "k9…"]`): name normalized (camelCase → snake, `-`/space → `_`), split on `_`; secret if a part is `authorization, token, secret, password, passwd, pwd, passphrase, cookie, credential(s), otp, pin, cvv, cvc, signature, sig` or the name contains `api_key, apikey, private_key, secret_key, access_key`. Not secret if the LAST part is `usage, type, use, count, limit, expires, expiry, ttl, details` (`token_usage`, `token_type`, `input_token_details`, `output_token_details` stay readable); a part must equal the word, so `max_tokens`, `tokenizer`, `spinner`, `pinned`, `shipping` are not secret.

**Secrets inside text** (every string, including exception text): the same key rule applied to `key=value`, `key: value`, `"key": "value"`, `'key': 'value'`, `\"key\": \"value\"` (JSON inside JSON) and URL query parameters — `https://x/mcp?tavilyApiKey=***&sig=***`, `X-Amz-Signature=***`, `{"access_token": "***"}` (JSON stays parseable); `scheme://user:***@host`; `Bearer ***`/`Basic ***`; JWT ⇒ `***JWT***`; `sk-…`/`vn-…`/`rk-…` ⇒ `***KEY***`.

**PII** (`TRACE_MASK_PII=true`): email; VN mobile `0`/`+84`/`(+84)` + `3|5|7|8|9` + 8 digits with optional space/dot/dash separators (`0.123456789` is a decimal, kept); Luhn-valid 15–19-digit cards; CCCD-shaped 12 digits (`0` + province 00–96 + century/gender digit 0–3 + 8 digits); any 9- or 12-digit number within 30 characters after `CMND/CCCD/CMT/chứng minh/căn cước/định danh` (12 after `ID`) — "Số CMND của tôi là: 123456789" ⇒ `***ID***`. Bare 9-digit numbers (amounts) and non-CCCD-shaped 12-digit codes (order ids) stay readable.

**Known limits / tradeoffs** (extend `_mask_text` per project):
- Unquoted values end at whitespace, `& , ; ) ] } < >`, a quote or a backslash — a password containing one of these is only partly masked; a trailing `.` is swallowed (`OTP: ***`).
- Structured values inside text (`"credentials": {...}`) are masked through their inner keys only; generic names (`key`, `pass`, `code`, Vietnamese `mật khẩu`) and landline numbers are not masked.
- Over-masking is accepted for secret words: `Pin: 5000 mAh` (battery) ⇒ `Pin: *** mAh` and a product-spec key `pin` ⇒ `***` — for agents that never handle PINs, drop `pin` from `_SECRET_PARTS`. A 12-digit code that happens to be CCCD-shaped is masked.
- Spans created outside `tracing.step` / `trace_request` / `langchain_callbacks()` (another OTel instrumentation) only get the `mask_otel_spans` attribute patch — their OTel status description and exception events are exported as-is.
