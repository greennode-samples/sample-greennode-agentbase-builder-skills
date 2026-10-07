---
name: agentbase-build-tracing
description: "Observability/tracing standard using the Langfuse Python SDK v4 for LangGraph AI agents on GreenNode AgentBase: 1 trace/request, input/output of every step (auth, tools.collect, MCP, compress, recall, LLM generation, tool, HITL, reflection), model + usage + cost + prompt cache tokens + TTFT, user/session/tags/version, secret/PII masking, Prompt Management, score/feedback. Use when adding tracing, debugging wrong answers via traces, tracking cost/latency/cache, attaching prompt versions, or adding spans for a new step. Trigger: langfuse, tracing, trace, observability, log LLM, token cost, prompt cache, TTFT, prompt management, span, chi phí token. DO NOT use for container runtime/CPU/RAM logs (use /agentbase-monitor) or for running evaluations (use /agentbase-build-eval)."
---

# Tracing with Langfuse v4

## Version constraints (REQUIRED)

- **Langfuse Python SDK v4** (`langfuse>=4,<5`, OpenTelemetry-based) + the `langchain` package (required for `langfuse.langchain`).
- APIs used: `Langfuse(..., mask=, mask_otel_spans=)` (`mask_otel_spans` verified on SDK 4.17; `init_tracing` skips it with a warning on older 4.x), `get_client()`, `start_as_current_observation(as_type=...)`, `propagate_attributes(...)`, `langfuse.langchain.CallbackHandler`, `create_event`, `create_score`, `get_prompt`, `run_experiment`.
- **FORBIDDEN** legacy v2/v3 APIs: `from langfuse.decorators import observe, langfuse_context`, `langfuse.trace(...)`, `CallbackHandler(user_id=..., session_id=...)`, old-style `handler.flush()`, `update_current_trace` to set user/session (v4 uses `propagate_attributes`).
- The only module that touches Langfuse: `app/observability/tracing.py` (an asset of this skill). Other code only calls the helpers: `trace_request`, `step`, `event`, `score_trace`, `record_feedback`, `langchain_callbacks`.

## Step 1 — Connect

1. Have a Langfuse instance (Cloud or self-hosted). Create project `<project-name>`, get the public/secret keys.
2. Fill env (local `.env`, deploy `.env.<env>`): `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_BASE_URL`, optional `LANGFUSE_SAMPLE_RATE`. Missing keys ⇒ tracing turns itself off (no-op), the agent still runs.
3. Runtime in VPC mode ⇒ make sure it can reach `LANGFUSE_BASE_URL` (route/NAT).
4. **Declare GreenNode model prices** in Langfuse (Project Settings → Models: match pattern = `LLM_MODEL` path, input/output/cached-input prices) — without this, generations have no cost. Take prices from the user's contract/billing; do not guess.

## Step 1b — Local self-hosted Langfuse for dev/test (actually run: server v4.49.0 + SDK v4.16)

```bash
curl -fsSL -o docker-compose.yml https://raw.githubusercontent.com/langfuse/langfuse/main/docker-compose.yml
# Generate secrets for every "# CHANGEME" variable (.env next to compose) + headless init of project & API key:
#   LANGFUSE_INIT_ORG_ID/NAME, LANGFUSE_INIT_PROJECT_ID/NAME, LANGFUSE_INIT_PROJECT_PUBLIC_KEY/SECRET_KEY,
#   LANGFUSE_INIT_USER_EMAIL/NAME/PASSWORD, NEXTAUTH_URL=http://localhost:<port>
docker compose -p langfuse-local up -d && curl http://localhost:<port>/api/public/health
```
- Ports 3000/5432 often taken ⇒ change the mapping (e.g. `127.0.0.1:3200:3000`, `127.0.0.1:55432:5432`) and `NEXTAUTH_URL`. Don't touch other running Langfuse instances.
- Declare model prices via `POST /api/public/models` (`modelName`, `matchPattern`, `unit=TOKENS`, `inputPrice`, `outputPrice`) to get cost.
- Runtimes on AgentBase cannot call localhost ⇒ prod uses a Langfuse with a public/VPC URL.

