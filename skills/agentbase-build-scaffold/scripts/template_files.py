"""Who owns each file of a generated project, and the project's template manifest (.agentbase-template.json).

Standard library only, Python >= 3.9 (macOS system python3): scaffold.sh and upgrade.py run it before any venv exists.

Every path of a generated project belongs to one class (checked in this order):

  never    Never read, written or listed by upgrade.py. Secrets and generated state: .env and the filled
           .env.<env> copies, .greennode.json, *.credentials.json, deploy/agent.yaml + deploy/runtime.yaml (rendered
           with secrets), uv.lock (regenerate it with `uv lock`), .venv/, node_modules/, .git/, and the create-expo
           part of src/frontend (package.json, app.json, tsconfig.json, index.ts, assets/…: not reproducible offline).
  state    .agentbase-state.json: upgrade.py sets standard_version / scaffold_version and adds the keys a newer
           template introduces; it never changes a value you recorded (resource IDs, environments).
  seed     Created once from the template, then the developer's: the root README, the system prompt, knowledge docs,
           eval datasets, mcp_servers.json, a2a_agents.json (see SEEDS for why). upgrade.py replaces a seed only while
           it is still byte-identical to the template that wrote it; otherwise it keeps it and writes the new template
           next to it as <file>.agentbase-new for review.
  managed  Everything else the template writes (code, tests, Dockerfile, pyproject.toml, Makefile, CI workflow, deploy
           scripts, .env*.example): upgraded by a 3-way merge — the template as it was generated × your file × the new
           template.

.agentbase-template.json (committed with the project) lists every file the scaffold or the last upgrade wrote from the
template: its class and the SHA-256 of the content the TEMPLATE wrote — not your current content. The next upgrade
compares against it to tell your edits from the template's, even without the skills' git history. Globs listed under
"developer_owned" turn managed files into seeds (e.g. a graph you rewrote and never want merged).

    python3 template_files.py record <project-dir> <name> <standard-version> <scaffold-version> <frontend 0|1> <file>...
        (called by scaffold.sh with the files it created; files it kept because they already existed are not listed)
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import sys
from pathlib import Path

STATE = ".agentbase-state.json"
MANIFEST = ".agentbase-template.json"
SIDECAR = ".agentbase-new"  # suffix of the new template written next to a file upgrade.py did not merge
FRONTEND = "src/frontend"
USAGE = "Usage: template_files.py record <project-dir> <name> <standard-version> <scaffold-version> <0|1> <file>..."

# Created once, then the developer's. Why each one is not merged:
SEEDS = {
    "README.md": "the project's own README; teams rewrite it",
    "src/backend/app/prompts/system.md": "the domain prompt; conflict markers would reach the LLM. Template changes"
    " to its standard sections (Dates & time, Memory) are reported for review",
    "src/backend/app/knowledge/*": "your documents for search_knowledge",
    "src/backend/evals/datasets/*": "your eval items; template sample rows would change your pass rate",
    "src/backend/mcp_servers.json": "your connectors and URLs; a text merge can leave invalid JSON",
    "src/backend/a2a_agents.json": "your A2A peers; a text merge can leave invalid JSON",
}

# create-expo output in src/frontend (only the template overlay of agentbase-build-frontend is managed)
FRONTEND_GENERATED = (
    "package.json",
    "package-lock.json",
    "app.json",
    "tsconfig.json",
    "index.ts",
    "index.js",
    "babel.config.js",
    "metro.config.js",
    ".gitignore",
    "AGENTS.md",
    "CLAUDE.md",
)
NEVER_DIRS = {".git", ".venv", "node_modules", "__pycache__", ".expo", ".agentbase", ".pytest_cache", ".ruff_cache"}
NEVER_FILES = {".greennode.json", "uv.lock"}
NEVER_PATHS = {"deploy/agent.yaml", "deploy/runtime.yaml"}

# Human-readable form of never_touch(), copied into the manifest
NEVER_TOUCH_DOC = [
    ".env, .env.<env> (every .env* except *.example)",
    ".greennode.json, *.credentials.json",
    "uv.lock (regenerate: cd src/backend && uv lock)",
    "deploy/agent.yaml, deploy/runtime.yaml (rendered, contain secrets)",
    ".git/, .venv/, node_modules/, caches",
    "src/frontend files created by create-expo (package.json, app.json, tsconfig.json, index.ts, assets/, …)",
]


def never_touch(rel: str) -> bool:
    """True for paths upgrade.py must never read or write (secrets, generated files, create-expo output)."""
    parts = rel.split("/")
    name = parts[-1]
    if NEVER_DIRS.intersection(parts[:-1]) or name in NEVER_FILES or rel in NEVER_PATHS:
        return True
    if name == ".env" or (name.startswith(".env.") and not name.endswith(".example")):
        return True
    if name.endswith(".credentials.json"):
        return True
    if rel.startswith(FRONTEND + "/"):
        sub = rel[len(FRONTEND) + 1 :]
        return sub in FRONTEND_GENERATED or sub.startswith("assets/") or sub.startswith(".expo/")
    return False


def owner(rel: str, developer_owned: list[str] | tuple[str, ...] = ()) -> str:
    """Class of a template path that is not never_touch(): state, seed or managed."""
    if rel == STATE:
        return "state"
    if any(fnmatch.fnmatchcase(rel, pattern) for pattern in list(SEEDS) + list(developer_owned)):
        return "seed"
    return "managed"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def frontend_template(skills_dir: Path, name: str) -> dict[str, bytes]:
    """The files agentbase-build-frontend overlays on the create-expo app, as setup_frontend.sh writes them."""
    tpl = skills_dir / "agentbase-build-frontend" / "assets" / "frontend"
    out: dict[str, bytes] = {}
    if tpl.is_dir():
        for path in sorted(tpl.rglob("*")):
            if path.is_file():
                rel = f"{FRONTEND}/{path.relative_to(tpl).as_posix()}"
                if not never_touch(rel):
                    out[rel] = path.read_bytes().replace(b"__PROJECT_NAME__", name.encode())
    return out


def load(project: Path) -> dict | None:
    """The project's manifest, or None (older projects have none until their first upgrade)."""
    path = project / MANIFEST
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"{path}: unreadable ({exc}) — fix it or restore it from git") from None
    if not isinstance(data, dict) or not isinstance(data.get("files"), dict):
        raise ValueError(f"{path}: no 'files' object — fix it or restore it from git")
    return data


