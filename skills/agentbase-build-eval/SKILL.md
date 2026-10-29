---
name: agentbase-build-eval
description: "Evaluation loop standard for AI agents on GreenNode AgentBase with Langfuse v4: offline eval (Datasets + Experiments, evaluators correctness LLM-judge / expected_tools / must_contain / no_error, pass_rate), regression gate in CI (make eval --min-pass-rate), online eval (score user_feedback, self_eval), improvement loop from low-rated traces → dataset → fix → rerun, and an in-graph self-evaluation loop (judge → retry). Use when measuring agent quality, comparing prompts/models, blocking regressions before deploy, collecting user feedback, or enabling self-evaluation. Trigger: evaluation, eval, loop evaluation, evaluate agent, quality test, dataset, LLM as judge, regression, compare models, self-reflection, đánh giá agent, test chất lượng, so sánh model. DO NOT use for pure code unit tests (use pytest in tests/)."
---

# Evaluation loop

## Standard loop

```
      ┌──────────── (1) Dataset (evals/datasets/*.jsonl ⇄ Langfuse Dataset) ◀─────────────┐
      ▼                                                                                   │
 (2) Offline eval: make eval → Langfuse Experiment run (pass_rate, correctness, tools)   │
      │  threshold met?                                                                    │
      ├─ no → fix prompt / tool / model → rerun (compare runs in the Langfuse UI)         │
      ▼  yes                                                                               │
 (3) Deploy (CI gate)                                                                      │
      ▼                                                                                    │
 (4) Online eval: user_feedback 👍👎, self_eval, (optional) Langfuse managed LLM-as-judge   │
      ▼                                                                                    │
 (5) Low-score / 👎 traces → review → add to the regression dataset ──────────────────────┘
```

## (1) Dataset

- JSONL file `src/backend/evals/datasets/<name>.jsonl`, one item per line:
  `{"id", "input": {"message"}, "expected_output": "<description of the correct answer>", "metadata": {"expected_tools": [...], "forbidden_tools": [...], "must_contain": [...], "user_id": "<optional>", "tags": [...]}}`
- `forbidden_tools`: tools that must **not** be called (e.g. an injection message must not call `send_email`; a policy-denied message must not retry the tool; small talk must not call `remember`). An **attempt** fails the item: a call that reached a HITL approval is recorded in `tools_requested` even when it was rejected. The runner always **rejects** a forbidden tool at the approval, even with `--hitl approve`, so it never executes. A forbidden tool that is not in `HITL_TOOLS` runs before it can be stopped; put side-effecting tools in `HITL_TOOLS`. `user_id`: run the item as a fixed user (multi-turn LTM/isolation tests); empty ⇒ random user.
- `expected_tools: []` checks nothing. Give small-talk items a real check instead: `forbidden_tools: ["remember"]`, `expected_output` naming the language. The shipped `smoke.jsonl` has an English and a Vietnamese greeting; `ltm.jsonl` has negative items (small talk, a one-off request) where `remember` is forbidden.
- Split datasets by feature: `smoke.jsonl`, one `<feature>.jsonl` per main flow, `ltm.jsonl` (only when `LTM_ENABLED=true`). **The gate (`make eval`, CI) runs every `*.jsonl` directly in `evals/datasets/`**, and each must reach the pass rate — a feature's dataset blocks a regression of that feature (the gate used to run `smoke.jsonl` only). Items that need deployed services (an MCP server limited to deployed envs, a `user_jwt` server, a dev gateway, test accounts in a real system) go in **`evals/datasets/live/`**: not gated, run by hand against dev with `make eval DATA=evals/datasets/live/<file>.jsonl`. One file: `make eval DATA=evals/datasets/ltm.jsonl`.
- Build from Agent Spec section 8: ≥ 2 items per main flow, with items for tools, memory, HITL, out-of-scope questions (must decline politely), Vietnamese/English if needed.
- Fixed `id` ⇒ re-pushing doesn't duplicate. `--push` stores it as `<dataset>:<id>`: Langfuse item ids are **global** (unique across all datasets), so the same JSONL can be pushed to several datasets without overwriting each other. Items pushed by an older runner with bare ids are not reused — archive them once in the UI.
- Sensitive datasets: no real PII.

