"""Routing eval for models that Claude Code cannot run: GPT, DeepSeek, GLM, Gemini, Qwen... (standard library only, Python >= 3.9)

Same cases and grading as run_triggers.py, but it SIMULATES how a coding agent shows skills to its model instead of
running a real agent: one chat-completions call to any OpenAI-compatible endpoint, with

  - a system prompt listing every available skill as `name: description` (this repo's + the user-level skills you have
    installed, e.g. the platform's agentbase-* set — the real conflicts are with them),
  - the project's AGENTS.md for `"in_project": true` cases (agents load it into context on their own),
  - one tool, `Skill(skill)`, that the model should call before doing anything else when a skill matches.

The first `Skill` call is the pick; no call = "none". This measures what the skill descriptions and AGENTS.md can fix
(wording that a model reads), not how Codex / Gemini CLI / Cursor / OpenCode load skills: they differ in how they list
them and how hard they push the model to use one. Treat a score here as the floor-check for "can a model of this size
tell the skills apart", and confirm with the real agent when it matters.

    export LLM_API_KEY=...                                   # never pass a key on the command line
    python3 evals/run_triggers_api.py --api-key-file ~/.maas-key --list-models   # which models can this key call?
    python3 evals/run_triggers_api.py --model deepseek-v3 --runs 2
    python3 evals/run_triggers_api.py --model glm-4.6 --model gpt-oss-120b --file evals/triggers_heldout.jsonl
    python3 evals/run_triggers_api.py --base-url https://api.openai.com/v1 --api-key-env OPENAI_API_KEY --model gpt-5
    python3 evals/run_triggers_api.py --no-competitors ...   # only this repo's skills (isolates our descriptions)

Results: evals/results/triggers-api-<timestamp>.json (git-ignored), one file for all models of the run.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CASES = Path(__file__).with_name("triggers.jsonl")
RESULTS = Path(__file__).with_name("results")
DEFAULT_BASE_URL = "https://maas-llm-aiplatform-hcm.api.vngcloud.vn/v1"
AGENTS_MD = REPO / "skills/agentbase-build-scaffold/assets/root/AGENTS.md"

SYSTEM = """You are a coding agent working for a developer in their terminal.

Skills are packaged instructions for particular kinds of task. The skills available to you are listed below as
`name: description`. When the developer's request matches a skill, call the Skill tool with that skill's exact name
BEFORE doing anything else — a skill carries knowledge you do not have. If no skill fits, answer normally and call no tool.

Available skills:
{skills}
"""

TOOL = {
    "type": "function",
    "function": {
        "name": "Skill",
        "description": "Load a skill by its exact name from the list of available skills, before starting the task.",
        "parameters": {
            "type": "object",
            "properties": {"skill": {"type": "string", "description": "Exact skill name."}},
            "required": ["skill"],
        },
    },
}


def frontmatter(path: Path) -> tuple[str, str] | None:
    """(name, description) from a SKILL.md, or None. Handles quoted, plain and folded (`>`) descriptions."""
    text = path.read_text(encoding="utf-8")
    m = re.match(r"---\n(.*?)\n---", text, re.S)
    if not m:
        return None
    name, desc, lines = "", "", m.group(1).splitlines()
    for i, line in enumerate(lines):
        if line.startswith("name:"):
            name = line.split(":", 1)[1].strip().strip("\"'")
        elif line.startswith("description:"):
            value = line.split(":", 1)[1].strip()
            if value in (">", "|", ">-", "|-", ""):
                block = []
                for nxt in lines[i + 1 :]:
                    if nxt and not nxt[0].isspace():
                        break
                    block.append(nxt.strip())
                value = " ".join(b for b in block if b)
            desc = value.strip("\"'")
    return (name, desc) if name and desc else None


def load_skills(competitors: bool, extra_dirs: list[Path]) -> tuple[list[tuple[str, str]], int]:
    """This repo's skills first, then the user-level ones (they are the real competition). Returns (skills, n_other)."""
    own = [fm for p in sorted((REPO / "skills").glob("*/SKILL.md")) if (fm := frontmatter(p))]
    other: list[tuple[str, str]] = []
    if competitors:
        roots = [Path.home() / ".claude" / "skills", *extra_dirs]
        seen = {n for n, _ in own}
        for root in roots:
            for p in sorted(root.glob("*/SKILL.md")):
                fm = frontmatter(p)
                if fm and fm[0] not in seen:
                    seen.add(fm[0])
                    other.append(fm)
    return own + other, len(other)


def chat(base_url: str, key: str, model: str, system: str, prompt: str, retries: int = 4) -> dict | str:
    body = json.dumps(
        {
            "model": model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            "tools": [TOOL],
            "tool_choice": "auto",
            "max_tokens": 1024,
        }
    ).encode()
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/chat/completions",
        data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
    )
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                time.sleep(2**attempt * 2)
                continue
            return f"error:http_{e.code}"
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            if attempt < retries - 1:
                time.sleep(2**attempt)
                continue
            return "error:network"
    return "error:retries"


