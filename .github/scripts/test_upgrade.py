"""Offline end-to-end test of skills/agentbase-build-scaffold/scripts/upgrade.py (run by skills-ci.yml).

Scenarios — every project is a git repo, as `upgrade.py --apply` requires:
  history   scaffolded at --from (default 3528ac6: before 0.3.0, nothing recorded ⇒ explicit --from) plus a frontend
            overlay, then developer work: a non-conflicting and a conflicting edit to managed files, an edited seed, a
            deleted optional module, own files, secrets. Upgraded to this checkout; every outcome is checked, then
            (--pytest) the conflict is resolved and the backend's pytest + ruff run. A second run must find nothing.
  version   scaffolded from a `git archive` of --version-ref (default ae3e701, 0.3.0: only the version is recorded):
            the base is found among the commits stamped with that version; the result equals a fresh scaffold.
  manifest  scaffolded by this checkout (has .agentbase-template.json), upgraded to a modified copy of these skills:
            with history (3-way merge) and with --no-history (hashes only ⇒ <file>.agentbase-new).

    python3 .github/scripts/test_upgrade.py [--from REF] [--version-ref REF] [--pytest] [--keep DIR]

Needs the skills' git history (actions/checkout with fetch-depth: 0). Standard library only.
"""

from __future__ import annotations

import argparse
import difflib
import io
import json
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "skills/agentbase-build-scaffold/scripts"
UPGRADE = SCRIPTS / "upgrade.py"
NAME = "demo-agent"
GIT_ID = ["-c", "user.name=upgrade-test", "-c", "user.email=upgrade-test@example.com", "-c", "commit.gpgsign=false"]
DEV_LINE = "# upgrade-test: developer comment"
SECRET = b"UPGRADE_TEST_SECRET=keep-me\n"
STATEMENTS = ("def ", "async def ", "class ", "return ", "if ", "for ", "while ", "with ", "try:", "raise ")
OPEN_ENDINGS = ("(", "[", "{", ",", "\\", "=", "+", "-", " and", " or")

sys.path.insert(0, str(SCRIPTS))
import template_files as tf  # noqa: E402

failures: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(f"  {'ok ' if cond else 'FAIL'} {msg}")
    if not cond:
        failures.append(msg)


