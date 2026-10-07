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
| API key | Create/select via **`/agentbase-llm`** → `.env` `LLM_API_KEY` (never print the key) |
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
| `judge` | `reflection.py` (within the chat turn) | large | must be fast, the user is waiting |
| `eval_judge` | `evals/evaluators.py` (offline) | reasoning | accuracy first |

- Override with `LLM_TASK_TIERS='{"agent": "reasoning"}'` (e.g. a financial-analysis agent always needs reasoning).
- **Adding a new flow** (planner, sql, extractor…): add it to `DEFAULT_TASK_TIERS`, then call `get_llm("<task>", tools)` — do **not** create `ChatOpenAI` yourself.
- Trace: chain `llm.<task>` (metadata `llm_task`, `llm_tier`) contains a generation **named after the model** (`qwen/qwen3.8-flash`, `… (fallback)`), tag `llm.<tier>` ⇒ cost/latency can be filtered per flow and per model.

## 3. Adaptive routing (`LLM_ADAPTIVE_ROUTING`, off by default)

`compress → recall → route → agent`: the **small** model grades `simple | standard | complex` → the agent uses `agent_simple` (small) / `agent` (large) / `agent_complex` (reasoning). The tier is chosen at the start of the turn and kept across that turn's tool rounds. Router error or broken JSON ⇒ `standard`.

**Enable when**: traffic is varied, mostly simple messages (savings via small) but some truly need reasoning (quality via reasoning). **Don't enable when**: all questions have similar difficulty (pin the tier via `LLM_TASK_TIERS`); first-token latency is very sensitive (the router adds about 1 small call); there's no eval to prove it yet. Decide with data: `make eval` with/without it, compare `pass_rate`, cost and latency on Langfuse. Span `llm.route` records `complexity`, `task`, `reason`.

## 4. Fallback (backup model)

```bash
LLM_TIER_FALLBACKS='{"large": ["qwen/qwen3.8-max"], "small": ["qwen/qwen3.8-flash"], "reasoning": ["qwen/qwen3.8-max"]}'
LLM_FALLBACK_MODELS='["qwen/qwen3.8-flash"]'   # shared for tiers without their own entry
```

- Switch models only on **infrastructure/model** errors: `APIConnectionError`/timeout, `RateLimitError` (429), `InternalServerError` (5xx), `NotFoundError` (model removed/wrong path), `PermissionDeniedError` (model not enabled). Do **not** switch on `BadRequestError` (400) or a wrong API key, since changing models doesn't fix those.
- With fallbacks ⇒ the **primary** gets `max_retries=0` (switch fast instead of re-calling a hung model); fallback models keep `LLM_MAX_RETRIES` so a 429 (account-wide 10 RPM) still gets the SDK's backoff.
- **Time budget:** worst case = (1 + fallbacks × (1 + `LLM_MAX_RETRIES`)) × `LLM_TIMEOUT_S` (×2 for the reasoning tier) must stay under `REQUEST_TIMEOUT_S` (default 180s), otherwise the request is cancelled before the last fallback answers. The app logs a WARNING at startup when a tier is over budget.
- Fallbacks should have similar capability, **a different model family / provider** (avoid correlated failures), be enabled on AIP and already evaluated.
- Streaming: if the primary fails **mid-answer**, the fallback answers from scratch and the stream first sends `{"event": "reset", "reason": "llm_fallback"}` so the client clears the partial text (tested).
- Langfuse: the primary model's generation at level ERROR, followed by the fallback model's generation (different `model_name`) in the same trace.
- Verified on MaaS: nonexistent primary model (404) → automatically switched to `qwen/qwen3.8-flash` and answered normally.
- Cross-provider fallback (OpenAI, internal vLLM): needs per-model `base_url`/key, extend `_chat()`; keys stored in Identity.

## 5. Choosing models per tier (the user decides)

1. `/agentbase-llm`: list ENABLED models, **let the user choose**, don't choose for them.
2. Quickly probe each candidate (tool calling, latency, reasoning tokens), then run `make eval` with each configuration to settle.