## Step 2 — Standard trace tree (verified on a real Langfuse server)

```
agent.invoke [agent]        input=message|resume · output={response, tools_used}|{interrupt}
│                           user_id · session_id · tags[agent, env, type:chat|resume, stream] · version · release
│                           metadata{request_id, llm_model, memory_backend, memory_id, auth_mode, hitl_tools, ...}
├─ auth [event]             output{user_id, mode, iss, aud, exp, client}      (NO token)
├─ tools.collect [span]     output{local, memory, mcp, mcp_errors} · level WARNING if MCP fails
│   └─ mcp.list_tools [span] input{server, transport, url, auth, cached} · output[tool names]
└─ agent.graph [chain]      (CallbackHandler: input/output = state of each node)
    ├─ compress [chain]
    │   ├─ context.compress [span]   metadata{tokens_before, threshold, keep_last, removed_messages,
    │   │                             tokens_after, compression_ratio, latency_ms} · output{decision, summary}
    │   └─ context.summarize [generation]
    ├─ recall [chain]
    │   └─ memory.recall [span]      input{query, namespace, limit, trigger:auto} · output{facts, count}
    ├─ route [chain]                 (LLM_ADAPTIVE_ROUTING) llm.route [span] output{complexity, task, reason}
    │   └─ llm.router [generation]   (tier small)
    ├─ agent [chain]
    │   ├─ context.hard_trim [event, WARNING]
    │   └─ llm.<task> [chain]        metadata{llm_task, llm_tier} · links prompt version (agent flow only)
    │       ├─ <model> [generation]  NAME = model (e.g. qwen/qwen3.8-flash) · input = messages + tool schemas ·
    │       │                        output · model_parameters · usage · cost · TTFT
    │       └─ <model> (fallback)    only when the primary model fails (primary at level ERROR + statusMessage, e.g. 503)
    │                                usage{input, output, total, input_cache_read, cache_creation_*,
    │                                      output_reasoning} · cost · completion_start_time (TTFT)
    ├─ approval [chain]              hitl.request [event] · hitl.decision [event]
    ├─ tools [chain]
    │   └─ <tool_name> [tool]        input=args · output=result · level ERROR when the tool fails
    │       ├─ memory.save [span]    (tool remember)
    │       ├─ a2a.call [span]       {agent, url, message, state, text} (tool ask_<agent>)
    │       └─ mcp.policy_denied [event, WARNING]  {server, tool} — blocked by the Gateway's Policy Group
    └─ reflect [chain]               reflection.judge [generation]
scores: user_feedback (-1|0|1, from frontend) · self_eval (0..1) · hitl_requested
```

Field details and how to read them: `references/trace-schema.md`.

## Step 3 — Instrumentation rules for new code

| Code type | How to trace |
|---|---|
| LangChain/LangGraph (node, LLM, tool, retriever) | Automatic via `CallbackHandler` — just set meaningful `run_name`/`tags` |
| Non-LangChain I/O steps (HTTP, DB, MCP list, memory API) | `with tracing.step("<domain>.<action>", input={...}) as st: ...; st.set(output=..., metadata=...)` |
| Instant milestone / warning | `tracing.event("<name>", output=..., level="WARNING")` |
| Online evaluation | `tracing.score_trace("<metric>", value, comment=...)` |