def run(cmd: list[str], cwd: Path | None = None, check_rc: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if check_rc and proc.returncode != 0:
        raise SystemExit(f"{' '.join(cmd)} failed ({proc.returncode}):\n{proc.stdout}\n{proc.stderr}")
    return proc


def export(ref: str, dest: Path) -> Path:
    data = subprocess.run(
        ["git", "-C", str(ROOT), "archive", "--format=tar", ref, "skills"], capture_output=True, check=True
    ).stdout
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        tar.extractall(dest)  # noqa: S202 — this repo's own history
    return dest / "skills"


def scaffold(skills: Path, dest: Path) -> None:
    run(["bash", str(skills / "agentbase-build-scaffold/scripts/scaffold.sh"), NAME, str(dest), "--no-sync"])


def files_of(project: Path) -> dict[str, bytes]:
    return {
        p.relative_to(project).as_posix(): p.read_bytes()
        for p in sorted(project.rglob("*"))
        if p.is_file() and ".git" not in p.relative_to(project).parts
    }


def commit(project: Path, msg: str) -> None:
    if not (project / ".git").exists():
        run(["git", "init", "-q"], project)
    run(["git", "add", "-A"], project)
    run(["git", *GIT_ID, "commit", "-qm", msg, "--allow-empty"], project)


def upgrade(project: Path, *args: str) -> tuple[int, dict]:
    proc = run([sys.executable, str(UPGRADE), str(project), "--json", *args], check_rc=False)
    if proc.returncode == 2 or not proc.stdout.strip():
        raise SystemExit(f"upgrade.py {' '.join(args)} failed ({proc.returncode}):\n{proc.stdout}\n{proc.stderr}")
    return proc.returncode, json.loads(proc.stdout)


def actions(plan: dict) -> dict[str, str]:
    return {it["path"]: it["action"] for it in plan["items"]}


def is_comment_at(text: str, line_no: int) -> bool:
    """True when line `line_no` (1-based) of a Python source is a comment token (not inside a string)."""
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type == tokenize.COMMENT and tok.start[0] == line_no:
                return True
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return False
    return False


def insert_dev_comment(base: str, head: str) -> str | None:
    """Insert DEV_LINE inside a long block the template did not change (⇒ a clean 3-way merge)."""
    a, b = base.splitlines(keepends=True), head.splitlines(keepends=True)
    for tag, i1, i2, _, _ in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag != "equal" or i2 - i1 < 12:
            continue
        for at in range(i1 + 5, i2 - 5):
            line, prev = a[at], a[at - 1].strip()
            # a comment above a statement (not in an import block, an expression or under a decorator) keeps ruff happy
            if not line.lstrip().startswith(STATEMENTS) or prev.startswith("@") or prev.endswith(OPEN_ENDINGS):
                continue
            indent = line[: len(line) - len(line.lstrip())]
            text = "".join(a[:at] + [f"{indent}{DEV_LINE}\n"] + a[at:])
            if is_comment_at(text, at + 1):
                return text
    return None


def conflicting_edit(base: str, head: str) -> str | None:
    """Change a line the template also changed (⇒ a conflict)."""
    a, b = base.splitlines(keepends=True), head.splitlines(keepends=True)
    for tag, i1, _, _, _ in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag == "replace" and a[i1].strip():
            a[i1] = a[i1].rstrip("\n") + "  # upgrade-test: conflicting edit\n"
            return "".join(a)
    return None


def take_template_side(text: str) -> str:
    """Resolve diff3 conflict blocks by keeping the new template's side."""
    out, part = [], None
    for line in text.splitlines(keepends=True):
        if line.startswith("<<<<<<< "):
            part = "yours"
        elif line.startswith("||||||| ") and part:
            part = "base"
        elif line.startswith("=======") and part:
            part = "theirs"
        elif line.startswith(">>>>>>> ") and part:
            part = None
        elif part in (None, "theirs"):
            out.append(line)
    return "".join(out)


# --------------------------------------------------------------------------------------------------- scenarios


def policy_checks() -> None:
    print("ownership classes")
    for rel in (
        "src/backend/.env",
        "src/backend/.env.prod",
        ".greennode.json",
        "src/backend/.greennode.json",
        "src/backend/uv.lock",
        "deploy/agent.yaml",
        "src/frontend/package.json",
        "src/frontend/app.json",
        "src/frontend/assets/icon.png",
        "src/frontend/node_modules/x/index.js",
        "src/backend/.venv/bin/python",
    ):
        check(tf.never_touch(rel), f"never touched: {rel}")
    for rel in ("src/backend/.env.example", "src/backend/.env.prod.example", "src/frontend/App.tsx", "Makefile"):
        check(not tf.never_touch(rel) and tf.owner(rel) == "managed", f"managed: {rel}")
    for rel in (
        "README.md",
        "src/backend/app/prompts/system.md",
        "src/backend/evals/datasets/smoke.jsonl",
        "src/backend/mcp_servers.json",
        "src/backend/a2a_agents.json",
        "src/backend/app/knowledge/faq.md",
    ):
        check(tf.owner(rel) == "seed", f"seed: {rel}")
    check(tf.owner("src/backend/app/graph/builder.py", ["src/backend/app/graph/*"]) == "seed", "developer_owned glob")


def scenario_history(work: Path, ref: str, head: dict[str, bytes], pytest: bool) -> None:
    print(f"history: scaffolded at {ref}, developer edits, upgraded to this checkout")
    old_skills = export(ref, work / "old-skills")
    project = work / "history"
    scaffold(old_skills, project)
    for rel, data in tf.frontend_template(old_skills, NAME).items():  # what setup_frontend.sh overlays
        (project / rel).parent.mkdir(parents=True, exist_ok=True)
        (project / rel).write_bytes(data)
    for rel in ("src/frontend/package.json", "src/frontend/app.json"):  # create-expo output (never touched)
        (project / rel).write_text('{"name": "frontend", "upgrade-test": true}\n')
    base = files_of(project)
    head = dict(head, **tf.frontend_template(SCRIPTS.parents[1], NAME))
    commit(project, "scaffold")

    py = [
        r
        for r in sorted(base)
        if r.startswith("src/backend/app/") and r.endswith(".py") and r in head and base[r] != head[r]
    ]
    merged = conflicted = None
    for rel in py:
        if merged is None and (text := insert_dev_comment(base[rel].decode(), head[rel].decode())):
            merged = rel
            (project / rel).write_text(text)
        elif conflicted is None and (text := conflicting_edit(base[rel].decode(), head[rel].decode())):
            conflicted = rel
            (project / rel).write_text(text)
    if not (merged and conflicted):
        raise SystemExit(f"{ref}: no managed file suits the merge/conflict edits (template unchanged since?)")
    seed = "src/backend/app/prompts/system.md"
    edited_seed = base[seed] + b"\n## Domain rules (upgrade-test)\n- Answer about leave policies only.\n"
    (project / seed).write_bytes(edited_seed)
    for rel in ("src/backend/app/identity.py", "src/backend/tests/test_identity.py"):  # allowed to remove
        (project / rel).unlink()
    own = "src/backend/app/tools/upgrade_test_tool.py"
    (project / own).write_text('"""A developer module the template does not know."""\n')
    state = json.loads((project / tf.STATE).read_text())
    state["memory_id"] = "mem-upgrade-test"
    (project / tf.STATE).write_text(json.dumps(state, indent=2) + "\n")
    commit(project, "developer work")
    secrets = ["src/backend/.env", "src/backend/.env.dev", "src/backend/.greennode.json", "src/frontend/.env"]
    for rel in secrets:  # git-ignored: never read, written or listed
        (project / rel).write_bytes(SECRET)
    print(f"  merge edit: {merged}\n  conflicting edit: {conflicted}")

    code, plan = upgrade(project, "--from", ref)
    act = actions(plan)
    check(code == 1, f"dry run predicts the conflict (exit {code})")
    check(act.get(merged) == "merge", f"{merged}: merge ({act.get(merged)})")
    check(act.get(conflicted) == "conflict", f"{conflicted}: conflict ({act.get(conflicted)})")
    want_seed = "skipped-owned" if base[seed] != head[seed] else None
    check(act.get(seed) == want_seed, f"{seed}: {want_seed} ({act.get(seed)})")
    check(act.get("src/backend/app/identity.py") in ("deleted-locally", None), "identity.py: deleted-locally")
    untouched = [
        r
        for r in head
        if r in base
        and base[r] != head[r]
        and (project / r).is_file()
        and (project / r).read_bytes() == base[r]
        and r != tf.STATE
    ]
    check(all(act.get(r) == "replace" for r in untouched), f"{len(untouched)} files you never changed: replace")
    new = [r for r in head if r not in base and not tf.never_touch(r) and r not in (tf.STATE, tf.MANIFEST)]
    check(all(act.get(r) == "add" for r in new), f"{len(new)} new template files: add ({', '.join(new)})")
    check(act.get(tf.STATE) == "state", "state file: versions + new keys")
    check(not any(tf.never_touch(p) for p in act), "no never-touch path in the plan")
    check(own not in act and "src/frontend/package.json" not in act, "developer files are not in the plan")
    print(f"  plan: {plan['counts']}")

    (project / "untracked.txt").write_text("x\n")
    proc = run([sys.executable, str(UPGRADE), str(project), "--from", ref, "--apply"], check_rc=False)
    check(proc.returncode == 2 and "uncommitted" in proc.stderr, "--apply refused on a dirty worktree")
    (project / "untracked.txt").unlink()

    code, plan = upgrade(project, "--from", ref, "--apply")
    check(code == 1, f"--apply exits 1 while a conflict remains (exit {code})")
    for rel in untouched + new:
        if (project / rel).read_bytes() != head[rel]:
            check(False, f"{rel} equals the new template")
            break
    else:
        check(True, f"{len(untouched)} replaced + {len(new)} added files equal the new template")
    lines = (project / merged).read_text().splitlines(keepends=True)
    check(
        any(DEV_LINE in ln for ln in lines)
        and "".join(ln for ln in lines if DEV_LINE not in ln) == head[merged].decode(),
        f"{merged}: your line + every template change",
    )
    text = (project / conflicted).read_text()
    check("<<<<<<< yours" in text and ">>>>>>> template" in text, f"{conflicted}: conflict markers")
    check(take_template_side(text) == head[conflicted].decode(), f"{conflicted}: outside the conflicts = template")
    check((project / seed).read_bytes() == edited_seed, f"{seed}: your version kept")
    sidecar = project / f"{seed}{tf.SIDECAR}"
    check(
        sidecar.is_file() == (want_seed is not None) and (not sidecar.is_file() or sidecar.read_bytes() == head[seed]),
        f"{seed}{tf.SIDECAR}: the new template, for review",
    )
    check(not (project / "src/backend/app/identity.py").exists(), "identity.py stays deleted")
    check(all((project / r).read_bytes() == SECRET for r in secrets), "secrets untouched")
    check((project / "src/frontend/package.json").read_text().find("upgrade-test") > 0, "create-expo files untouched")
    new_state = json.loads((project / tf.STATE).read_text())
    head_state = json.loads(head[tf.STATE])
    check(
        new_state["standard_version"] == head_state["standard_version"]
        and new_state["scaffold_version"] == head_state["scaffold_version"],
        "state: versions of the new template",
    )
    check(
        new_state["memory_id"] == "mem-upgrade-test" and "environments" in new_state, "state: values kept, keys added"
    )
    manifest = json.loads((project / tf.MANIFEST).read_text())
    check(
        all(manifest["files"][r]["sha256"] == tf.sha256(head[r]) for r in (merged, conflicted, seed)),
        "manifest: hashes of the new template",
    )
    check(
        "src/backend/app/identity.py" in manifest["files"] and own not in manifest["files"],
        "manifest: lists the template files only",
    )

    (project / conflicted).write_text(take_template_side(text))
    sidecar.unlink(missing_ok=True)
    commit(project, "upgrade")
    code, plan = upgrade(project)
    check(code == 0 and not plan["items"], f"second run: nothing to do ({plan['counts']}, {plan['from_how']})")
    if pytest:
        backend = project / "src/backend"
        for cmd in (
            ["uv", "lock"],
            ["uv", "sync"],
            ["uv", "run", "pytest", "-q", "-p", "no:cacheprovider"],
            ["uv", "run", "ruff", "check", "."],
            ["uv", "run", "ruff", "format", "--check", "."],
        ):
            proc = run(cmd, backend, check_rc=False)
            tail = (proc.stdout.strip().splitlines() or [""])[-1]
            check(proc.returncode == 0, f"upgraded backend: {' '.join(cmd)}  {tail}")
            if proc.returncode:
                print(proc.stdout[-3000:], proc.stderr[-3000:])


def scenario_version(work: Path, ref: str, head: dict[str, bytes]) -> None:
    print(f"version: scaffolded from an archive of {ref} (version recorded, no SHA)")
    project = work / "version"
    scaffold(export(ref, work / "version-skills"), project)
    commit(project, "scaffold")
    code, plan = upgrade(project)
    counts = plan["counts"]
    check(code == 0 and "commits" in plan["from_how"], f"base found by version: {plan['from_how'][:120]}")
    check(set(counts) <= {"replace", "add", "state", "current", "unchanged"}, f"only replace/add/state: {counts}")
    code, plan = upgrade(project, "--apply")
    same = [r for r in head if r != tf.STATE and (project / r).read_bytes() == head[r]]
    check(code == 0 and len(same) == len(head) - 1, f"upgraded = fresh scaffold ({len(same)}/{len(head) - 1} files)")


def scenario_manifest(work: Path) -> None:
    print("manifest: scaffolded by this checkout, upgraded to a modified copy of the skills")
    project = work / "manifest"
    scaffold(SCRIPTS.parents[1], project)
    check((project / tf.MANIFEST).is_file(), "scaffold.sh wrote .agentbase-template.json")
    commit(project, "scaffold")
    future = work / "future" / "skills"
    shutil.copytree(SCRIPTS.parents[1], future)
    backend = future / "agentbase-build-scaffold/assets/backend"
    with (future / "agentbase-build-scaffold/assets/root/Makefile").open("a") as f:
        f.write("\n# upgrade-test: new template line\n")
    config = backend / "app/config.py"
    config.write_text("# upgrade-test: template header\n" + config.read_text())
    (backend / "app/upgrade_test_new.py").write_text('"""New in the template."""\n')
    with (backend / "app/prompts/system.md").open("a") as f:
        f.write("\n- upgrade-test: new template rule\n")
    (future / "agentbase-build-eval/assets/backend/evals/datasets/ltm.jsonl").unlink()
    with (project / "src/backend/app/config.py").open("a") as f:
        f.write(f"\n{DEV_LINE}\n")
    commit(project, "developer edit")
    want = {
        "Makefile": "replace",
        "src/backend/app/config.py": "merge",
        "src/backend/app/upgrade_test_new.py": "add",
        "src/backend/app/prompts/system.md": "replace",
        "src/backend/evals/datasets/ltm.jsonl": "removed-upstream",
    }
    code, plan = upgrade(project, "--to", str(future))
    got = {r: actions(plan).get(r) for r in want}
    check(code == 0 and got == want, f"with history: {got}")
    code, plan = upgrade(project, "--to", str(future), "--no-history")
    got = {r: actions(plan).get(r) for r in want}
    check(code == 1 and got == dict(want, **{"src/backend/app/config.py": "manual"}), f"hashes only: {got}")
    code, plan = upgrade(project, "--to", str(future), "--apply", "--delete-removed")
    text = (project / "src/backend/app/config.py").read_text()
    check(
        code == 0 and text.startswith("# upgrade-test: template header") and DEV_LINE in text,
        "applied: both changes in config.py",
    )
    check(not (project / "src/backend/evals/datasets/ltm.jsonl").exists(), "--delete-removed: unchanged file deleted")
    manifest = json.loads((project / tf.MANIFEST).read_text())
    rendered = config.read_bytes().replace(b"__PROJECT_NAME__", NAME.encode())
    check(
        tf.sha256(rendered) == manifest["files"]["src/backend/app/config.py"]["sha256"],
        "manifest: hash of the new template",
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--from", dest="from_", default="3528ac6")
    ap.add_argument("--version-ref", default="ae3e701")
    ap.add_argument("--pytest", action="store_true", help="resolve, then run pytest + ruff in the upgraded backend")
    ap.add_argument("--keep", metavar="DIR", help="work in DIR (kept) instead of a temporary directory")
    args = ap.parse_args()
    if shutil.which("git") is None:
        raise SystemExit("git is required")
    tmp = None if args.keep else tempfile.TemporaryDirectory(prefix="upgrade-test-")
    work = Path(args.keep or tmp.name).resolve()
    if args.keep:
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True)
    head_dir = work / "head"
    scaffold(SCRIPTS.parents[1], head_dir)
    head = {r: d for r, d in files_of(head_dir).items() if not tf.never_touch(r) and r != tf.MANIFEST}
    policy_checks()
    scenario_history(work, args.from_, head, args.pytest)
    scenario_version(work, args.version_ref, head)
    scenario_manifest(work)
    if tmp:
        tmp.cleanup()
    print(f"\n{'FAILED: ' + str(len(failures)) if failures else 'all upgrade checks passed'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
