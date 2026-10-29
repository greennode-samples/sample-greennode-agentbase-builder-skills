"""Routing eval: does a coding agent pick the right skill for a request? (standard library only, Python >= 3.9)

Each line of triggers.jsonl is a user request and the skill(s) that should handle it (`expect`; "none" = no skill).
For every request this starts a fresh, headless Claude Code (`claude -p`) in an empty directory with THIS repo loaded
as a plugin, lets it call only the Skill tool, and records the first skill it invokes — then stops the run, so a case
costs one model turn. Cases with "in_project": true run inside a freshly scaffolded standard project (scaffold.sh
--no-sync, created once per eval run), as real requests about an existing agent do: the repo changes what is right. Your user-level skills stay installed (e.g. the platform agentbase-* set), because the real
conflicts are with them.

    python3 evals/run_triggers.py                       # all cases, 1 run each
    python3 evals/run_triggers.py --runs 3 --jobs 4     # 3 runs per case (routing is not deterministic)
    python3 evals/run_triggers.py --only hitl-vi,neg-logs --model sonnet
    python3 evals/run_triggers.py --min 0.9             # exit 1 below 90% (CI / before merging a description change)
    python3 evals/run_triggers.py --file evals/triggers_heldout.jsonl --runs 2   # the held-out set: see evals/README.md

Uses your Claude Code login and quota: ~1 turn per case per run. Results: evals/results/triggers-<timestamp>.json.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CASES = Path(__file__).with_name("triggers.jsonl")
RESULTS = Path(__file__).with_name("results")
TIMEOUT_S = 180
# What the runs ran on, taken from the first `system/init` event: routing depends on the model, the Claude Code version
# and the OTHER skills installed (a platform skill that claims "any build request" competes with ours), so a score is
# only comparable with another one from the same environment — it is stored with the results
ENVIRONMENT: dict = {}


def scaffold_project(root: Path) -> Path:
    project = root / "demo-agent"
    script = REPO / "skills/agentbase-build-scaffold/scripts/scaffold.sh"
    subprocess.run(["bash", str(script), "demo-agent", str(project), "--no-sync"], check=True, capture_output=True)
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    return project


def first_skill(prompt: str, model: str | None, project: Path | None = None) -> str:
    """Name of the first skill Claude invokes for `prompt` ('none' if it answers without one, 'error:…' on failure)."""
    cmd = [
        "claude", "-p", prompt,
        "--output-format", "stream-json", "--verbose",
        "--plugin-dir", str(REPO),
        "--allowed-tools", "Skill",
        "--max-turns", "2",
    ]  # fmt: skip
    if model:
        cmd += ["--model", model]
    with tempfile.TemporaryDirectory(prefix="trigger-eval-") as empty:
        cwd = str(project or empty)
        proc = subprocess.Popen(
            cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, env={**os.environ}
        )
        deadline = time.monotonic() + TIMEOUT_S
        result: dict = {}
        try:
            for line in proc.stdout:  # type: ignore[union-attr]
                if time.monotonic() > deadline:
                    return "error:timeout"
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("type") == "system" and event.get("subtype") == "init" and not ENVIRONMENT:
                    ENVIRONMENT.update(
                        model=event.get("model"),
                        claude_code_version=event.get("claude_code_version"),
                        plugins=sorted(f"{p.get('name')}@{p.get('version', '?')}" for p in event.get("plugins", [])),
                        other_skills=sorted(s for s in event.get("skills", []) if not s.startswith("agentbase-build:")),
                    )
                if event.get("type") == "result":
                    result = event
                if event.get("type") != "assistant":
                    continue
                for block in event.get("message", {}).get("content", []):
                    if block.get("type") == "tool_use" and block.get("name") == "Skill":
                        return normalize(str(block.get("input", {}).get("skill", "")))
            proc.wait()
            # Answered, or ran out of turns trying other (denied) tools: either way no skill was chosen
            if result and (not result.get("is_error") or result.get("subtype") == "error_max_turns"):
                return "none"
            return f"error:{result.get('subtype') or 'exit'}"
        finally:
            if proc.poll() is None:
                proc.kill()


def normalize(skill: str) -> str:
    """'agentbase-build:agentbase-build-hitl' or '/agentbase-build-hitl' → 'agentbase-build-hitl'."""
    return skill.strip().lstrip("/").split(":")[-1]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--model")
    ap.add_argument("--only", help="comma-separated case ids")
    ap.add_argument("--min", type=float, default=0.0, help="minimum accuracy (0–1)")
    ap.add_argument(
        "--file",
        type=Path,
        default=CASES,
        help="cases file (default triggers.jsonl, the set descriptions are tuned on; triggers_heldout.jsonl is not)",
    )
    args = ap.parse_args()

    cases = [json.loads(ln) for ln in args.file.read_text(encoding="utf-8").splitlines() if ln.strip()]
    if args.only:
        wanted = set(args.only.split(","))
        cases = [c for c in cases if c["id"] in wanted]
    jobs = [(c, i) for c in cases for i in range(args.runs)]
    with tempfile.TemporaryDirectory(prefix="trigger-project-") as tmp:
        project = scaffold_project(Path(tmp)) if any(c.get("in_project") for c in cases) else None
        with ThreadPoolExecutor(max_workers=args.jobs) as pool:
            got = list(
                pool.map(
                    lambda job: first_skill(job[0]["prompt"], args.model, project if job[0].get("in_project") else None),
                    jobs,
                )
            )

    rows = []
    per_case: dict[str, list[bool]] = defaultdict(list)
    confusion: Counter[tuple[str, str]] = Counter()
    for (case, _), skill in zip(jobs, got):
        ok = skill in case["expect"]
        per_case[case["id"]].append(ok)
        rows.append({"id": case["id"], "expect": case["expect"], "got": skill, "ok": ok})
        if not ok:
            confusion[(case["expect"][0], skill)] += 1

    for case in cases:
        results = per_case[case["id"]]
        picks = [r["got"] for r in rows if r["id"] == case["id"]]
        mark = "PASS" if all(results) else ("FLAKY" if any(results) else "FAIL")
        print(f"{mark:<6} {case['id']:<22} expect {'/'.join(case['expect']):<40} got {', '.join(picks)}")
    accuracy = sum(r["ok"] for r in rows) / max(len(rows), 1)
    print(f"\naccuracy {accuracy:.0%} over {len(rows)} runs ({len(cases)} cases × {args.runs})")
    if ENVIRONMENT:
        print(
            f"environment: model {ENVIRONMENT.get('model')}, Claude Code {ENVIRONMENT.get('claude_code_version')}, "
            f"{len(ENVIRONMENT.get('other_skills', []))} other skills installed"
        )
    for (want, got_skill), n in confusion.most_common():
        print(f"  confused {want} → {got_skill} ×{n}")

    RESULTS.mkdir(exist_ok=True)
    out = RESULTS / f"triggers-{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.write_text(
        json.dumps(
            {"model": args.model, "environment": ENVIRONMENT, "accuracy": accuracy, "rows": rows},
            ensure_ascii=False,
            indent=2,
        )
    )
    print(f"results: {out.relative_to(REPO)}")
    return 0 if accuracy >= args.min else 1


if __name__ == "__main__":
    sys.exit(main())
