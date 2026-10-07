"""Offline evaluation loop (Langfuse v4 Experiments) — run the real agent on a dataset, score it,
compare across runs, block regressions in CI.

  # local dataset (JSONL); results go to Langfuse if keys are set, otherwise runs locally
  uv run python -m evals.run_eval --data evals/datasets/smoke.jsonl --min-pass-rate 0.8

  # sync JSONL to a Langfuse Dataset, then run on the Dataset (Dataset Run, compare in the UI)
  uv run python -m evals.run_eval --data evals/datasets/smoke.jsonl --push --dataset __PROJECT_NAME__-smoke

  # run on an existing Langfuse Dataset (e.g. one collected from 👎 production traces)
  uv run python -m evals.run_eval --dataset __PROJECT_NAME__-regressions

HITL in eval: --hitl approve (default) | reject — answers interrupts automatically to run the full flow.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

load_dotenv()

from app.config import get_settings  # noqa: E402
from app.observability import tracing  # noqa: E402
from app.service import run_chat, run_resume  # noqa: E402
from evals.evaluators import (  # noqa: E402
    ITEM_EVALUATORS,
    RUN_EVALUATORS,
    item_passed,
)


def load_jsonl(path: str) -> list[dict]:
    return [
        json.loads(line)
        for line in Path(path).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _field(item: Any, name: str) -> Any:
    return item.get(name) if isinstance(item, dict) else getattr(item, name, None)


def make_task(hitl_action: str):
    async def task(*, item: Any, **_: Any) -> dict:
        inp = _field(item, "input")
        message = inp.get("message") if isinstance(inp, dict) else str(inp)
        meta = _field(item, "metadata") or {}
        # metadata.user_id: run the item under a fixed identity (per-user seeded data); random by default
        user_id = meta.get("user_id") or f"eval-{uuid.uuid4().hex[:8]}"
        session_id = str(uuid.uuid4())
        result = await run_chat(message, user_id=user_id, session_id=session_id)
        for _round in range(5):
            if result.get("status") != "interrupted":
                break
            decisions = [
                {"tool_call_id": tc["id"], "action": hitl_action, "reason": "eval"}
                for tc in result["interrupt"]["tool_calls"]
            ]
            result = await run_resume(decisions, user_id=user_id, session_id=session_id)
        return result

    return task


async def run_local(items: list[dict], task) -> dict[str, float]:
    """Fallback without Langfuse: run sequentially, print results."""
    rows = []
    for it in items:
        out = await task(item=it)
        evals = []
        for ev in ITEM_EVALUATORS:
            r = ev(
                input=it.get("input"),
                output=out,
                expected_output=it.get("expected_output"),
                metadata=it.get("metadata"),
            )
            r = await r if asyncio.iscoroutine(r) else r
            if r:
                evals.append(r)
        rows.append(evals)
        status = "PASS" if item_passed(evals) else "FAIL"
        print(f"[{status}] {it.get('id')}: " + ", ".join(f"{e.name}={e.value:.2f}" for e in evals))
    passed = sum(item_passed(e) for e in rows)
    return {"pass_rate": passed / (len(rows) or 1)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", help="Local JSONL")
    ap.add_argument("--dataset", help="Langfuse Dataset name")
    ap.add_argument("--push", action="store_true", help="Upsert JSONL into the Langfuse Dataset")
    ap.add_argument("--run-name", default=os.getenv("EVAL_RUN_NAME"))
    ap.add_argument("--hitl", choices=["approve", "reject"], default="approve")
    ap.add_argument("--min-pass-rate", type=float, default=0.0)
    ap.add_argument("--concurrency", type=int, default=4)
    args = ap.parse_args()

    settings = get_settings()
    tracing.init_tracing(settings)
    client = tracing.get_client()
    task = make_task(args.hitl)
    run_name = args.run_name or f"{settings.agent_version}-{settings.llm_model}"

    if client is None:
        if not args.data:
            print("No Langfuse key => --data JSONL is required", file=sys.stderr)
            return 2
        metrics = asyncio.run(run_local(load_jsonl(args.data), task))
    else:
        if args.push:
            if not (args.data and args.dataset):
                print("--push requires --data and --dataset", file=sys.stderr)
                return 2
            client.create_dataset(name=args.dataset)
            for it in load_jsonl(args.data):
                client.create_dataset_item(
                    dataset_name=args.dataset,
                    id=it.get("id"),
                    input=it["input"],
                    expected_output=it.get("expected_output"),
                    metadata=it.get("metadata"),
                )
        common = dict(
            name=f"{settings.agent_name}-eval",
            run_name=run_name,
            task=task,
            evaluators=ITEM_EVALUATORS,
            run_evaluators=RUN_EVALUATORS,
            max_concurrency=args.concurrency,
            metadata={"model": settings.llm_model, "version": settings.agent_version},
        )
        if args.dataset:
            result = client.get_dataset(args.dataset).run_experiment(**common)
        else:
            result = client.run_experiment(data=load_jsonl(args.data), **common)
        print(result.format())
        metrics = {e.name: float(e.value) for e in result.run_evaluations}
        tracing.shutdown_tracing()

    rate = metrics.get("pass_rate", 0.0)
    print(f"pass_rate={rate:.2%} (min {args.min_pass_rate:.0%})")
    return 0 if rate >= args.min_pass_rate else 1


if __name__ == "__main__":
    sys.exit(main())