def pick(reply: dict | str) -> str:
    if isinstance(reply, str):
        return reply
    try:
        msg = reply["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        return "error:bad_response"
    for call in msg.get("tool_calls") or []:
        fn = call.get("function", {})
        if fn.get("name") == "Skill":
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                return "error:bad_arguments"
            return str(args.get("skill", "")).strip().lstrip("/").split(":")[-1] or "error:empty_skill"
    return "none"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--model", action="append", default=[], help="repeatable: one run per model")
    ap.add_argument("--base-url", default=DEFAULT_BASE_URL)
    ap.add_argument("--api-key-env", default="LLM_API_KEY", help="name of the env var holding the key")
    ap.add_argument("--api-key-file", type=Path, help="file whose first line is the key (instead of the env var)")
    ap.add_argument("--list-models", action="store_true", help="print the model ids the endpoint offers and exit")
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--only", help="comma-separated case ids")
    ap.add_argument("--file", type=Path, default=CASES)
    ap.add_argument("--no-competitors", action="store_true", help="list only this repo's skills")
    ap.add_argument("--skills-dir", type=Path, action="append", default=[], help="more user-level skill dirs")
    args = ap.parse_args()

    if args.api_key_file:
        key = args.api_key_file.expanduser().read_text(encoding="utf-8").splitlines()[0].strip()
    else:
        key = os.environ.get(args.api_key_env, "")
    if not key:
        print(f"error: set {args.api_key_env} in the environment or pass --api-key-file", file=sys.stderr)
        return 2
    if args.list_models:
        req = urllib.request.Request(f"{args.base_url.rstrip('/')}/models", headers={"Authorization": f"Bearer {key}"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            for m in sorted(x.get("id", "") for x in json.load(resp).get("data", [])):
                print(m)
        return 0
    if not args.model:
        print("error: pass at least one --model (or --list-models)", file=sys.stderr)
        return 2
    cases = [json.loads(ln) for ln in args.file.read_text(encoding="utf-8").splitlines() if ln.strip()]
    if args.only:
        wanted = set(args.only.split(","))
        cases = [c for c in cases if c["id"] in wanted]
    skills, n_other = load_skills(not args.no_competitors, args.skills_dir)
    listing = "\n".join(f"- {n}: {d}" for n, d in skills)
    base_system = SYSTEM.format(skills=listing)
    agents_md = AGENTS_MD.read_text(encoding="utf-8").replace("__PROJECT_NAME__", "demo-agent")
    in_project_system = base_system + "\nThe project you are working in has this AGENTS.md:\n\n" + agents_md

    summary = {}
    all_rows = {}
    for model in args.model:
        jobs = [(c, i) for c in cases for i in range(args.runs)]
        with ThreadPoolExecutor(max_workers=args.jobs) as pool:
            got = list(
                pool.map(
                    lambda j: pick(
                        chat(
                            args.base_url,
                            key,
                            model,
                            in_project_system if j[0].get("in_project") else base_system,
                            j[0]["prompt"],
                        )
                    ),
                    jobs,
                )
            )
        rows, per_case, confusion = [], defaultdict(list), Counter()
        for (case, _), skill in zip(jobs, got):
            ok = skill in case["expect"]
            per_case[case["id"]].append(ok)
            rows.append({"id": case["id"], "expect": case["expect"], "got": skill, "ok": ok})
            if not ok:
                confusion[(case["expect"][0], skill)] += 1
        print(f"\n=== {model}")
        for case in cases:
            results = per_case[case["id"]]
            picks = [r["got"] for r in rows if r["id"] == case["id"]]
            mark = "PASS" if all(results) else ("FLAKY" if any(results) else "FAIL")
            if mark != "PASS":
                print(f"{mark:<6} {case['id']:<22} expect {'/'.join(case['expect']):<40} got {', '.join(picks)}")
        errors = sum(r["got"].startswith("error:") for r in rows)
        accuracy = sum(r["ok"] for r in rows) / max(len(rows), 1)
        print(f"accuracy {accuracy:.0%} over {len(rows)} runs ({len(cases)} cases × {args.runs}), {errors} errors")
        for (want, got_skill), n in confusion.most_common(8):
            print(f"  confused {want} → {got_skill} ×{n}")
        summary[model] = accuracy
        all_rows[model] = rows

    RESULTS.mkdir(exist_ok=True)
    out = RESULTS / f"triggers-api-{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.write_text(
        json.dumps(
            {
                "harness": "simulated: system prompt + Skill tool over chat-completions",
                "environment": {
                    "base_url": args.base_url,
                    "file": args.file.name,
                    "own_skills": len(skills) - n_other,
                    "competing_skills": n_other,
                },
                "accuracy": summary,
                "rows": all_rows,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    print(f"\nresults: {out.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
