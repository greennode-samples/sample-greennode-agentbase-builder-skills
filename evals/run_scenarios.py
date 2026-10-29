"""End-to-end eval: can a coding agent build a working, standard agent from a business request with these skills?

For each scenario in scenarios.json this starts a headless Claude Code (`claude -p`) in a new empty directory with
THIS repo loaded as a plugin, gives it the business request (offline: no platform resources, no deploy), lets it
work until it stops, then grades the project it produced. Grading is objective, never an LLM opinion:

  common    the project exists; make test, make lint and make security-scan pass; docs/agent-spec.md was written
  scenario  tools exist (@tool in src/backend/app), side-effect tools are in HITL_TOOLS of every deploy env file and
            read tools are not, MCP servers declared with allow_tools, knowledge documents, eval items and tags,
            tests that exercise the new tools

    python3 evals/run_scenarios.py --only hr-leave              # one scenario (~15–40 min, a full coding session)
    python3 evals/run_scenarios.py --keep                       # keep the generated projects to inspect them
    python3 evals/run_scenarios.py --grade <project-dir> --scenario hr-leave   # grade an existing project only

Uses your Claude Code login and a full session's quota per scenario. The coding agent runs with edits and a
restricted set of shell commands allowed (make, uv, bash, git, python3, ls, cat…) and network tools denied; it is told
not to touch the platform, but its platform skills are installed — run it with an account whose GreenNode credentials
are absent from the environment. Results: evals/results/scenarios-<timestamp>.json.
"""

from __future__ import annotations

import argparse
import ast
import fnmatch
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SPEC = json.loads(Path(__file__).with_name("scenarios.json").read_text(encoding="utf-8"))
RESULTS = Path(__file__).with_name("results")
DEPLOY_ENVS = ("dev", "staging", "prod")
ALLOWED = [
    "Skill", "Read", "Write", "Edit", "Glob", "Grep", "TodoWrite",
    "Bash(make:*)", "Bash(uv:*)", "Bash(bash:*)", "Bash(git:*)", "Bash(python3:*)", "Bash(ls:*)", "Bash(cat:*)",
    "Bash(mkdir:*)", "Bash(cp:*)", "Bash(mv:*)", "Bash(grep:*)", "Bash(find:*)", "Bash(sed:*)", "Bash(head:*)",
    "Bash(tail:*)", "Bash(wc:*)", "Bash(cd:*)", "Bash(echo:*)", "Bash(test:*)", "Bash(diff:*)",
]  # fmt: skip
# What the template already ships — a scenario's checks only count what the agent added on top
TEMPLATE_TESTS = " ".join(p.read_text(encoding="utf-8") for p in (REPO / "skills").glob("*/assets/backend/tests/test_*.py"))
TEMPLATE_EVAL_IDS = {
    json.loads(ln).get("id")
    for ds in (REPO / "skills").glob("*/assets/backend/evals/datasets/*.jsonl")
    for ln in ds.read_text(encoding="utf-8").splitlines()
    if ln.strip()
}
DENIED = ["WebFetch", "WebSearch", "Bash(curl:*)", "Bash(wget:*)", "Bash(grn:*)", "Bash(docker:*)"]


def run_agent(scenario: dict, workdir: Path, model: str | None, max_turns: int) -> dict:
    prompt = scenario["prompt"] + "\n\n" + SPEC["common_prompt"].format(name=scenario["name"])
    cmd = [
        "claude", "-p", prompt, "--output-format", "json", "--plugin-dir", str(REPO),
        "--permission-mode", "acceptEdits", "--max-turns", str(max_turns),
        "--allowed-tools", *ALLOWED, "--disallowed-tools", *DENIED,
    ]  # fmt: skip
    if model:
        cmd += ["--model", model]
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GREENNODE_", "GRN_"))}
    started = time.monotonic()
    proc = subprocess.run(cmd, cwd=workdir, capture_output=True, text=True, env=env, timeout=3 * 3600)
    try:
        out = json.loads(proc.stdout)
    except json.JSONDecodeError:
        out = {"is_error": True, "result": proc.stdout[-2000:] or proc.stderr[-2000:]}
    return {
        "minutes": round((time.monotonic() - started) / 60, 1),
        "turns": out.get("num_turns"),
        "cost_usd": out.get("total_cost_usd"),
        "error": out.get("is_error"),
        "final_message": str(out.get("result", ""))[-1500:],
    }


def find_project(workdir: Path) -> Path | None:
    """The directory holding src/backend (the agent may scaffold into a sub-folder)."""
    for backend in sorted(workdir.rglob("src/backend/pyproject.toml")):
        if ".venv" not in backend.parts:
            return backend.parent.parent.parent
    return None


def _env_file(path: Path) -> dict[str, str]:
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip()
    return out


def _tools(backend: Path) -> dict[str, Path]:
    found = {}
    for py in (backend / "app").rglob("*.py"):
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for d in node.decorator_list:
                    target = d.func if isinstance(d, ast.Call) else d
                    if getattr(target, "id", getattr(target, "attr", None)) == "tool":
                        name = node.name
                        if isinstance(d, ast.Call) and d.args and isinstance(d.args[0], ast.Constant):
                            name = str(d.args[0].value)
                        found[name] = py
    return found


def _make(project: Path, target: str) -> tuple[bool, str]:
    p = subprocess.run(["make", target], cwd=project, capture_output=True, text=True, timeout=1800)
    return p.returncode == 0, (p.stdout + p.stderr)[-600:]


