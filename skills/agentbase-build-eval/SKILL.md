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
- Split datasets by optional feature: `smoke.jsonl` (always runs), `ltm.jsonl` (only when `LTM_ENABLED=true`), `make eval DATA=evals/datasets/ltm.jsonl`.
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
  deployed environments and `user_jwt` servers are not loaded ⇒ keep items that need them in a separate dataset.
- `uv sync --locked`, then **evaluates the config of a real environment**: `deploy/env_file.py export` loads
  `LLM_*` (tiers, fallbacks, routing, token caps), `REFLECTION_*`, `LTM_*`, `APP_TIMEZONE`, `MAX_TOOL_ROUNDS`, `HITL_TOOLS`,
  `CONTEXT_*` from the committed `src/backend/.env.<EVAL_CONFIG_ENV>.example` (repo variable `EVAL_CONFIG_ENV`, default `prod`;
  `LLM_API_KEY` is never read from it). Missing file ⇒ a notice and the eval runs on `EVAL_LLM_MODEL` only.
- Uses a **dedicated, repo-level** secret `EVAL_LLM_API_KEY` (eval traffic gets its own rate limit; never the runtime's
  `LLM_API_KEY`) — it must be allowed for **every model** that config names (check with `make check-creds` locally).
  `vars.EVAL_LLM_MODEL` (or `vars.LLM_MODEL`) overrides the primary model; otherwise `LLM_MODEL` from the config is used.
- No key or no model ⇒ the gate is **skipped** with a notice (`gate=skipped`) and the deploy job allows **dev only**.
  Otherwise `python -m evals.run_eval --data evals/datasets/smoke.jsonl --min-pass-rate ${EVAL_MIN_PASS_RATE:-0.8}
  --concurrency 1` (MaaS allows 10 requests/min per account) ⇒ `gate=passed` or the job fails.

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

## In-graph self-evaluation loop (optional, asset `app/reflection.py`)

- Enable: `REFLECTION_ENABLED=true`, `REFLECTION_MAX_RETRIES=1`, `REFLECTION_CRITERIA="..."` (written for the business), `REFLECTION_TIMEOUT_S` (default 20).
- After the final answer, node `reflect` uses a judge (flow `judge`, **small** tier by default, output capped at 2048 tokens: the user is waiting, so give it a fast low-reasoning model, see `/agentbase-build-llm` §5) to grade JSON `{pass, score, critique}`. The judge sees the question, **this turn's tool results** (each clipped to 1,500 chars, 6,000 in total) and the answer. Criteria like "accurate to the tool data" can't be judged without the data; without it, correct answers failed and were reset. On fail with retries left ⇒ remove the poor answer from history, put the critique into the **per-turn context message** (after the latest user message, not the system prompt), `agent` answers again. Streaming sends a `reset` event so the frontend clears the old text.
- Skipped (`should_reflect`) when the verdict can't change anything or would be wrong:
  - the turn was rated `simple` by adaptive routing (`REFLECTION_SKIP_SIMPLE`, **only effective with `LLM_ADAPTIVE_ROUTING=true`**: without the router no turn is rated simple);
  - the retries are used up (the retry answer is final whatever the verdict ⇒ no second judge call);
  - the user rejected a tool call in this turn (HITL): the answer explains what was not done.
- Cost: +1 LLM call per turn (+2 per retry), higher latency ⇒ enable only for use cases needing high accuracy; decide by running `make eval` with/without it.
- Judge error, non-JSON, timeout (`REFLECTION_TIMEOUT_S`, WARNING event `reflection.judge_timeout`) or a judge config error (`get_llm("judge")` is inside the `try`) ⇒ the answer is kept (never blocks the user). A non-finite score (`NaN`) counts as missing.

## Test

`tests/test_reflection.py` (retry + removal of the poor answer, no second judge once retries are used up, the judge sees this turn's tool results, skipped after a HITL rejection, timeout and judge config error keep the answer, NaN scores, evaluators, CI gate over the whole dataset) and `tests/test_eval.py` (exit codes 1 vs 2, empty dataset ⇒ 2, namespaced `--push` ids, NaN scores, skipped empty averages, `--hitl reject` ⇒ rejected tools are not `tools_used` but count as `tools_requested`, forbidden tools rejected even with `--hitl approve`, CLI defaults and unique run names, `.env` loaded in `main()` only). Add tests for new evaluators.

## Official docs

- [available-models](https://docs.greennode.ai/ai-stack/model-as-a-service/available-models) — MaaS rate limits — size eval concurrency to them