- `step()` attaches itself to the **currently running LangGraph node/tool** (reads the current run from `var_child_runnable_config`), so the span lands in the right place in the tree. Without a LangChain run, it attaches to the current OTel span.
- An exception inside `step()` ⇒ observation `level=ERROR` + masked `status_message`, then re-raised (outside the OTel span context, so no raw `exception` event/stacktrace is exported). Same for `trace_request`.
- Name spans `<domain>.<action>` (`crm.lookup_customer`, `payment.create`) for filtering/dashboards.
- **Never** put tokens, API keys, passwords or card data in input/output — masking is a safety net. `_mask` masks secret **keys** (dict keys and `[key, value]` header pairs/tuples whose normalized name has a part `token`, `secret`, `password`, `cookie`, `credential`, `otp`, `pin`, `cvv`, `signature`/`sig`… or contains `api_key`/`private_key`/`access_key` — but not `max_tokens`, `token_usage`, `input_token_details`), secrets **inside text** (`key=value`, `key: value`, JSON/repr inside a string, URL query such as `?tavilyApiKey=…&sig=…`, `user:password@host`, `Bearer`/`Basic`, JWT, `sk-…`/`vn-…`), dataclass/pydantic objects passed to the helpers, and PII (`TRACE_MASK_PII=true`, default: email, VN mobile, Luhn-valid card, CCCD-shaped 12 digits, 9/12 digits after a CMND/CCCD/căn cước… keyword ⇒ `***EMAIL***`/`***PHONE***`/`***CARD***`/`***ID***`). Langfuse does **not** pass `status_message`, score comments, trace `user_id` or trace metadata through `mask` ⇒ the helpers mask status messages and comments, `langchain_callbacks()` returns a masking CallbackHandler, `mask_otel_spans` re-masks `status_message` on every exported span, `trace_user_id` hashes email-shaped ids; keep `trace_request(metadata=…)` non-sensitive. Rules, known limits and false-positive tradeoffs: `references/trace-schema.md` → Masking. Add business patterns in `_mask_text`; tests: `tests/test_tracing_mask.py`, `tests/test_tracing_export.py`. Disable PII masking only in dev environments using fake data.
- **Generations always carry the model name**: `get_llm` wraps the model in a chain `llm.<task>` (or the `run_name` set by the caller, e.g. `context.summarize`, `reflection.judge`), while the inner generation is named via the `RunnableBinding`'s `config_factories`. Reason: `RunnableWithFallbacks` passes the chain's `run_name` down to the child model, overriding `with_config(run_name=...)`; this actually happened — generations carried the flow name and the UI didn't show which model was called.
- **Prompt version is attached only to the agent's generation**: the `langfuse_prompt` metadata is passed at call time to the chain wrapping the LLM call in the `agent` node (`llm.<task>`, named by `get_llm` in `app/llm`). Setting it on node/graph metadata also wrongly attaches the prompt to judge, router and summarize generations (seen on real Langfuse).
- Non-LangChain platform calls must have their own span: `memory.checkpoint_load` (checkpoint read; once hung for 7 minutes while this step was invisible in the trace), `memory.recall`, `mcp.list_tools`, `knowledge.search` (query + list of hits file/heading/score — see why RAG returns the wrong source).
- Nodes never create a new `Langfuse()`; don't call `flush()` per request (lifespan flushes on shutdown).

## Step 4 — Model, cache, cost, latency

- Model name + params: taken from `ChatOpenAI` (matches `LLM_MODEL`). Generations from the fake model in tests have no model name — that's normal.
- Prompt cache: OpenAI-compatible APIs return `prompt_tokens_details.cached_tokens` ⇒ Langfuse records `input_cache_read` (verified). Not showing ⇒ the provider/model doesn't enable caching or the prompt prefix is unstable (see `/agentbase-build-llm`, prompt caching section).
- TTFT: requires `LLM_STREAMING=true` (Langfuse records `completion_start_time` at the first token).
- Usage when streaming: requires `LLM_STREAM_USAGE=true`.
- Cost: requires declared model prices (Step 1.4).

## Step 5 — Prompt Management (optional, recommended for prod)