## (2) Offline eval (asset `evals/`)

```bash
make eval                     # local JSONL → Langfuse Experiment (if LANGFUSE_* set) | local run (no Langfuse); LLM_API_KEY always required
make eval MIN=0.9 CONCURRENCY=1   # change the threshold; CONCURRENCY defaults to 1 (MaaS rate limit, below), MIN to 0.8
make eval-push DATA=evals/datasets/ltm.jsonl   # upsert the JSONL to Langfuse Dataset <project>-ltm, then run a Dataset Run
uv run python -m evals.run_eval --dataset <project>-regressions --min-pass-rate 0.85
uv run python -m evals.run_eval --data ... --hitl reject   # test the rejection flow
```

- The task runs the real agent in-process (`service.run_chat`, random user/session, auto-resumes HITL per `--hitl`). **`make eval` executes tools for real**: every tool outside `HITL_TOOLS`, and the approved ones (`--hitl approve`, the default). Run it against dev/test backends and accounts, never production data.
- CLI defaults match `make eval` and the CI job: `--concurrency 1`, `--min-pass-rate 0.8`. `.env` is loaded by `main()`, not when the module is imported.
- MaaS allows **10 requests/minute per account** (all models): each item costs ≥ 2 calls (agent + judge, more with tools/routing) ⇒ use `--concurrency 1` and small datasets on a default account, or request a whitelist; otherwise 429s show up as failed items.
- Evaluators (`evals/evaluators.py`), Langfuse v4 signature:
  - `no_error` — has a reply, status success.
  - `expected_tools` — all expected tools were called.
  - `forbidden_tools` — no forbidden tool called (0 if any).
  - `must_contain` — contains the required phrases.
  - `llm_judge_correctness` — judge (flow `eval_judge`, **reasoning** tier by default) against `expected_output`, 0..1.
  - Run-level: `pass_rate` (an item passes when every score ≥ `PASS_THRESHOLD`=0.7), `avg_correctness`, `avg_expected_tools` (skipped — not recorded as 0 — when no item produced that score, e.g. no `expected_output`).
  - Non-finite scores (a judge returning `NaN`/`Infinity`, which `json.loads` accepts) count as **0** — `min([1.0, nan])` is 1.0 and would pass the item.
- Add business evaluators (valid JSON format, policy compliance, length...) to `ITEM_EVALUATORS`.
- Default `run_name` is `<AGENT_VERSION>-<LLM_MODEL>-<UTC timestamp>` (e.g. `0.1.0-qwen/x-20261015T083005Z`): Langfuse merges runs with the same name, so every rerun gets its own. Compare runs by version/model in the Datasets tab; pass `--run-name` / `EVAL_RUN_NAME` for a fixed name.
- Exit codes: `0` pass · `1` `pass_rate < --min-pass-rate` (**CI gate** — the only quality verdict) · `2` the eval could not run: config/infra error with a one-line `Config error: …` (no `LLM_API_KEY`, neither `--data` nor `--dataset`, missing/invalid JSONL, **empty dataset** (JSONL or Langfuse), unknown Langfuse dataset, Langfuse unreachable or wrong keys — checked **before** any LLM call) or an unexpected runner crash (traceback + `Eval could not run: …`). Fix the setup on `2`, the agent on `1`.
- The gate divides by the **dataset size**: an item whose agent run crashed (LLM 502, timeout…) is recorded as `status=error` and fails; a judge error scores `correctness=0`. Langfuse silently drops raising tasks/evaluators, so never let them raise.

CI: the scaffold already ships the gate — job `eval` in `.github/workflows/ci.yml` (source of truth, don't hand-write a
second one; read it there rather than copying it). What it does:

- Runs in-process (`APP_ENV=local`, `AUTH_MODE=none`, `MEMORY_BACKEND=inmemory`): no runtime, no IAM. MCP servers limited to
  deployed environments and `user_jwt` servers are not loaded ⇒ keep items that need them in `evals/datasets/live/` (not gated).
