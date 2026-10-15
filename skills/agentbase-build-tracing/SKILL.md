---
name: agentbase-build-tracing
description: "Tracing standard (Langfuse Python SDK v4) for LangGraph agents on GreenNode AgentBase, aligned with the Langfuse best practices and used together with the official Langfuse skill (langfuse/skills): 1 trace per turn, sessions, specific observation types (one agent per agent, retriever, evaluator), stable names, root input/output, model/usage/cost/cache/TTFT/thinking on every generation, user/session/tags/env, secret/PII masking, Prompt Management, feedback scores, and a required run-and-audit loop (lf_trace.py --audit). Use when adding or auditing tracing in an AgentBase agent, debugging wrong/slow/expensive answers via traces, attaching prompt versions, or adding spans for a new step. Trigger: langfuse, tracing, trace, observability, log LLM, token cost, prompt cache, TTFT, thinking, span, chi phí token. DO NOT use for container runtime/CPU/RAM logs (use /agentbase-monitor) or for running evaluations (use /agentbase-build-eval)."
---

# Tracing with Langfuse v4

## Works with the official Langfuse skill

Install it next to this skill set — it is what Langfuse itself recommends before writing Langfuse code:

```bash
npx skills add langfuse/skills --skill langfuse      # or: git clone https://github.com/langfuse/skills && symlink skills/langfuse
```

| The `langfuse` skill brings | This skill decides (AgentBase standard) |
|---|---|
| **Docs first**: `https://langfuse.com/llms.txt`, any page as `.md`, `https://langfuse.com/api/search-docs?query=…` — never write SDK calls from memory | The single module `app/observability/tracing.py` and its helpers; nothing else imports `langfuse` |
| `langfuse-cli` (`npx langfuse-cli api <resource> <action>`) to read/write traces, prompts, datasets, scores | Names, observation types and the trace tree of Step 2 (dashboards, evaluators and evals depend on them) |
| Playbooks: instrumentation audit, prompt migration, user-feedback signals, setting up evals, judge calibration, CI experiment gates, v4 project migration | Masking (secrets + VN PII), GreenNode MaaS specifics (model prices, thinking text, prompt cache), LangGraph span attachment, `lf_trace.py --audit` |

Conflicts: the hard rules here win (masking, one module, no per-request `flush()`, Step 2 names). For SDK/API signatures and UI behaviour, the **current** Langfuse docs win over this file — then fix this skill. Never ask the user to paste Langfuse keys into the chat: they go in `.env` (`/agentbase-build-scaffold`).

Workflow (same order as the Langfuse instrumentation playbook): **assess** what exists → **baseline** (table below + Step 2) → **run & self-audit until clean** (Step 6, required) → **explore the traces with the user** (Step 7).

## Version constraints (REQUIRED)

- **Langfuse Python SDK v4** (`langfuse>=4.17,<5`, OpenTelemetry-based) + the `langchain` package (required for `langfuse.langchain`). Use the latest 4.x (offline test suite last run on 4.18.0).
- APIs used: `Langfuse(..., mask=, mask_otel_spans=)` (`mask_otel_spans` verified on SDK 4.17; `init_tracing` skips it with a warning on older 4.x), `get_client()`, `start_as_current_observation(as_type=...)`, `propagate_attributes(...)`, `langfuse.langchain.CallbackHandler`, `create_event`, `create_score`, `get_prompt`, `run_experiment`.
- **FORBIDDEN** legacy v2/v3 APIs: `from langfuse.decorators import observe, langfuse_context`, `langfuse.trace(...)`, `CallbackHandler(user_id=..., session_id=...)`, old-style `handler.flush()`, `update_current_trace` to set user/session (v4 uses `propagate_attributes`).
- The only module that touches Langfuse: `app/observability/tracing.py` (an asset of this skill). Other code only calls the helpers: `trace_request`, `step`, `event`, `score_trace`, `record_feedback`, `langchain_callbacks`.

## Langfuse best practices → how an AgentBase agent applies them