1. Create a text prompt on Langfuse named `LANGFUSE_PROMPT_NAME`, with label `production`/`staging`.
2. Env: `LANGFUSE_PROMPT_NAME`, `LANGFUSE_PROMPT_LABEL`, `LANGFUSE_PROMPT_CACHE_TTL` (seconds, default 300; blank or not an integer ⇒ 300 with a warning). `LANGFUSE_PROMPT_CACHE_TTL` is read with `os.getenv` in `app/prompts/__init__.py` (not a `Settings` field) — `.env.example` lists it with the default.
3. `app/prompts/__init__.py::get_system_prompt()` fetches the prompt (SDK cache), falling back to `system.md` on error; `graph/builder.py` passes `metadata.langfuse_prompt` at call time to the agent's `llm.<task>` chain ⇒ the agent generations link to the prompt version ⇒ compare latency/cost/score per version, change prompts without redeploying.

## Reading traces via API / script (Langfuse v4 events_only)

Server v4 **drops** the old `/api/public/traces`, `/sessions`, `/scores`, `/metrics` (404 "not available … events_only mode"). Use:
- `GET /api/public/v2/observations?traceId=<id>&fields=core,basic,time,io,metadata,model,usage,prompt,metrics`
- `GET /api/public/v3/scores?traceId=<id>` · datasets: `GET /api/public/v2/datasets`

Bundled script: `uv run python <skill-dir>/scripts/lf_trace.py <trace_id> | --latest N | --session S`. It prints the tree with latency, model, usage (input/output/reasoning/cache), cost, TTFT, prompt version, statusMessage of failed observations, plus scores. **Use it first when debugging "slow / wrong / expensive"**.

Real example (a "Hello" message took 55s); the trace showed it immediately: `mcp.list_tools` connector failed for 15s (sequential loading, failures not cached); `reflect` ran for a greeting and retried (+27s); judge glm 503 ⇒ fallback. After fixing (parallel MCP loading + negative cache, skip reflection for simple messages) it dropped to 11s.

## Step 6 — Feedback & sessions

- The response returns `trace_id`; the frontend sends `{"type":"feedback","trace_id","score"}` ⇒ score `user_feedback`. `record_feedback` sends a deterministic score id (`feedback_score_id`: sha256 of `user|trace|name`, 32 hex) ⇒ Langfuse upserts: re-tapping 👍/👎 keeps ONE score with the latest value (the eval loop never reads stale votes). Pass `user_id=` when calling it.
- Langfuse **Sessions** = `session_id` (the whole conversation, including HITL resume turns); **Users** = `user_id`.
- Traces with 👎 ⇒ add to the regression dataset (`/agentbase-build-eval`).

## Checks (required before deploy)

1. `make dev`, send 2–3 requests (1 with a tool call), open Langfuse: exactly 1 trace/request, tree matches Step 2, has user/session.
2. Generations have model, usage, cost; TTFT when streaming.
3. Search traces (input/output **and** statusMessage of failed observations) for `Bearer `, `eyJ`, `sk-`, `access_token=`, `password=`, `api_key`/`ApiKey=` followed by anything other than `***` ⇒ no results.
4. Remove the keys ⇒ the agent still runs normally.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `ModuleNotFoundError: langchain` when importing CallbackHandler | Missing `langchain` package | `uv add langchain` |
| No traces at all | Missing keys / `LANGFUSE_TRACING_ENABLED=false` / wrong base URL / runtime has no internet egress | Check env, `LANGFUSE_DEBUG=true` |
| Custom span under the wrong parent | Span created directly via the client instead of `tracing.step()` | Use `tracing.step()` |
| Each node becomes its own trace | Graph invoked outside `trace_request`, or CallbackHandler created multiple times for one request | Use `service._Run` |
| Generation missing cost | Model prices not declared | Step 1.4 |
| Traces missing for the last requests when the container stops | Not flushed | lifespan already calls `shutdown_tracing()`; don't kill -9 |

## Official docs

- [logs-and-metrics](https://docs.greennode.ai/ai-stack/agent-base/agent-runtime/logs-and-metrics) — platform runtime logs and CPU/RAM metrics (complements Langfuse traces)