Reference numbers, measured on GreenNode MaaS on 2026-10-02 (1 tool-calling request per model; not a benchmark):

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

Note: most models on MaaS emit reasoning tokens, even flash variants. Langfuse v4 **does separate** `output_reasoning` (verified), so check it directly on the generation.

**Judge burns reasoning tokens**: on real Langfuse, `reflection.judge` running `qwen3.8-max` used **939–1395 reasoning tokens per grading** (longer than the answer itself). The `judge` flow (within the chat turn) should use a fast, low-reasoning model (`LLM_TASK_TIERS='{"judge":"small"}'` with an evaluated small model), keeping `eval_judge` (offline) on reasoning. Re-measure on Langfuse before settling.

**Fallback proved its value in practice**: during testing, `z-ai/glm-5.3-flash` returned **503** repeatedly; the failed generation was recorded at level ERROR and automatically switched to `qwen/qwen3.8-max`, the user saw no error.

## Standard code (assets of this skill)

- `app/llm/__init__.py`: `get_llm(task, tools)` is the **only entry point**; `tier_for`, `model_for`, `fallbacks_for`, `FALLBACK_ERRORS`. `_chat` caches by (model, tier, role); the small tier uses temperature 0, the reasoning tier timeout ×2.
- `app/llm/routing.py`: `classify()` for adaptive routing.
- `tests/test_llm.py`: default/overridden tiers, flow table, fallback on infrastructure errors (no fallback on 400), each flow uses the right model, adaptive routing picks the right flow.
- `streaming=True` + `stream_usage=True` ⇒ Langfuse gets TTFT and usage when streaming (MaaS returns usage when streaming).

## Prompt caching

Order the system message **stable parts first, dynamic parts last** (prompt → summary → memories → critique); keep tool schemas stable; don't inject timestamps/request ids at the start of the prompt. MaaS returns `prompt_tokens_details.cached_tokens` (measured: 8128/8174 tokens cached on repeat) ⇒ Langfuse records `input_cache_read`.

## Sidecar LLM Proxy on Runtime (needs verification)

The docs say LLM calls on Runtime may go through the Sidecar LLM Proxy `localhost:18080` (rate limit / Protect & Govern). By default keep `LLM_BASE_URL` pointing directly at MaaS; if the platform requires the sidecar, only change `LLM_BASE_URL` in `.env.<env>`.

## Troubleshooting

| Error | Cause | Fix |
|---|---|---|
| 401 | Wrong/deleted key | `/agentbase-llm api-keys list`, reload with `--save-env` (fallback can't help) |
| 404 model not found | Wrong `path` / model not enabled | Fix the path; with fallback the request still works but the trace shows ERROR — must fix |
| Frequent 429 | Account limit 10 RPM shared by all models, or Protect & Govern limits | A fallback model does NOT help (same account limit); request a whitelist, turn off adaptive routing/reflection, lower `MAX_TOOL_ROUNDS`, lower eval `--concurrency` |
| No usage/cost on Langfuse | Missing model price / provider doesn't return usage when streaming | Declare model price; `LLM_STREAM_USAGE=false` |
| Router picks the wrong tier | Router prompt doesn't fit the domain | Adjust `ROUTER_PROMPT` with domain examples, re-measure with eval |

## Official docs

- [available-models](https://docs.greennode.ai/ai-stack/model-as-a-service/available-models) — model list and **rate limits (10 RPM / 14,400 per day per account)**
- [connect-openai-compatible-to-maas](https://docs.greennode.ai/ai-stack/ai-coding/connect-openai-compatible-to-maas) — pay-as-you-go vs Token Plan base URLs and model ids
- [maas-api](https://docs.greennode.ai/ai-stack/model-as-a-service/maas-api) — MaaS API
- [pricing](https://docs.greennode.ai/ai-stack/model-as-a-service/pricing) — pricing per model
- [rate-limit](https://docs.greennode.ai/ai-stack/agent-base/protect-govern/rate-limit) — Protect & Govern request/token limits (429)