- `uv sync --locked`, then **evaluates the config of a real environment**: `deploy/env_file.py export` loads
  `LLM_*` (tiers, fallbacks, routing, token caps), `REFLECTION_*`, `LTM_*`, `APP_TIMEZONE`, `MAX_TOOL_ROUNDS`, `HITL_TOOLS`,
  `CONTEXT_*` from the committed `src/backend/.env.<EVAL_CONFIG_ENV>.example` (repo variable `EVAL_CONFIG_ENV`, default `prod`;
  `LLM_API_KEY` is never read from it). Missing file ⇒ a notice and the eval runs on `EVAL_LLM_MODEL` only.
- Uses a **dedicated, repo-level** secret `EVAL_LLM_API_KEY` (eval traffic gets its own rate limit; never the runtime's
  `LLM_API_KEY`; the user sets it with `gh secret set EVAL_LLM_API_KEY`, which prompts without echo — never via the
  chat) — it must be allowed for **every model** that config names (check with `make check-creds` locally).
  `vars.EVAL_LLM_MODEL` (or `vars.LLM_MODEL`) overrides the primary model; otherwise `LLM_MODEL` from the config is used.
- No key or no model ⇒ the gate is **skipped** with a notice (`gate=skipped`) and the deploy job allows **dev only**.
  Otherwise `python -m evals.run_eval --data evals/datasets --min-pass-rate ${EVAL_MIN_PASS_RATE:-0.8}
  --concurrency 1` (every dataset of the directory, each gated; the exit code is the worst) (MaaS allows 10 requests/min per account) ⇒ `gate=passed` or the job fails.

Same gate locally: `make eval MIN=0.8 CONCURRENCY=1`.

Actually run against self-hosted Langfuse v4.49: `--push` creates the dataset, `run_experiment` creates a Dataset Run (prints the UI link), each item's trace has `experiment-item-task` (agent nested inside) and `experiment-item-evaluation` (each evaluator). Read scores via `GET /api/public/v3/scores?name=correctness`.

## (4) Online eval

- `user_feedback`: the frontend sends `{"type":"feedback","trace_id","feedback_token","score","comment"?}` (already wired). `feedback_token` comes from the same response (`done` / `success` / `interrupted`) and binds the trace to the user — without it the backend answers **403**.
- `self_eval`: when the self-evaluation loop is on (below).
- Langfuse managed evaluators (LLM-as-judge on production traces): configure in the Langfuse UI, filter by `tags`/`environment=prod`, sampling 5–20%.

## (5) From production back to the dataset

1. Filter traces with `user_feedback = -1` or `self_eval < 0.5` in Langfuse.
2. Review, write the correct `expected_output`.
3. Add to dataset `<project>-regressions` (UI "Add to dataset" — keeps `source_trace_id`) or to the JSONL.
4. Fix → `make eval` → compare runs → deploy.

## Self-evaluation loop (optional, asset `app/reflection.py`)