Source: [What does a good trace look like?](https://langfuse.com/docs/observability/best-practices) — re-read it (fresh, not from memory) before every audit.

| Best practice | Here |
|---|---|
| Trace = one unit of work, session = the conversation | 1 trace per `/invocations` turn; `session_id` = conversation, including HITL resume turns |
| Root input/output = what a reviewer needs | Root `agent.invoke` = the conversation line: input = user message (resume turn: `describe_decisions` text), output = **answer text** (approval request: `describe_interrupt` text; failure: **the error the caller received** `{error, status}`); raw payloads (`interrupt`, `resume`, `tools_used`) in root metadata |
| Session view = a readable conversation replay | Langfuse builds it from **generations**, shallowest per trace; see "Session view" below — conversation LLM calls (`CONVERSATION_TASKS`) at the top, internal LLM calls one level deeper, static system prompt + a per-turn context message |
| Generation input/output as role-labeled OpenAI chat messages | `_AgentBaseCallbackHandler` records input = the OpenAI request `{"messages": [...], "tools": [...]}`, output = the assistant message `{role, content, tool_calls, thinking}`; tool calls `{id, type: "function", function: {name, arguments: "<JSON string>"}}` (rendered as cards), tool results `{role: "tool", tool_call_id}` |
| Each LLM call is its own `generation`, interleaved with its tools | CallbackHandler: one generation per call of the agent loop, tool observations as siblings |
| Most specific observation type | `generation` LLM call · `tool` tool · `retriever` lookups (`memory.recall`, `knowledge.search`) · `evaluator` judges (`reflection.judge`, `eval.judge`) · `agent` **only** the root, remote A2A agents (`a2a.<agent>`) and in-graph sub-agents (`subagent.<role>`) · `chain` LangGraph nodes · `span` other I/O · `event` milestones |
| Capture thinking | `app/llm` keeps the provider's `reasoning_content`/`reasoning` (ChatOpenAI drops it) ⇒ generation output `thinking: [{"type": "thinking", "content": …}]` — the thinking block of Langfuse's trace view (masked, capped at 20k chars); never stored in the message/checkpoint |
| Names: stable, low-cardinality, verb/action, never the model | `<domain>.<action>`; generations `llm.<task>.generate`; no ids, counters, retries or model names in a name (→ metadata). Renaming breaks evaluators, dashboards and saved views |
| Model + usage (+ cost) on generations | model from `ChatOpenAI`; usage incl. `input_cache_read`, `output_reasoning`; cost needs MaaS prices declared (Step 1.4) |
| Metadata for context, tags for dimensions known upfront | trace metadata = config (request id, models per tier, memory backend…); tags `[agent, env, type:chat|resume, stream]` + your `feature:`/`tenant:` tags via `trace_request(tags=…)`; things learned later ⇒ scores |
| Environment, user, session on every observation | `Langfuse(environment=APP_ENV, release=AGENT_VERSION)`; `propagate_attributes(user_id, session_id, tags, version)` opened **before** any child ⇒ cost-bearing generations carry them |
| Link prompt versions | Step 5 |
| Mask sensitive data | Step 3 masking rules + `references/trace-schema.md` → Masking |
| No noise (HTTP/DB/framework spans) | SDK v4 default span filter keeps only Langfuse + GenAI spans — never set `should_export_span=lambda s: True`; to add a scope, compose with `langfuse.span_filter.is_default_export_span` |
| Feedback as scores | `user_feedback`, `self_eval`, `hitl_requested`; design more signals with the `langfuse` skill's user-feedback playbook (name a score after the observed signal, e.g. `tool_call_rejected`, not `quality`) |

## Step 1 — Connect

1. Have a Langfuse instance (Cloud or self-hosted). Create project `<project-name>`, get the public/secret keys.
2. Fill env (local `.env`, deploy `.env.<env>`): `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_BASE_URL`, optional `LANGFUSE_SAMPLE_RATE`. Missing keys ⇒ tracing turns itself off (no-op), the agent still runs. `main.py` calls `load_dotenv()` before anything imports Langfuse.
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

## Step 2 — Standard trace tree

Verified end to end on a real Langfuse server (self-hosted v4.58, SDK 4.18) with real MaaS models — chat, knowledge search, tool call, HITL interrupt + approve, follow-up in the same session, streamed answer with reflection, a 403 primary ⇒ fallback, feedback — `lf_trace.py --audit` clean on every trace. Names/types are pinned by `tests/test_tracing_tree.py` (in-memory OTel exporter).

```
agent.invoke [agent]        input=user message|resume decisions · output=answer text|{interrupt}
│                           user_id · session_id · tags[agent, env, type:chat|resume, stream] · version · release
│                           metadata{request_id, llm_model_*, memory_backend, memory_id, auth_mode, hitl_tools, ...,
│                                    tools_used}
├─ auth [event]             output{user_id, mode, iss, aud, exp, client}      (NO token)
├─ tools.collect [span]     output{local, memory, mcp, a2a, mcp_errors, hitl_unmatched} · metadata{tool_count} ·
│   │                       level WARNING if an MCP server fails or a HITL_TOOLS pattern matches no tool
│   └─ mcp.list_tools [span] input{server, transport, url, auth, cached} · output[tool names]
├─ memory.checkpoint_load [span] output{messages, next, interrupts} · metadata{backend, session_id, kind}
└─ agent.graph [chain]      (CallbackHandler; LangGraph chains carry NO input/output — see Session view)
    ├─ compress [chain]
    │   ├─ context.compress [span]   metadata{tokens_before, threshold, keep_last, removed_messages,
    │   │                             tokens_after, compression_ratio, latency_ms} · output{decision, summary}
    │   └─ context.summarize [chain] → llm.summarize [chain] → llm.summarize.generate [generation]
    ├─ recall [chain]
    │   └─ memory.recall [retriever] input{query, namespace, limit, trigger:auto} · output{facts, count}
    ├─ route [chain]                 (LLM_ADAPTIVE_ROUTING) llm.route [span] output{complexity, task, reason}
    │   └─ router.call [chain] → llm.router [chain] → llm.router.generate [generation]   (tier small)
    ├─ agent [chain]
    │   ├─ context.hard_trim [event, WARNING]
    │   └─ llm.<task> [chain]        metadata{llm_task, llm_tier} · links prompt version (agent flow only)
    │       ├─ llm.<task>.generate [generation]  model attribute (e.g. qwen/qwen3.8-flash) · input = {messages,
    │       │                        tools} (OpenAI chat request) · output = {role, content, tool_calls, thinking} ·
    │       │                        model_parameters · usage{input, output, total, input_cache_read,
    │       │                        cache_creation_*, output_reasoning} · cost · completion_start_time (TTFT)
    │       └─ llm.<task>.generate   2nd generation only when the primary fails: metadata llm_role=fallback, other
    │                                model; the primary one is at level ERROR + statusMessage (e.g. 503)
    ├─ approval [chain]              hitl.request [event] · hitl.decision [event] (the node re-runs on resume ⇒ the
    │                                resume trace shows both: what was asked and what was decided)
    ├─ tools [chain]
    │   └─ <tool_name> [tool]        input=args · output=result · level ERROR when the tool fails
    │       ├─ memory.save [span]    (tool remember) · memory.recall [retriever] (tool recall_memory)
    │       ├─ knowledge.search [retriever] (tool search_knowledge) output{hits[file, heading, score]}
    │       ├─ a2a.<agent> [agent]   {agent, url, message, state, text} (tool ask_<agent>)
    │       ├─ identity.<kind> [span] input{provider} · output{status: ok|authorization_required} (WARNING when
    │       │                        consent is needed) — kind = agent_api_key | agent_access_token | user_api_key |
    │       │                        user_access_token (app/identity.py helpers; never the credential)
    │       └─ mcp.policy_denied [event, WARNING]  {server, tool} — blocked by the Gateway's Policy Group
    └─ reflect [chain]               reflection.judge [evaluator] → llm.judge [chain] → llm.judge.generate
    (internal LLM calls — router, summarize, judge — sit one level deeper than the agent's llm.<task>.generate)
scores: user_feedback (-1|0|1, from frontend) · self_eval (0..1) · hitl_requested
```

Field details, debug playbook, dashboards: `references/trace-schema.md`.

## Step 3 — Instrumentation rules for new code

| Code type | How to trace |
|---|---|
| LangChain/LangGraph (node, LLM, tool, retriever) | Automatic via `CallbackHandler` — just set meaningful `run_name`/`tags` |
| LLM call | Only through `get_llm(task)` (`/agentbase-build-llm`) ⇒ chain `llm.<task>` + generation `llm.<task>.generate` |
| Non-LangChain I/O step (HTTP, DB, MCP list, memory API) | `with tracing.step("<domain>.<action>", input={...}) as st: ...; st.set(output=..., metadata=...)` |
| Lookup that changes nothing (search, read, vector query) | `tracing.step(..., as_type="retriever")` |
| Call to another agent (A2A, remote) | `tracing.step("a2a.<agent>", as_type="agent")` — one distinct name per target agent |
| In-graph sub-agent (LangGraph subgraph) | run name `subagent.<role>` ⇒ typed `agent` (`_CHAIN_TYPES`) |
| LLM judge inside the turn | run name `reflection.judge` / `eval.judge` ⇒ typed `evaluator`; add new judges to `_CHAIN_TYPES` |
| Instant milestone / warning | `tracing.event("<name>", output=..., level="WARNING")` |
| Online evaluation | `tracing.score_trace("<metric>", value, comment=...)` |

- `step()` attaches itself to the **currently running LangGraph node/tool** (reads the current run from `var_child_runnable_config`), so the span lands in the right place in the tree. Without a LangChain run, it attaches to the current OTel span.
- An exception inside `step()` ⇒ observation `level=ERROR` + masked `status_message`, then re-raised (outside the OTel span context, so no raw `exception` event/stacktrace is exported). Same for `trace_request`.
- `event()` inside a node makes the node's span current before `create_event`: the SDK's `span.start_observation(as_type="event")` (4.18) ignores its parent in async graphs — on a real server the `hitl.*` events landed under `agent.invoke` instead of `approval` (`tests/test_tracing_tree.py`).
- LangChain runs started inside a `step()` (e.g. the router LLM inside `llm.route`) are siblings of that span under the node, not its children: the CallbackHandler nests by LangChain run, not by OTel context.
- **Names are an API**: `<domain>.<action>` (`crm.lookup_customer`, `payment.create`); never a model name, user/order id, retry counter or anything per-request (put it in metadata). Keep existing names stable — evaluators, dashboards and saved views match them.
- **Observation types — one agent per agent**: the SDK's `CallbackHandler` types every chain whose name contains "agent" as an `agent` (seen in tests: `agent.graph`, node `agent` and `llm.agent` became 3 nested agents under `agent.invoke`). `langchain_callbacks()` returns `_AgentBaseCallbackHandler`, which types chains from `_CHAIN_TYPES` (default `chain`).
- Every observation should have input and/or output; one with neither is noise — drop it or fill it.
- **Never** put tokens, API keys, passwords or card data in input/output — masking is a safety net. `_mask` masks secret **keys** (dict keys and `[key, value]` header pairs/tuples whose normalized name has a part `token`, `secret`, `password`, `cookie`, `credential`, `otp`, `pin`, `cvv`, `signature`/`sig`… or contains `api_key`/`private_key`/`access_key` — but not `max_tokens`, `token_usage`, `input_token_details`), secrets **inside text** (`key=value`, `key: value`, JSON/repr inside a string, URL query such as `?tavilyApiKey=…&sig=…`, `user:password@host`, `Bearer`/`Basic`, JWT, `sk-…`/`vn-…`), dataclass/pydantic objects passed to the helpers, and PII (`TRACE_MASK_PII=true`, default: email, VN mobile, Luhn-valid card, CCCD-shaped 12 digits, 9/12 digits after a CMND/CCCD/căn cước… keyword ⇒ `***EMAIL***`/`***PHONE***`/`***CARD***`/`***ID***`). Thinking text goes through the same `mask`. Langfuse does **not** pass `status_message`, score comments, trace `user_id` or trace metadata through `mask` ⇒ the helpers mask status messages and comments, `langchain_callbacks()` returns a masking CallbackHandler, `mask_otel_spans` re-masks `status_message` on every exported span, `trace_user_id` hashes email-shaped ids; keep `trace_request(metadata=…)` non-sensitive. Rules, known limits and false-positive tradeoffs: `references/trace-schema.md` → Masking. Add business patterns in `_mask_text`; tests: `tests/test_tracing_mask.py`, `tests/test_tracing_export.py`. Disable PII masking only in dev environments using fake data.
- **Generations are named after the task, not the model**: `get_llm` wraps the model in a chain `llm.<task>` (or the `run_name` set by the caller, e.g. `context.summarize`, `reflection.judge`), while the inner generation is named `llm.<task>.generate` via the `RunnableBinding`'s `config_factories` (`RunnableWithFallbacks` passes the chain's `run_name` down to the child model, overriding `with_config(run_name=...)`). The model is the generation's `model` attribute (shown on the generation, filterable, used for cost); the fallback generation has the same name with metadata `llm_role=fallback`. Earlier versions of this skill named generations after the model — that breaks every filter/evaluator/dashboard on a model swap (Langfuse best practice).
- **Prompt version is attached only to the agent's generation**: the `langfuse_prompt` metadata is passed at call time to the chain wrapping the LLM call in the `agent` node (`llm.<task>`, named by `get_llm` in `app/llm`). Setting it on node/graph metadata also wrongly attaches the prompt to judge, router and summarize generations (seen on real Langfuse).
- Non-LangChain platform calls must have their own span: `memory.checkpoint_load` (checkpoint read; once hung for 7 minutes while this step was invisible in the trace), `memory.recall`, `mcp.list_tools`, `knowledge.search` (query + list of hits file/heading/score — see why RAG returns the wrong source).
- Nodes never create a new `Langfuse()`; don't call `flush()` per request (lifespan flushes on shutdown).