def write(
    project: Path,
    *,
    standard_version: str | None,
    scaffold_version: str | None,
    project_name: str,
    frontend: bool,
    files: dict[str, dict],
    developer_owned: list[str],
) -> None:
    data = {
        "about": "Written by agentbase-build scaffold.sh / upgrade.py — commit it, do not edit 'files'. Per file the"
        " template created: owner (managed = 3-way merged on upgrade; seed = yours once created, upgrades only"
        " report template changes; state = versions and new keys only) and the SHA-256 of what the TEMPLATE wrote."
        " Add globs to developer_owned to stop upgrades merging a managed file. Never touched: see never_touched.",
        "standard_version": standard_version,
        "scaffold_version": scaffold_version,
        "project_name": project_name,
        "frontend": frontend,
        "developer_owned": developer_owned,
        "never_touched": NEVER_TOUCH_DOC,
        "files": {rel: files[rel] for rel in sorted(files)},
    }
    (project / MANIFEST).write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def record(
    project: Path, name: str, standard_version: str, scaffold_version: str, frontend: bool, created: list[str]
) -> int:
    """Manifest of a freshly scaffolded project: the files scaffold.sh created (+ the frontend overlay)."""
    files: dict[str, dict] = {}
    for f in created:
        rel = Path(os.path.relpath(f, project)).as_posix()
        path = project / rel
        if rel.startswith("../") or rel == MANIFEST or never_touch(rel) or not path.is_file():
            continue
        entry = {"owner": owner(rel)}
        if rel != STATE:
            entry["sha256"] = sha256(path.read_bytes())
        files[rel] = entry
    if frontend:  # setup_frontend.sh keeps existing files: list only those that are the template's
        for rel, data in frontend_template(Path(__file__).resolve().parents[2], name).items():
            path = project / rel
            if path.is_file() and path.read_bytes() == data:
                files[rel] = {"owner": owner(rel), "sha256": sha256(data)}
    write(
        project,
        standard_version=standard_version,
        scaffold_version=scaffold_version,
        project_name=name,
        frontend=frontend,
        files=files,
        developer_owned=[],
    )
    return 0


def main(argv: list[str]) -> int:
    if len(argv) < 6 or argv[0] != "record" or argv[5] not in ("0", "1"):
        print(USAGE, file=sys.stderr)
        return 2
    _, project, name, standard_version, scaffold_version, frontend, *created = argv
    return record(Path(project), name, standard_version, scaffold_version, frontend == "1", created)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
