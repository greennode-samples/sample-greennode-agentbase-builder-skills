# Detailed trace schema & how to use it for debugging

## Observation types (Langfuse v4)

| as_type | Used for |
|---|---|
| `agent` | Root `agent.invoke`, and chains LangGraph marks as agent |
| `chain` | LangGraph nodes |
| `generation` | Each LLM call (model, usage, cost, TTFT) |
| `tool` | Each tool call |
| `span` | Custom-instrumented steps (`tracing.step`) |
| `event` | Instant milestones (`tracing.event`) |

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

## Debug playbook

| Question | Where to look |
|---|---|
| "Why did the agent answer wrong?" | Last generation: input messages (does the system prompt have the right memories/summary?), prior tool outputs |
| "Why didn't it call the tool?" | Generation: does the input include tool schemas? Does `tools.collect.output` include that tool? Did `mcp.list_tools` fail? |
| "Why did it forget information?" | `memory.recall` (query, namespace, count), `context.compress` (did summarization lose details?) |
| "Why is it slow?" | Timeline: generation TTFT, `mcp.list_tools` latency (cache hit?), `memory.recall`, tool spans |
| "Why is it expensive?" | Usage per generation; does `context.summarize` run too often? Low cache_read? |
| "Which action did the user reject?" | Event `hitl.decision` |
| "MCP tool got 403/denied?" | Event `mcp.policy_denied` (server, tool) ⇒ fix the Policy Group (`/agentbase-build-mcp`) |
| "Quality this week?" | Scores `user_feedback`, `self_eval`; Dataset runs from `/agentbase-build-eval` |

## Suggested dashboards (Langfuse Custom Dashboards)

1. Latency p50/p95 by `trace_name`, split by `version`.
2. Cost per day, top 10 by `user_id`.
3. Rate of `level=ERROR` / `WARNING` observations by `name` (`mcp.list_tools`, tool spans).
4. Distribution of `user_feedback`, `self_eval`.
5. Count of `context.compress` with decision `summarized` / total traces.

## Sampling & tracing cost

- `LANGFUSE_SAMPLE_RATE=1.0` for dev/staging; high-load prod can use 0.2–0.5. Feedback can still be attached to sampled traces.
- Large input/output (tools returning big JSON): trim inside the tool before returning, or mask in `_mask`.