## Step 4 — Model, cache, cost, latency, thinking

- Model name + params: taken from `ChatOpenAI` (matches `LLM_MODEL`). Generations from the fake model in tests have no model name — that's normal.
- Prompt cache: OpenAI-compatible APIs return `prompt_tokens_details.cached_tokens` ⇒ Langfuse records `input_cache_read` (verified). Not showing ⇒ the provider/model doesn't enable caching or the prompt prefix is unstable (see `/agentbase-build-llm`, prompt caching section).
- TTFT: requires `LLM_STREAMING=true` (Langfuse records `completion_start_time` at the first token).
- Usage when streaming: requires `LLM_STREAM_USAGE=true`.
- Cost: requires declared model prices (Step 1.4).
- **Thinking**: most MaaS models emit reasoning tokens (`output_reasoning`, verified). The thinking **text** comes back in the non-standard `reasoning_content` field (verified on MaaS for `qwen/qwen3.8-flash`, `qwen/qwen3.8-max`, `deepseek/deepseek-v4-pro`, streamed and not) that `ChatOpenAI` drops; `app/llm` `_ChatMaaS` keeps it (`reasoning_content` or `reasoning`) in `generation_info` (streamed deltas concatenated) and `_AgentBaseCallbackHandler` adds it to the traced output — the message, the checkpoint and the next request stay unchanged. Seen on a real Langfuse 4.58: judge 902 reasoning tokens ⇒ 3,120 chars of thinking on `llm.judge.generate`. `output_reasoning > 0` but no `reasoning_content` in the output ⇒ another provider/field name — extend `_reasoning_text` in `app/llm`.