def grade(project: Path | None, scenario: dict) -> list[dict]:
    results: list[dict] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        results.append({"check": name, "ok": bool(ok), "detail": detail})

    check("project created", project is not None, "no src/backend/pyproject.toml found")
    if project is None:
        return results
    backend = project / "src/backend"
    for target in ("test", "lint", "security-scan"):
        ok, tail = _make(project, target)
        check(f"make {target}", ok, "" if ok else tail)
    check("agent spec written", (project / "docs/agent-spec.md").exists(), "docs/agent-spec.md")

    c = scenario["checks"]
    tools = _tools(backend)
    for t in c.get("tools", []):
        check(f"tool {t} exists", t in tools)
    envs = {e: _env_file(backend / f".env.{e}.example") for e in DEPLOY_ENVS if (backend / f".env.{e}.example").exists()}
    check("deploy env files", len(envs) == len(DEPLOY_ENVS), f"found {sorted(envs)}")
    for env, values in envs.items():
        try:
            patterns = json.loads(values.get("HITL_TOOLS") or "[]")
        except json.JSONDecodeError:
            patterns = []
        for t in c.get("hitl", []):
            check(f"{env}: {t} needs approval", any(fnmatch.fnmatch(t, p) for p in patterns), str(patterns))
        for t in c.get("not_hitl", []):
            check(f"{env}: {t} runs without approval", not any(fnmatch.fnmatch(t, p) for p in patterns), str(patterns))
    if servers := c.get("mcp_servers"):
        try:
            cfg = json.loads((backend / "mcp_servers.json").read_text(encoding="utf-8")).get("servers", {})
        except (OSError, json.JSONDecodeError):
            cfg = {}
        for name, wanted in servers.items():
            entry = cfg.get(name) or {}
            check(f"MCP server {name} declared", bool(entry))
            check(f"MCP server {name} has explicit auth", "auth" in entry)
            check(f"MCP server {name} allow_tools", sorted(entry.get("allow_tools") or []) == sorted(wanted),
                  str(entry.get("allow_tools")))  # fmt: skip
    if n := c.get("knowledge_files"):
        docs = [p for p in (backend / "app/knowledge").rglob("*") if p.is_file() and p.suffix in (".md", ".txt")]
        check(f"≥{n} knowledge documents", len(docs) >= n, str(len(docs)))
    items = []
    for ds in (backend / "evals/datasets").glob("*.jsonl"):
        items += [json.loads(ln) for ln in ds.read_text(encoding="utf-8").splitlines() if ln.strip()]
    new_items = [i for i in items if i.get("id") not in TEMPLATE_EVAL_IDS]
    if n := c.get("eval_items_min"):
        check(f"≥{n} new eval items", len(new_items) >= n, str(len(new_items)))
    for tag in c.get("eval_tags", []):
        check(f"eval item tagged {tag}", any(tag in ((i.get("metadata") or {}).get("tags") or []) for i in new_items))
    tests = " ".join(p.read_text(encoding="utf-8") for p in (backend / "tests").glob("test_*.py"))
    for word in c.get("test_mentions", []):
        check(f"tests exercise {word}", tests.count(word) > TEMPLATE_TESTS.count(word))
    return results


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--only", help="comma-separated scenario ids")
    ap.add_argument("--model")
    ap.add_argument("--max-turns", type=int, default=200)
    ap.add_argument("--keep", action="store_true", help="keep the generated projects")
    ap.add_argument("--grade", type=Path, help="grade this existing project directory (no agent run)")
    ap.add_argument("--scenario", help="scenario id for --grade")
    args = ap.parse_args()

    scenarios = SPEC["scenarios"]
    if args.only or args.scenario:
        wanted = set((args.only or args.scenario).split(","))
        scenarios = [s for s in scenarios if s["id"] in wanted]
    report = []
    for sc in scenarios:
        if args.grade:
            run, checks, where = {}, grade(find_project(args.grade.resolve()), sc), str(args.grade)
        else:
            workdir = Path(tempfile.mkdtemp(prefix=f"scenario-{sc['id']}-"))
            subprocess.run(["git", "init", "-q", str(workdir)], check=True)
            print(f"▶ {sc['id']}: agent working in {workdir} …", flush=True)
            run = run_agent(sc, workdir, args.model, args.max_turns)
            checks, where = grade(find_project(workdir), sc), str(workdir)
            if not args.keep:
                shutil.rmtree(workdir, ignore_errors=True)
                where += " (deleted)"
        passed = sum(c["ok"] for c in checks)
        print(f"\n{sc['id']}: {passed}/{len(checks)} checks  {json.dumps(run, ensure_ascii=False)[:300]}")
        for c in checks:
            print(f"  {'PASS' if c['ok'] else 'FAIL'}  {c['check']}" + ("" if c["ok"] else f"  — {c['detail'][:200]}"))
        report.append({"scenario": sc["id"], "project": where, "run": run, "score": passed / max(len(checks), 1),
                       "checks": checks})  # fmt: skip
    RESULTS.mkdir(exist_ok=True)
    out = RESULTS / f"scenarios-{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"\nresults: {out.relative_to(REPO)}")
    return 0 if report and all(r["score"] == 1 for r in report) else 1


if __name__ == "__main__":
    sys.exit(main())
