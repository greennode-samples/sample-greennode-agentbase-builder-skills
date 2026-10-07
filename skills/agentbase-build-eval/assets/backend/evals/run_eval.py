"""Offline evaluation loop (Langfuse v4 Experiments) — run the real agent on a dataset, score it,
compare across runs, block regressions in CI.

  # local dataset (JSONL); results go to Langfuse if keys are set, otherwise runs locally
  uv run python -m evals.run_eval --data evals/datasets/smoke.jsonl --min-pass-rate 0.8

  # sync JSONL to a Langfuse Dataset, then run on the Dataset (Dataset Run, compare in the UI)
  uv run python -m evals.run_eval --data evals/datasets/smoke.jsonl --push --dataset __PROJECT_NAME__-smoke

  # run on an existing Langfuse Dataset (e.g. one collected from 👎 production traces)
  uv run python -m evals.run_eval --dataset __PROJECT_NAME__-regressions

HITL in eval: --hitl approve (default) | reject — answers interrupts automatically to run the full flow.

Exit codes: 0 gate passed · 1 pass_rate < --min-pass-rate (the ONLY quality verdict) ·
            2 the eval could not run (bad args/settings, missing or invalid JSONL, unknown dataset,
              Langfuse unreachable / wrong keys, unexpected crash) — fix the setup, not the agent.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import traceback
import uuid
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from langfuse.api import NotFoundError

load_dotenv()

from app.config import get_settings  # noqa: E402
from app.observability import tracing  # noqa: E402
from app.service import run_chat, run_resume  # noqa: E402
from evals.evaluators import (  # noqa: E402
    ITEM_EVALUATORS,
    RUN_EVALUATORS,
    count_passed,
    item_passed,
)

EXIT_GATE_FAILED, EXIT_CONFIG_ERROR = 1, 2


class ConfigError(Exception):
    """The eval could not run (setup problem) ⇒ exit 2, never confused with a failed quality gate (1)."""


def load_jsonl(path: str) -> list[dict]:
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError as e:
        raise ConfigError(f"cannot read dataset file {path}: {e.strerror or e}") from e
    items = []
    for n, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            items.append(json.loads(line))
        except json.JSONDecodeError as e:
            raise ConfigError(f"{path}:{n}: invalid JSON ({e.msg})") from e
    return items


def _langfuse_url() -> str:
    return os.getenv("LANGFUSE_BASE_URL") or os.getenv("LANGFUSE_HOST") or "Langfuse"


def _langfuse(what: str, fn, *args: Any, not_found: str | None = None, **kwargs: Any) -> Any:
    """Langfuse API call; failures are setup/infra problems (exit 2), not quality regressions."""
    try:
        return fn(*args, **kwargs)
    except NotFoundError as e:
        raise ConfigError(not_found or f"Langfuse {what}: not found") from e
    except Exception as e:  # noqa: BLE001 — network error, 401 wrong keys, 5xx…
        raise ConfigError(
            f"Langfuse {what} failed at {_langfuse_url()} ({type(e).__name__}: {e}) — check "
            "LANGFUSE_BASE_URL/keys, or unset LANGFUSE_PUBLIC_KEY/SECRET_KEY to run locally"
        ) from e


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
        try:
            result = await run_chat(message, user_id=user_id, session_id=session_id)
            for _round in range(5):
                if result.get("status") != "interrupted":
                    break
                decisions = [
                    {"tool_call_id": tc["id"], "action": hitl_action, "reason": "eval"}
                    for tc in result["interrupt"]["tool_calls"]
                ]
                result = await run_resume(decisions, user_id=user_id, session_id=session_id)
        except Exception as e:  # noqa: BLE001
            # Never raise: Langfuse drops raising items and pass_rate would only count the survivors.
            return {"status": "error", "response": "", "error": f"{type(e).__name__}: {e}"}
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
    return {"pass_rate": passed / (len(items) or 1)}


def _parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", help="Local JSONL")
    ap.add_argument("--dataset", help="Langfuse Dataset name")
    ap.add_argument("--push", action="store_true", help="Upsert JSONL into the Langfuse Dataset")
    ap.add_argument("--run-name", default=os.getenv("EVAL_RUN_NAME"))
    ap.add_argument("--hitl", choices=["approve", "reject"], default="approve")
    ap.add_argument("--min-pass-rate", type=float, default=0.0)
    ap.add_argument("--concurrency", type=int, default=4)
    return ap.parse_args()


def main() -> int:
    args = _parse_args()
    try:
        return _run(args)
    except ConfigError as e:
        print(f"Config error: {e}", file=sys.stderr)
    except Exception as e:  # noqa: BLE001 — a crash of the runner itself is not a quality verdict
        traceback.print_exc()
        print(f"Eval could not run: {type(e).__name__}: {e}", file=sys.stderr)
    return EXIT_CONFIG_ERROR


def _push(client: Any, data: str, dataset: str) -> None:
    """Upsert the JSONL into `dataset`. Langfuse item ids are GLOBAL (unique across datasets) ⇒ the id is
    namespaced `<dataset>:<id>`, so the same JSONL can be pushed to several datasets without collisions."""
    items = load_jsonl(data)
    _langfuse(f"create dataset '{dataset}'", client.create_dataset, name=dataset)
    for it in items:
        item_id = it.get("id")
        _langfuse(
            f"upsert item '{item_id}' into dataset '{dataset}'",
            client.create_dataset_item,
            dataset_name=dataset,
            id=f"{dataset}:{item_id}" if item_id else None,
            input=it["input"],
            expected_output=it.get("expected_output"),
            metadata=it.get("metadata"),
        )


def _run(args: argparse.Namespace) -> int:
    try:
        settings = get_settings()
    except ValueError as e:  # e.g. LLM_API_KEY missing — not a quality regression
        errors = getattr(e, "errors", None)
        raise ConfigError(errors()[0]["msg"] if callable(errors) else str(e)) from e
    if not (args.data or args.dataset):
        raise ConfigError("--data <jsonl> or --dataset <Langfuse dataset> is required")
    if args.push and not (args.data and args.dataset):
        raise ConfigError("--push requires --data and --dataset")
    tracing.init_tracing(settings)
    client = tracing.get_client()
    task = make_task(args.hitl)
    run_name = args.run_name or f"{settings.agent_version}-{settings.llm_model}"

    if client is None:
        if not args.data:
            raise ConfigError("No Langfuse key => --data JSONL is required")
        metrics = asyncio.run(run_local(load_jsonl(args.data), task))
    else:
        try:
            # Fail fast (before any paid LLM call) when Langfuse is down or the keys are wrong
            _langfuse("auth check", client.auth_check)
            if args.push:
                _push(client, args.data, args.dataset)
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
                dataset = _langfuse(
                    f"get dataset '{args.dataset}'",
                    client.get_dataset,
                    args.dataset,
                    not_found=f"Langfuse dataset '{args.dataset}' not found — create it with "
                    f"--push --data <jsonl> --dataset {args.dataset}",
                )
                expected = len(dataset.items)
                result = dataset.run_experiment(**common)
            else:
                data = load_jsonl(args.data)
                expected = len(data)
                result = client.run_experiment(data=data, **common)
        finally:
            tracing.shutdown_tracing()
        print(result.format())
        metrics = {e.name: float(e.value) for e in result.run_evaluations}
        # Gate over the WHOLE dataset: an item without a result (dropped by Langfuse) counts as failed
        done = len(result.item_results)
        if done < expected:
            print(
                f"WARNING: {expected - done}/{expected} items produced no result", file=sys.stderr
            )
        metrics["pass_rate"] = count_passed(result.item_results) / (expected or 1)

    rate = metrics.get("pass_rate", 0.0)
    print(f"pass_rate={rate:.2%} (min {args.min_pass_rate:.0%})")
    return 0 if rate >= args.min_pass_rate else EXIT_GATE_FAILED


if __name__ == "__main__":
    sys.exit(main())
