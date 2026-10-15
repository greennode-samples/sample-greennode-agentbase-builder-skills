---
name: agentbase-build-llm
description: "LLM standard for AI agents on GreenNode AgentBase: GreenNode AI Platform MaaS (OpenAI-compatible) via ChatOpenAI; capability TIERS (reasoning / large / small), mapping each processing FLOW (agent, router, summarize, judge, eval_judge, planner...) to the right tier to optimize quality–cost–latency, adaptive routing by question complexity, model FALLBACK chains on infrastructure errors, streaming/usage/TTFT for Langfuse, prompt caching. Use when choosing a model, configuring the LLM, assigning tiers/models per task, adding fallback/backup models, optimizing LLM cost, switching provider, or debugging LLM call errors. Trigger: where to get the LLM, choose model, model tier, reasoning model, small model, fallback model, backup model, routing model, optimize LLM cost, LLM_MODEL, LLM lấy ở đâu, chọn model, model suy luận, model nhỏ, tối ưu chi phí LLM. DO NOT use for creating/deleting API keys or enabling models on the platform (use /agentbase-llm)."
---

# LLM: capability tiers, flows per tier, fallback

## Standard LLM source

| | Value |
|---|---|
| Provider | GreenNode AI Platform (MaaS) |
| Endpoint | `https://maas-llm-aiplatform-hcm.api.vngcloud.vn/v1` (OpenAI-compatible, pay-as-you-go keys). A **Token Plan** key uses `https://tokenplan.api.greennode.ai/v1` with the model **code** — a key on the wrong host returns 401 ([connect-openai-compatible-to-maas](https://docs.greennode.ai/ai-stack/ai-coding/connect-openai-compatible-to-maas)) |
| Rate limit | **10 requests/minute and 14,400/day per account, shared across ALL models** ([available-models](https://docs.greennode.ai/ai-stack/model-as-a-service/available-models)) — a fallback to another model does NOT bypass it; more needs a whitelist request. One chat turn can use several calls (router + agent + tool rounds + judge) ⇒ budget accordingly |
| API key | Create/select via **`/agentbase-llm`** run from `src/backend` (`api-keys create` / `--save-env` writes `LLM_API_KEY` into `.env` itself, never shown). A key the user already has: they run `make set-secret NAME=LLM_API_KEY` in their own terminal (hidden input) — never ask for it in chat, never print it |
| Model | The model's **`path`** field (`/agentbase-llm models list --status ENABLED`) |

## 1. Three capability tiers

| Tier | Used for | Required traits | Env |
|---|---|---|---|
| **reasoning** | Multi-step reasoning, planning, analysis/comparison, math, generating/checking SQL/code, hard grading | Highest quality; slower and more tokens are acceptable | `LLM_MODEL_REASONING` |
| **large** | Default agent: conversation + reliable tool calling | Balanced quality / latency | `LLM_MODEL_LARGE` (empty ⇒ `LLM_MODEL`) |
| **small** | Router, classification, extraction, summarization, simple questions | Fast, cheap, temperature 0 | `LLM_MODEL_SMALL` |

Empty tier ⇒ uses `large`, so a single model (`LLM_MODEL`) still works.

## 2. Flow → tier (`app/llm/__init__.py::DEFAULT_TASK_TIERS`)

| Flow (task) | Where | Default tier | Reason |
|---|---|---|---|
| `agent` | node `agent` | large | tool calling + conversation |
| `agent_simple` / `agent_complex` | node `agent` with adaptive routing on | small / reasoning | answer by difficulty |
| `router` | node `route` | small | 1 short call, classification only |
| `summarize` | `compression.py` | small | summarization needs no deep reasoning |
| `judge` | `reflection.py` (within the chat turn) | small | must be fast, the user is waiting (a thinking large model spent 939–1395 reasoning tokens per grading, §5) |
| `eval_judge` | `evals/evaluators.py` (offline) | reasoning | accuracy first |

- Override with `LLM_TASK_TIERS='{"agent": "reasoning"}'` (e.g. a financial-analysis agent always needs reasoning). A tier typo in `LLM_TASK_TIERS` or a key typo in `LLM_TIER_FALLBACKS` is refused at startup (`Settings`).
- **Adding a new flow** (planner, sql, extractor…): add it to `DEFAULT_TASK_TIERS`, then call `get_llm("<task>", tools)` — do **not** create `ChatOpenAI` yourself.
- **Structured output**: `get_llm("<task>", schema=MyModel)` (Pydantic model / TypedDict / JSON schema) applies `with_structured_output` to **every** model of the fallback chain and returns the parsed object, for internal and conversation tasks alike. `get_llm(...).with_structured_output(...)` does not work: internal tasks return a `RunnableLambda`. Default `method="function_calling"`, because tool calling is verified on MaaS while `json_schema`/`json_mode` are not; the path is tested offline against a mock server only, so check it once on MaaS with your model. The JSON-in-the-prompt pattern of `routing.py` works with any model.
- **Output caps per flow** (`DEFAULT_TASK_MAX_TOKENS`, override `LLM_TASK_MAX_TOKENS='{"router": 512, "judge": 0}'`, 0 = no cap): `router` 1024, `judge` 2048, `summarize` 4096; other flows use `LLM_MAX_TOKENS`. Reasoning tokens count toward the cap, which bounds the latency of internal flows when a thinking model is used. A capped answer (`finish_reason=length`) logs one WARNING per flow and model, and fails safe: the router answers `standard`, the judge passes, compression is skipped (a cut summary never replaces history).
- Trace: chain `llm.<task>` (metadata `llm_task`, `llm_tier`) contains the generation `llm.<task>.generate` — a stable name per flow, **never the model** (Langfuse best practice: a model swap must not break filters, evaluators or dashboards). The model is the generation's `model` attribute; a fallback generation has the same name with metadata `llm_role=fallback`; tag `llm.<tier>` ⇒ cost/latency can be filtered per flow (name) and per model (`model`).
- Session view: only `CONVERSATION_TASKS` (`agent`, `agent_simple`, `agent_complex`) are returned unwrapped; every other task is nested one level deeper (`<caller run_name>` or `<task>.call` → `llm.<task>` → generation) so Langfuse's session view shows the dialogue, not router JSON or judge critiques (`/agentbase-build-tracing` → Session view). A new user-facing flow must be added to `CONVERSATION_TASKS`.
- Thinking: MaaS reasoning models return their thinking in the non-standard `reasoning_content`/`reasoning` field, which `ChatOpenAI` drops. `_chat` builds a `_ChatMaaS` (ChatOpenAI subclass) that keeps it in `generation_info` ⇒ Langfuse shows it on the generation (`/agentbase-build-tracing`). LangChain also copies `generation_info` into the message's `response_metadata`, so `get_llm` removes it from every returned message and stream chunk; otherwise it would be stored in the checkpoint and grow it every turn. Tracing has already read it at that point (`on_llm_end`). `get_chat_model()` returns the bare primary model, without that cleanup.

## 3. Adaptive routing (`LLM_ADAPTIVE_ROUTING`, off by default)

`compress → recall → route → agent`: the **small** model grades `simple | standard | complex` → the agent uses `agent_simple` (small) / `agent` (large) / `agent_complex` (reasoning). The tier is chosen at the start of the turn and kept across that turn's tool rounds. Router error or broken JSON ⇒ `standard`.

**Enable when**: traffic is varied, mostly simple messages (savings via small) but some truly need reasoning (quality via reasoning). **Don't enable when**: all questions have similar difficulty (pin the tier via `LLM_TASK_TIERS`); first-token latency is very sensitive (the router adds about 1 small call); there's no eval to prove it yet. Decide with data: `make eval` with/without it, compare `pass_rate`, cost and latency on Langfuse. Span `llm.route` records `complexity`, `task`, `reason`.

## 4. Fallback (backup model)

```bash
# Placeholders: model `path`s that `/agentbase-llm models list --status ENABLED` shows for THIS key
LLM_TIER_FALLBACKS='{"large": ["<large model, other family>"], "small": ["<small non-thinking model, other family>"], "reasoning": ["<reasoning model, other family>"]}'
LLM_FALLBACK_MODELS='["<small model, other family>"]'   # shared for tiers without their own entry
```

- Pick each fallback with the **same latency/reasoning profile** as the primary (a heavy-reasoning model as the backup of a fast tier blows the time budget) and from a **different family** (avoid correlated failures). Verify that it is ENABLED for this key before relying on it.
- Switch models only on **infrastructure/model** errors: `APIConnectionError`/timeout, `RateLimitError` (429), `InternalServerError` (5xx), `NotFoundError` (404: wrong `path` or model removed), `PermissionDeniedError` (403: model not enabled for this key or not in its plan), and a provider error **inside a streamed 200 response** (vLLM-style SSE chunk `data: {"error": {...}}` — the openai SDK raises it as a bare `openai.APIError` with no status; each model is wrapped so exactly that class is re-raised as `ProviderStreamError`, which is in `FALLBACK_ERRORS`). Do **not** switch on `BadRequestError` (400), `AuthenticationError` (401), `UnprocessableEntityError` (422) or context-overflow errors — they are `APIError` subclasses and are left unchanged, since changing models doesn't fix them.
- **Configuration errors are logged, not hidden**: 401 (key), 402 (plan/balance), 403 (model not allowed) and 404 (model not found) log **one ERROR per model and status per process**, naming the env var to fix (`LLM_MODEL…`, `LLM_TIER_FALLBACKS`/`LLM_FALLBACK_MODELS`, or `LLM_API_KEY`) and `/agentbase-llm`. A 403/404 primary still falls back on every call, so fix it. When the whole chain fails, the primary's error is raised; `config_error_hint(exc)` turns a 401/402/403/404 into a caller-facing "LLM configuration error" message (map it in `app/service.py` before the generic "provider error").
- With fallbacks ⇒ the **primary** gets `max_retries=0` (switch fast instead of re-calling a hung model); fallback models keep `LLM_MAX_RETRIES` so a 429 still gets the SDK's backoff.
- **Time budget** (`worst_case_s(tier)`): every attempt hangs until `LLM_TIMEOUT_S` (×2 for the reasoning tier) **plus the SDK's retry sleeps** (0.5 s × 2ⁿ, capped at 8 s per retry: 0.5 s for 1 retry, 1.5 s for 2, 3.5 s for 3) must stay under `REQUEST_TIMEOUT_S` (default 180 s), otherwise the request is cancelled before the last fallback answers:
  - with fallbacks: `(1 + F × (1 + R)) × T + F × sleep(R)` (primary 1 attempt, each of the F fallbacks 1 + R attempts);
  - without: `(1 + R) × T + sleep(R)`.

  The app logs a WARNING at the first `get_llm` call for **every** tier that is over budget. The template defaults are `LLM_TIMEOUT_S=25`, `LLM_MAX_RETRIES=1` (the earlier 60 s / 2 retries were over budget for every tier even without fallbacks: 3 × 60 + 1.5 = 181.5 s). Values that fit `REQUEST_TIMEOUT_S=180` for **hung or failing** attempts:

  | Setup | `LLM_TIMEOUT_S` | `LLM_MAX_RETRIES` | Fallbacks | Worst case large / small | Worst case reasoning |
  |---|---|---|---|---|---|
  | **Recommended**: 1 fallback per tier | 25 | 1 | 1 per tier | 3 × 25 + 0.5 = 75.5 s | 3 × 50 + 0.5 = 150.5 s |
  | More fallbacks for large/small | 25 | 1 | 2 large/small, **1** reasoning | 5 × 25 + 1 = 126 s | 150.5 s |
  | No fallbacks | 40 | 1 | 0 | 2 × 40 + 0.5 = 80.5 s | 2 × 80 + 0.5 = 160.5 s |
  | Reasoning-heavy agent | 45 | 1 | 1 per tier | 135.5 s | 270.5 s ⇒ needs `REQUEST_TIMEOUT_S=300` |

  **This bounds failures, not slow answers.** With `LLM_STREAMING=true` the timeout is an **inactivity** timeout (time to first token or between chunks): a model that keeps streaming its thinking is never cut. Tested (`tests/test_llm.py`): a stream that lasts 3× `LLM_TIMEOUT_S` completes. On a real run, three calls to thinking models took 44 + 69 + 61 s and the turn hit the 504 of `REQUEST_TIMEOUT_S`. Budget the **turn**:

  `turn ≈ compress (when over the threshold) + router (adaptive routing) + (tool rounds + 1) × agent + judge (reflection) + Memory + tool time`

  Take each term from the **p95 latency of `llm.<task>.generate` on Langfuse with realistic prompts** (long history, tool results), not from one-line probes: the 2.5–7 s in §5 come from single trivial requests. To keep a turn inside `REQUEST_TIMEOUT_S`:
  - use **non-thinking (or low-reasoning) models for internal flows** (`router`, `summarize`, `judge`); a thinking model there adds tens of seconds to every turn;
  - keep the per-flow output caps (§2, `LLM_TASK_MAX_TOKENS`) and set `LLM_MAX_TOKENS` for the agent;
  - lower `MAX_TOOL_ROUNDS`, keep reflection off unless an eval proves its value, or raise `REQUEST_TIMEOUT_S` (and the client's timeout) for agents that need long reasoning.

  With `LLM_STREAMING=false` the timeout must cover the whole answer: keep 60 s or more and raise `REQUEST_TIMEOUT_S`. Not budgeted: a server `Retry-After` header replaces the SDK backoff (the SDK honours up to 120 s). Whether MaaS sends `Retry-After` on 429 is not verified, so check a 429's headers before relying on retries.
- Fallbacks should have similar capability and latency profile, **a different model family / provider** (avoid correlated failures), be enabled on AIP for this key and already evaluated.
- Streaming: if the primary fails **mid-answer**, the fallback answers from scratch and the stream first sends `{"event": "reset", "reason": "llm_fallback"}` so the client clears the partial text. Tested with a fake model that dies after tokens (`APIConnectionError`) and with the real `ChatOpenAI` against a local OpenAI-compatible mock server that streams tokens and then an SSE error chunk: the backup model answers, and the service emits `reset` followed by the backup's tokens only. Not yet observed on MaaS itself.
- Langfuse: the primary model's generation at level ERROR, followed by the fallback model's generation (different `model_name`) in the same trace.
- Verified on MaaS: nonexistent primary model (404) → automatically switched to the fallback and answered normally (the one-ERROR log was added afterwards and is tested offline).
- Cross-provider fallback (OpenAI, internal vLLM): needs per-model `base_url`/key, extend `_chat()`; keys stored in Identity.

## Rate limits & budget (Protect & Govern)

Two layers, both answer **429**:

| Layer | Scope | Configure |
|---|---|---|
| MaaS account limit | **10 requests/min, 14,400/day per account**, shared by all models and keys ([available-models](https://docs.greennode.ai/ai-stack/model-as-a-service/available-models)) | More only via whitelist request to GreenNode |
| **Rate Limit** (Protect & Govern) | Requests and/or tokens per period, enforced **independently per API key and per model** it's attached to; rejected as soon as either threshold is hit | Console *Protect & Govern → Rate Limit* (Root/Admin) — [rate-limit](https://docs.greennode.ai/ai-stack/agent-base/protect-govern/rate-limit) |

Standard:
- **One LLM API key per agent per environment** (`/agentbase-llm`), so a rate limit and usage can be attached to exactly that agent; a separate key for eval/CI.
- Attach a Rate Limit to each agent key (requests/day **and** tokens/day) sized from the expected traffic × calls per turn (router + agent + tool rounds + judge). This caps runaway loops and cost.
- Budget: track spend in [Usage & Budget](https://docs.greennode.ai/ai-stack/usage-budget); per-trace cost is in Langfuse.
- On 429:
  - **A fallback helps for a Protect & Govern limit**: that limit is per key **and per model**, so another model has its own quota.
  - **A fallback does not help for the account limit** (10 RPM / 14,400 per day), which is shared by every model.
  - The SDK backoff (0.5–8 s) is much shorter than a per-minute window: after a 429 from a per-minute limit, retries in the same minute usually fail too. More retries don't fix it; fewer calls per turn do. Lower `MAX_TOOL_ROUNDS`, disable adaptive routing / reflection, lower eval `--concurrency`, or raise the limit.
  - A turn that still fails returns a clear error.

## 5. Choosing models per tier (the user decides)

1. `/agentbase-llm`: list ENABLED models, **let the user choose**, don't choose for them.
2. Quickly probe each candidate (tool calling, latency, reasoning tokens), then run `make eval` with each configuration to settle.

Reference numbers from **single-request probes** on GreenNode MaaS on 2026-10-02: one trivial tool-calling request per model. They are not a benchmark and not the latency on real prompts, where thinking models took 40–70 s per call (§4). The model ids are what that account had enabled then; some (`qwen3.8`, `deepseek-v4`, `glm-5.3`) are not in the public catalog. **Verify that a model is ENABLED for your key** (`/agentbase-llm models list --status ENABLED`) before you use it:

| Model | Tool calling | Latency | Reasoning tokens | Suggested tier |
|---|---|---|---|---|
| `deepseek/deepseek-v4-pro` | ✔ | 5.3s | 23 | reasoning |
| `qwen/qwen3.8-max` | ✔ | 3.5s | 14 | reasoning / large |
| `kimi/kimi-k2.6` | ✔ | 4.5s | 58 | reasoning |
| `z-ai/glm-5.2` | ✔ | 6.9s | 28 | large |
| `minimax/minimax-m3` | ✔ | 5.9s | 26 | large |
| `qwen/qwen3.7-plus` | ✔ | 7.1s | 228 | large (heavy on reasoning tokens) |
| `z-ai/glm-5.3-flash` | ✔ | 2.5s | 30 | small |
| `qwen/qwen3.8-flash` | ✔ | 3.5s | 25 | small |
| `deepseek/deepseek-v4-flash` | ✔ | 3.4s | 36 | small |
| `qwen/qwen3.6-flash` | ✔ | 3.4s | 131 | small |

**Quality warning (seen on a real runtime):** with adaptive routing, a greeting was routed to `agent_simple` using `qwen/qwen3.8-flash`, and the Vietnamese reply got mixed with Indonesian words ("beberapa"). Models for flows that **answer the user directly** (`agent_simple`, `agent`, `agent_complex`) must pass evals on language/tone; internal flows (`router`, `summarize`, `eval_judge`) are lower risk. Add eval items in the user's language for simple messages (greetings, thanks) when enabling adaptive routing.

Note: most models on MaaS emit reasoning tokens, even flash variants. Langfuse v4 **does separate** `output_reasoning` (verified), so check it directly on the generation — together with the thinking text in its output (captured by `_ChatMaaS`; MaaS returns it as `reasoning_content`, verified streamed and not on qwen3.8-flash/max and deepseek-v4-pro).

**Judge burns reasoning tokens**: on real Langfuse, `reflection.judge` running `qwen3.8-max` (large tier at the time) used **939–1395 reasoning tokens per grading** (longer than the answer itself). The `judge` flow (within the chat turn) therefore defaults to the **small** tier: give it a fast, low-reasoning, evaluated model (`LLM_MODEL_SMALL`), and keep `eval_judge` (offline) on reasoning. Its output is capped at 2048 tokens and each call at `REFLECTION_TIMEOUT_S` (`/agentbase-build-eval`). Re-measure on Langfuse before settling.

**Fallback proved its value in practice**: during testing, `z-ai/glm-5.3-flash` returned **503** repeatedly; the failed generation was recorded at level ERROR and automatically switched to `qwen/qwen3.8-max`, the user saw no error. That backup, a reasoning-heavy large model behind a fast small one, is the anti-pattern of §4: it saved the answer but cost latency.

## Standard code (assets of this skill)

- `app/llm/__init__.py`: `get_llm(task, tools, schema=)` is the **only entry point**; `tier_for`, `model_for`, `fallbacks_for`, `max_tokens_for`, `FALLBACK_ERRORS`, `ProviderStreamError`, `config_error_hint`, `worst_case_s`. `_chat` caches by (model, tier, role) and returns `_ChatMaaS` (keeps the thinking text for tracing); the small tier uses temperature 0, the reasoning tier timeout ×2. Tier names live in `app/config.py` (`LLM_TIERS`) so `Settings` validates the tables.
- `app/llm/routing.py`: `classify(text, summary, recent)` for adaptive routing. The router sees the request (clipped to 2,000 chars), the last 3 exchanges before it (`recent_exchanges`, tool calls skipped, 2,000 chars at most) and the rolling summary. A follow-up such as "and on Sunday?" can't be graded alone.
- `tests/test_llm.py`: default/overridden tiers, table typos refused at startup, flow table, fallback on infrastructure errors (no fallback on 400), each flow uses the right model, per-flow output caps (also on the fallback) and the truncation warning, adaptive routing picks the right flow and gets the previous turn. Against a local OpenAI-compatible mock HTTP server: an SSE error chunk before or after the first token falls back (`ainvoke`, `invoke`, `abatch`, and the streamed service turn with `reset`), while HTTP 400/401/422 do not; `reasoning_content` (streamed and not) reaches the callbacks but not the returned message (`additional_kwargs` or `response_metadata`) or stream chunks; 403/404 log one ERROR per model; 401/402/403/404 read as configuration errors; structured output through the fallback chain for an internal and a conversation task. Generation names stay `llm.<task>.generate` whatever the model, the fallback or the caller's `run_name`. Time budget: the backoff constants match the openai SDK, `worst_case_s` per setup, the warning fires when over budget (e.g. 60 s × 3 attempts + backoff > 180 s) for every tier at the first `get_llm`, and stays quiet for the template defaults and every row of the §4 table.
- `streaming=True` + `stream_usage=True` ⇒ Langfuse gets TTFT and usage when streaming (MaaS returns usage when streaming).

## Prompt caching

The prompt is `[static system prompt, *history, latest user message, per-turn context message, *this turn's tool rounds]`. The static system prompt and the tool schemas stay byte-identical. Everything that changes per turn goes in the **context system message** (`build_context_message` in `app/graph/builder.py`), inserted right after the latest user message and never persisted: current date/time, summary, recalled notes, critique. The cached prefix (system prompt + history + user message) is therefore reused across turns. Don't put timestamps, request ids or per-user data in the system prompt. MaaS returns `prompt_tokens_details.cached_tokens` (measured: 8128/8174 tokens cached on repeat) ⇒ Langfuse records `input_cache_read`.

Keep `LLM_BASE_URL` pointing at MaaS on the Runtime too. An older doc mentioned a sidecar LLM proxy on the Runtime; the current docs don't, so don't route through one unless GreenNode confirms it.

## Troubleshooting

| Error | Cause | Fix |
|---|---|---|
| 401 (ERROR log names `LLM_API_KEY`) | Wrong/deleted key, or a Token Plan key on the pay-as-you-go host | `/agentbase-llm api-keys list`, reload with `--save-env` (or the user re-enters it: `make set-secret NAME=LLM_API_KEY`); Token Plan keys use the Token Plan base URL (fallback can't help) |
| 402 | Token Plan expired or out of balance | Renew/top up the plan, or use a pay-as-you-go key (`/agentbase-llm`) |
| 403 model not allowed (ERROR log names the env var) | Model not enabled for this key, or not in the key's plan | Enable it or pick an ENABLED model (`/agentbase-llm models list --status ENABLED`); with a fallback the request still works but every call fails over first, so fix it |
| `ProviderStreamError` / bare `openai.APIError: <message>` (no status), often after some tokens | Provider failed inside the stream (SSE error chunk: engine overloaded or crashed) | Falls back automatically when the tier has fallbacks; without fallbacks it surfaces as an error, so add a fallback |
| WARNING `LLM tier …: worst case … > REQUEST_TIMEOUT_S` | Timeout × attempts + retry backoff exceed the request budget | Use the §4 time-budget table values |
| 404 model not found (ERROR log names the env var) | Wrong `path` or model removed (MaaS may also answer 404 for a model not enabled for the key) | Fix the path; with a fallback the request still works but every call fails over first, so fix it |
| Frequent 429 | Account limit (10 RPM, shared by all models) or a Protect & Govern limit (per key and model) | Account limit: a fallback does NOT help; request a whitelist, turn off adaptive routing/reflection, lower `MAX_TOOL_ROUNDS`, lower eval `--concurrency`. Protect & Govern: a fallback to another model helps, or raise the limit |
| 504 although no call failed | Thinking models streaming for minutes (the inactivity timeout never fires) | Budget the turn from Langfuse p95 (§4); non-thinking models for internal flows, output caps |
| WARNING `… hit its output cap (finish_reason=length)` | A thinking model spent the flow's `max_tokens` (§2) | Non-thinking model for that flow, or raise `LLM_TASK_MAX_TOKENS` |
| No usage/cost on Langfuse | Missing model price / provider doesn't return usage when streaming | Declare model price; `LLM_STREAM_USAGE=false` |
| Router picks the wrong tier | Router prompt doesn't fit the domain | Adjust `ROUTER_PROMPT` with domain examples, re-measure with eval |

## Official docs

- [available-models](https://docs.greennode.ai/ai-stack/model-as-a-service/available-models) — model list and **rate limits (10 RPM / 14,400 per day per account)**
- [connect-openai-compatible-to-maas](https://docs.greennode.ai/ai-stack/ai-coding/connect-openai-compatible-to-maas) — pay-as-you-go vs Token Plan base URLs and model ids
- [maas-api](https://docs.greennode.ai/ai-stack/model-as-a-service/maas-api) — MaaS API
- [pricing](https://docs.greennode.ai/ai-stack/model-as-a-service/pricing) — pricing per model
- [rate-limit](https://docs.greennode.ai/ai-stack/agent-base/protect-govern/rate-limit) — Protect & Govern request/token limits (429)
- [usage-budget](https://docs.greennode.ai/ai-stack/usage-budget) — usage and spend tracking