## Step 5 — Prompt Management (optional, recommended for prod)

1. Create a text prompt on Langfuse named `LANGFUSE_PROMPT_NAME`, with label `production`/`staging`. Moving more prompts (judge, router, summarizer…): follow the `langfuse` skill's prompt-migration playbook (`{{var}}` double-brace variables only, label `production`, text vs chat type).
2. Env: `LANGFUSE_PROMPT_NAME`, `LANGFUSE_PROMPT_LABEL`, `LANGFUSE_PROMPT_CACHE_TTL` (seconds, default 300; blank or not an integer ⇒ 300 with a warning). `LANGFUSE_PROMPT_CACHE_TTL` is read with `os.getenv` in `app/prompts/__init__.py` (not a `Settings` field) — `.env.example` lists it with the default.
3. `app/prompts/__init__.py::get_system_prompt()` fetches the prompt (SDK cache), falling back to `system.md` on error; `graph/builder.py` passes `metadata.langfuse_prompt` at call time to the agent's `llm.<task>` chain ⇒ the agent generations link to the prompt version ⇒ compare latency/cost/score per version, change prompts without redeploying.

## Session view — the conversation; the trace — every step

Goal for every reviewer (support, QA, product, developers): **a session reads as the user's conversation** (one
user → assistant line per turn), **a trace holds every step** (spans, retrievers, router, judge, thinking, usage,
cost). Langfuse 4.58 has two session renderings; the template is correct for both:

1. **Session page (default)** — per trace, a card per observation, filtered by a *view* (`Filter observations`,
   `My Views`). Default view = "All observations with I/O" (every observation with input and output ⇒ long cards,
   every generation replaying the history). **Conversation view = filter `Is Root Observation = true`** ⇒ only
   `agent.invoke` per trace, whose input/output are the conversation line:
   user message → answer · approval request `[Awaiting approval] - remember({...})` · decision
   `[Approval decision] - Rejected remember({...}) — reason` · failure `{error, status}` as the user got it.
   - Link: `<LANGFUSE_BASE_URL>/project/<project>/sessions/<session_id>?filter=isRootObservation%3Bboolean%3B%3B%3D%3Btrue`
     (`lf_trace.py --session <id>` prints it next to the every-step link).
   - **Once per Langfuse project** (project owner): open any session → `Filter observations` → `Is Root Observation`
     = true → save as view **"Conversation"** → set it as the default view. Keep "All observations with I/O" for
     debugging.
2. **Conversation timeline** (newer renderer, source `packages/shared/src/server/transcript` in langfuse/langfuse —
   re-check on upgrades): assembled from **GENERATION** observations (+ TOOL observations matched by tool-call id).
   Per trace it shows the **current turn** only (input after the previous turn's last `assistant`/`tool` message)
   and only threads whose first generation is at the **shallowest depth** — so generation I/O must be faithful
   OpenAI chat messages and internal LLM calls must sit deeper (rules below).

Rules (all enforced by the template; keep them when adding code):

| Rule | Why (seen on a real 10-turn session) | How |
|---|---|---|
| Generation I/O in the OpenAI chat format | the SDK's LangChain handler appends the tool definitions to the input as fake `{"role": "tool", "content": <schema>}` messages AFTER the user's question ⇒ the session view treated the whole input as the current turn and showed "User: chào bạn…" (turn 1) again in every turn; LangChain tool calls (`args` dicts) were not rendered as cards | `_AgentBaseCallbackHandler` (`tracing.langchain_callbacks()`): input `{"messages", "tools"}`, messages via `convert_to_openai_messages`, tool-call `arguments` as JSON strings, no `additional_kwargs`. `lf_trace.py --audit` flags tool definitions recorded as messages |
| LangGraph chains carry no I/O | every node/edge/wrapper chain recorded the graph state — the whole message history — so the session page's default view showed 230 cards for a 13-turn conversation and the first user message 143 times | `_AgentBaseCallbackHandler` drops input/output of plain chains (graph, nodes, conditional edges, `llm.<task>`, `<task>.call`); they keep name, timing, metadata, level/status. Measured: 230 ⇒ 76 cards, 143 ⇒ 23 repeats (the rest is the faithful history in generation inputs); Conversation view: 1 card per turn |
| Only user-facing LLM calls are at the top | router JSON (`{"complexity": …}`) and judge critiques showed up as threads "3.1 / 3.2 / 3.3" between the user's messages | `get_llm(task)`: tasks in `CONVERSATION_TASKS` (`agent`, `agent_simple`, `agent_complex`) are returned as is; **every other task is wrapped one level deeper** (`<run_name>` or `<task>.call`). A new internal flow (planner, classifier, extractor…) needs nothing; a new user-facing flow must be added to `CONVERSATION_TASKS` |
| Static system prompt, per-turn context separate | the full system prompt was repeated every turn the recalled facts changed; the changing prefix also cost prompt cache | `[SystemMessage(system_prompt), *history, Human, SystemMessage(context)…]` — current date/time, summary, recalled notes (framed as data) and the self-eval critique go in `build_context_message`, inserted right after the latest user message (`with_turn_context`). Measured: agent `cache_read` 1,024–2,048 ⇒ 5,120 tokens |
| Don't rewrite history | an edited/removed message breaks the thread match | expected splits: a reflection retry (the rejected answer is removed), context compression (history replaced by a summary), HITL `edit` (arguments changed) — the session shows a new thread for that turn |
| A failed turn reads like the user saw it | the root showed `TimeoutError: ` with no output; cancelled children showed `<object object at 0x…>` | `trace_request(error_output=_user_error)` ⇒ root output `{error, status}` = the HTTP error / SSE `error` event; `_describe()` names empty errors; cancellations are `WARNING` "Cancelled: …" |
| Thinking in the trace, not in the session | thinking text in `additional_kwargs.reasoning_content` was rendered nowhere (raw JSON only) | output message-level `thinking: [{"type": "thinking", "content": …}]` ⇒ the trace view's thinking block (`ThinkingBlock`); the session view ignores it, so the conversation stays clean |

## Step 6 — Run and self-audit the traces (required, before every deploy)

Instrumentation is not done when the code compiles. Loop until the trace is clean:

1. **Run** the instrumented path end to end: `make test` (offline: `tests/test_tracing_tree.py` checks names, types, root I/O, thinking, masking), then `make dev` + 2–3 real requests (1 with a tool call, 1 HITL approval if enabled, 1 follow-up in the same session).
2. **Fetch** what was sent: `uv run python <skill-dir>/scripts/lf_trace.py --latest 3 --audit` (tree + findings, exit 1 on FAIL), or `npx langfuse-cli api observations list --trace-id <id> --json`.
3. **Audit** against the best-practices page fetched **fresh** (`https://langfuse.com/docs/observability/best-practices.md`) and the Step 2 tree. For each observation ask: could someone see exactly what context the agent had when it decided (messages, tool results, recalled facts, thinking)?
   - exactly 1 trace per request, user/session/environment set, the session view replays the conversation;
   - generations have model, usage, cost (TTFT when streaming) and thinking when `output_reasoning > 0`;
   - search input/output **and** statusMessage of failed observations for `Bearer `, `eyJ`, `sk-`, `access_token=`, `password=`, `api_key`/`ApiKey=` followed by anything other than `***` ⇒ no results (`--audit` does this);
   - remove the keys ⇒ the agent still runs normally.
4. **Fix** every gap, re-run, re-fetch. Report what you audited and changed, with the link to the final trace (`tracing.current_trace_url()` or the Langfuse UI).

## Step 7 — Explore the traces with the user

Point them to what they now have — **Traces** (one per turn, filter by `type:` tags / `environment`), **Sessions** — the **Conversation** view (`Is Root Observation = true`, link from `lf_trace.py --session`) to read what the user lived, the default view to see every step, **Users** (cost per user), **Scores** (`user_feedback`, `self_eval`), the **Agent Graph** of a trace, the dashboards of `references/trace-schema.md` — and ask what is still missing to judge an answer at a glance. Explain *why* each addition helps (e.g. a `feature:` tag ⇒ cost/latency per feature).

## Reading traces via API / CLI / script (Langfuse v4 events_only)

Server v4 **drops** the old `/api/public/traces`, `/sessions`, `/scores`, `/metrics` (404 "not available … events_only mode"). Use:
- `GET /api/public/v2/observations?traceId=<id>&fields=core,basic,time,io,metadata,model,usage,prompt,metrics` (CLI: `observations`, not `traces` or `legacy-*`)
- `GET /api/public/v3/scores?traceId=<id>&fields=core,details` (without `details` no `comment`) · datasets: `GET /api/public/v2/datasets`

Bundled script: `uv run python <skill-dir>/scripts/lf_trace.py <trace_id> | --latest N | --session S [--audit]`. It prints the tree with latency, model (+ `llm_role`), usage (input/output/reasoning/cache), cost, TTFT, prompt version, statusMessage of failed observations, plus scores; `--audit` adds the best-practice findings. **Use it first when debugging "slow / wrong / expensive"**.

Real example (a "Hello" message took 55s); the trace showed it immediately: `mcp.list_tools` connector failed for 15s (sequential loading, failures not cached); `reflect` ran for a greeting and retried (+27s); judge glm 503 ⇒ fallback. After fixing (parallel MCP loading + negative cache, skip reflection for simple messages) it dropped to 11s.

## Feedback & sessions

- The response returns `trace_id`; the frontend sends `{"type":"feedback","trace_id","score"}` ⇒ score `user_feedback`. `record_feedback` sends a deterministic score id (`feedback_score_id`: sha256 of `user|trace|name`, 32 hex) ⇒ Langfuse upserts: re-tapping 👍/👎 keeps ONE score with the latest value (the eval loop never reads stale votes). Pass `user_id=` when calling it.
- Langfuse **Sessions** = `session_id` (the whole conversation, including HITL resume turns); **Users** = `user_id`.
- More signals (HITL reject/edit, answer copied, retry pressed…): propose them as a P0/P1 table with the `langfuse` skill's user-feedback playbook, then `score_trace("<observed_signal>", …)`.
- Traces with 👎 ⇒ add to the regression dataset (`/agentbase-build-eval`).

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `ModuleNotFoundError: langchain` when importing CallbackHandler | Missing `langchain` package | `uv add langchain` |
| No traces at all | Missing keys / `LANGFUSE_TRACING_ENABLED=false` / wrong base URL / runtime has no internet egress | Check env, `LANGFUSE_DEBUG=true` |
| Custom span under the wrong parent | Span created directly via the client instead of `tracing.step()` | Use `tracing.step()` |
| Each node becomes its own trace | Graph invoked outside `trace_request`, or CallbackHandler created multiple times for one request | Use `service._Run` |
| Several nested `agent` observations for one agent | Plain `CallbackHandler` (types chains named "*agent*" as agents) | Use `tracing.langchain_callbacks()`; real sub-agents ⇒ `subagent.<role>` |
| Generations named after the model (`qwen/…`) | Project scaffolded from an older version of this skill | Update `app/llm` (`llm.<task>.generate`) and the dashboards/evaluators that matched the old names |
| `output_reasoning > 0` but no thinking text | Provider uses another field / `_ChatMaaS` not used | Extend `_reasoning_text`; create models only via `get_llm` |
| Generation missing cost | Model prices not declared | Step 1.4 |
| HTTP/DB spans in the tree | `should_export_span` overridden to export everything | Back to the SDK default filter |
| Session view shows several threads per turn (router JSON, judge critique) | An internal LLM call at the same depth as the agent's generation | Call it through `get_llm(<task>)` with a task not in `CONVERSATION_TASKS` |
| Session view repeats the first user message / whole history every turn | Generation input not in the OpenAI chat format (tool definitions recorded as `role: tool` messages) — a plain `langfuse.langchain.CallbackHandler` | Use `tracing.langchain_callbacks()`; `lf_trace.py --audit` |
| Session view repeats the system prompt every turn | Per-turn data (facts, summary, critique) written into the system prompt | Put it in `build_context_message` |
| Status `<object object at 0x…>` / `TimeoutError: ` | Cancelled or message-less errors | Already handled by `_describe` / `_AgentBaseCallbackHandler` — use `tracing.step`/`trace_request` |
| Traces missing for the last requests when the container stops | Not flushed | lifespan already calls `shutdown_tracing()`; don't kill -9 |

## Official docs

- Langfuse: [best practices](https://langfuse.com/docs/observability/best-practices) · [observation types](https://langfuse.com/docs/observability/features/observation-types) · [LangChain/LangGraph integration](https://langfuse.com/integrations/frameworks/langchain) · [Agent Skill](https://langfuse.com/docs/api-and-data-platform/features/agent-skill) · [CLI](https://langfuse.com/docs/api-and-data-platform/features/cli) · [span filtering](https://langfuse.com/docs/observability/sdk/advanced-features#filtering-by-instrumentation-scope)
- [logs-and-metrics](https://docs.greennode.ai/ai-stack/agent-base/agent-runtime/logs-and-metrics) — platform runtime logs and CPU/RAM metrics (complements Langfuse traces)