- Enable: `REFLECTION_ENABLED=true`, `REFLECTION_MAX_RETRIES=1`, `REFLECTION_CRITERIA="..."` (written for the business), `REFLECTION_MODE` (default `auto`, below), `REFLECTION_TIMEOUT_S` (default 20).
- The judge (flow `judge`, **small** tier by default, output capped at 2048 tokens, thinking off by default — `/agentbase-build-llm` §5–6) grades the final answer as JSON `{pass, score, critique}` and writes the `self_eval` score. It sees the question, **this turn's tool results** (each clipped to 1,500 chars, 6,000 in total) and the answer. Criteria like "accurate to the tool data" can't be judged without the data; without it, correct answers failed and were reset.
- **When the judge runs** (`REFLECTION_MODE`, decided per turn by `reflection_plan`):

  | Mode | The user… | On fail | Use when |
  |---|---|---|---|
  | `gate` | waits for the verdict: node `reflect` runs before `done` (SSE `status` `checking`) | retries left ⇒ the poor answer is removed from history, the critique goes into the **per-turn context message** (after the latest user message, not the system prompt), `agent` answers again; streaming sends `reset` so the frontend clears the old text | short answers where a wrong one is costly |
  | `background` | gets `done` / the JSON response at once; the judge runs **after** it and only scores (`self_eval` on the same trace) | nothing — no retry (the user already has the answer) | monitoring quality without adding latency |
  | `auto` (default) | `gate`, except **background** when a retry is not worth its wait: the turn ran on the **reasoning** tier (`reason=reasoning_tier`), the answer is longer than `REFLECTION_GATE_MAX_CHARS` (default 1200, `long_answer` — streamed, it has been read already, and regenerating it costs as long again), or `REFLECTION_MAX_RETRIES=0` (`no_retries`: a gate could only delay the answer) | as the chosen mode | most agents |

  Why: on a real 10-turn session a 2-call reasoning turn (deepseek-v4-pro) took 85 s, then the small-tier judge hit `REFLECTION_TIMEOUT_S` (20 s) on the 1,369-char answer — the user waited 20 s more for nothing. Trade-off: in `background` a poor answer is only flagged (`self_eval` low ⇒ review loop (5) below), never replaced.
- Background judges are started by `app/service.py` (`judge_after_response`) **inside the request's trace context**: the evaluator `reflection.judge` (metadata `reflection_mode=background`, `reflection_reason`) is a child of `agent.invoke` in the same trace and `self_eval` lands on that trace. They don't need the HTTP connection (the frontend closes the stream at `done`), are bounded per call by `REFLECTION_BACKGROUND_TIMEOUT_S` (default 60 — nobody waits) and by `REFLECTION_BACKGROUND_MAX_CONCURRENCY` per process (default 4; more ⇒ that answer is not scored, WARNING event `reflection.background_skipped` — never an unbounded queue), never raise into the request, and are drained at shutdown (`main.py` lifespan → `drain_background()`: up to 10 s, then cancelled, before the Langfuse flush). The eval runner waits for each item's background judge. They count against the MaaS rate limit like any call.
- Not judged at all (`reflection_plan` → None) when the verdict can't change anything or would be wrong:
  - the turn was rated `simple` by adaptive routing (`REFLECTION_SKIP_SIMPLE`, **only effective with `LLM_ADAPTIVE_ROUTING=true`**: without the router no turn is rated simple);
  - the retries are used up (the retry answer is final whatever the verdict ⇒ no second judge call);
  - the user rejected a tool call in this turn (HITL): the answer explains what was not done.
- Cost: +1 LLM call per turn (+2 per gate retry) ⇒ enable only for use cases needing high accuracy; decide by running `make eval` with/without it.
- Judge error, non-JSON, timeout (WARNING event `reflection.judge_timeout`; `REFLECTION_TIMEOUT_S` for the gate, `REFLECTION_BACKGROUND_TIMEOUT_S` after the response) or a judge config error (`get_llm("judge")` is inside the `try`, event `reflection.judge_failed`) ⇒ the answer is kept (never blocks the user). A non-finite score (`NaN`) counts as missing.

## Test

`tests/test_reflection.py` (retry + removal of the poor answer, no second judge once retries are used up, the judge sees this turn's tool results, skipped after a HITL rejection, timeout and judge config error keep the answer, NaN scores, the `reflection_plan` rules per mode, the gate's `checking` → `reset` → `thinking` stream, background: response before the judge and no retry on fail, judge still scores after the client closed the stream, concurrency limit, timeout / errors never raise, drain waits then cancels, evaluators, CI gate over the whole dataset) and `tests/test_eval.py` (exit codes 1 vs 2, empty dataset ⇒ 2, namespaced `--push` ids, NaN scores, skipped empty averages, `--hitl reject` ⇒ rejected tools are not `tools_used` but count as `tools_requested`, forbidden tools rejected even with `--hitl approve`, CLI defaults and unique run names, `.env` loaded in `main()` only). Add tests for new evaluators.

## Official docs

- [available-models](https://docs.greennode.ai/ai-stack/model-as-a-service/available-models) — MaaS rate limits — size eval concurrency to them
