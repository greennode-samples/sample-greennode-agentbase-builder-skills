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
- `forbidden_tools`: tools that must **not** be called (e.g. an injection message must not call `send_email`; a policy-denied message must not retry the tool). `user_id`: run the item as a fixed user (multi-turn LTM/isolation tests); empty ⇒ random user.
- Split datasets by optional feature: `smoke.jsonl` (always runs), `ltm.jsonl` (only when `LTM_ENABLED=true`), `make eval DATA=evals/datasets/ltm.jsonl`.
- Build from Agent Spec section 8: ≥ 2 items per main flow, with items for tools, memory, HITL, out-of-scope questions (must decline politely), Vietnamese/English if needed.
- Fixed `id` ⇒ upserting to Langfuse doesn't duplicate.
- Sensitive datasets: no real PII.

## (2) Offline eval (asset `evals/`)

```bash
make eval                     # local JSONL → Langfuse Experiment (if keys set) | local run (no keys)
make eval MIN=0.9             # change the threshold
make eval-push                # upsert JSONL to a Langfuse Dataset, then run a Dataset Run
uv run python -m evals.run_eval --dataset <project>-regressions --min-pass-rate 0.85
uv run python -m evals.run_eval --data ... --hitl reject   # test the rejection flow
```

- The task runs the real agent in-process (`service.run_chat`, random user/session, auto-resumes HITL per `--hitl`).
- Evaluators (`evals/evaluators.py`), Langfuse v4 signature:
  - `no_error` — has a reply, status success.
  - `expected_tools` — all expected tools were called.
  - `forbidden_tools` — no forbidden tool called (0 if any).
  - `must_contain` — contains the required phrases.
  - `llm_judge_correctness` — judge (flow `eval_judge`, **reasoning** tier by default) against `expected_output`, 0..1.
  - Run-level: `pass_rate` (an item passes when every score ≥ `PASS_THRESHOLD`=0.7), `avg_correctness`, `avg_expected_tools`.
- Add business evaluators (valid JSON format, policy compliance, length...) to `ITEM_EVALUATORS`.
- Default `run_name` is `<AGENT_VERSION>-<LLM_MODEL>` ⇒ compare runs by version/model in the Datasets tab.
- Exit code ≠ 0 when `pass_rate < --min-pass-rate` ⇒ use as a **CI gate**.

CI (GitHub Actions example):

```yaml
- uses: astral-sh/setup-uv@v6
- run: cd src/backend && uv sync --frozen
- run: make test
- run: make eval MIN=0.8
  env: { LLM_API_KEY: ${{ secrets.LLM_API_KEY }}, LLM_MODEL: ..., MEMORY_BACKEND: inmemory, APP_ENV: local,
         AUTH_MODE: none, LANGFUSE_PUBLIC_KEY: ..., LANGFUSE_SECRET_KEY: ..., LANGFUSE_BASE_URL: ... }
```

Actually run against self-hosted Langfuse v4.49: `--push` creates the dataset, `run_experiment` creates a Dataset Run (prints the UI link), each item's trace has `experiment-item-task` (agent nested inside) and `experiment-item-evaluation` (each evaluator). Read scores via `GET /api/public/v3/scores?name=correctness`.

## (4) Online eval

- `user_feedback`: the frontend sends `{"type":"feedback","trace_id","score"}` (already wired).
- `self_eval`: when the self-evaluation loop is on (below).
- Langfuse managed evaluators (LLM-as-judge on production traces): configure in the Langfuse UI, filter by `tags`/`environment=prod`, sampling 5–20%.

## (5) From production back to the dataset

1. Filter traces with `user_feedback = -1` or `self_eval < 0.5` in Langfuse.
2. Review, write the correct `expected_output`.
3. Add to dataset `<project>-regressions` (UI "Add to dataset" — keeps `source_trace_id`) or to the JSONL.
4. Fix → `make eval` → compare runs → deploy.

## In-graph self-evaluation loop (optional, asset `app/reflection.py`)

- Enable: `REFLECTION_ENABLED=true`, `REFLECTION_MAX_RETRIES=1`, `REFLECTION_CRITERIA="..."` (written for the business).
- After the final answer, node `reflect` uses a judge (flow `judge`, large tier by default — fast because the user is waiting) to grade JSON `{pass, score, critique}`; on fail with retries left ⇒ remove the poor answer from history, put the critique into the system prompt, `agent` answers again. Streaming sends a `reset` event so the frontend clears the old text.
- Cost: +1 LLM call per turn (+2 per retry), higher latency ⇒ enable only for use cases needing high accuracy; decide by running `make eval` with/without it.
- Judge error/non-JSON ⇒ treated as pass (never blocks the user).

## Test

`tests/test_reflection.py` (retry + removal of the poor answer, evaluators). Add tests for new evaluators.
