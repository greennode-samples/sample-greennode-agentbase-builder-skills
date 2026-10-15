"""Static checks of this skill set (run by .github/workflows/skills-ci.yml; standard library only).

1. Every skills/<dir>/SKILL.md has YAML frontmatter with `name` == <dir> and a `description` of 1–1024 characters.
2. Internal links resolve: relative Markdown links `[..](path)` in *.md, and `references/<file>.md` mentions (in this
   skill, the orchestrator, or any skill here — mentions on a line naming a platform skill such as `/agentbase-policy`
   are skipped, those files live in the platform skill set).
3. The standard version is the same in the plugin manifests, scaffold.sh (STANDARD_VERSION) and CHANGELOG.md.

    python3 .github/scripts/check_skills.py        # from the repo root; exit 1 on any error
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SKILLS = ROOT / "skills"
MAX_DESCRIPTION = 1024
PLATFORM_SKILL_RE = re.compile(r"/agentbase(?:-(?!build)[a-z]+)?\b")
REF_RE = re.compile(r"references/[A-Za-z0-9_.-]+\.md")
LINK_RE = re.compile(r"\]\(([^)\s]+)\)")


def frontmatter(text: str) -> dict[str, str]:
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    if not m:
        return {}
    out: dict[str, str] = {}
    for line in m.group(1).splitlines():
        k, sep, v = line.partition(":")
        if sep:
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            out[k.strip()] = v
    return out


def check_frontmatter(errors: list[str]) -> None:
    for skill_md in sorted(SKILLS.glob("*/SKILL.md")):
        fm = frontmatter(skill_md.read_text(encoding="utf-8"))
        rel = skill_md.relative_to(ROOT)
        if fm.get("name") != skill_md.parent.name:
            errors.append(f"{rel}: frontmatter name {fm.get('name')!r} != directory {skill_md.parent.name!r}")
        desc = fm.get("description", "")
        if not 1 <= len(desc) <= MAX_DESCRIPTION:
            errors.append(f"{rel}: description is {len(desc)} chars (must be 1–{MAX_DESCRIPTION})")
        else:
            print(f"ok  {rel}: description {len(desc)}/{MAX_DESCRIPTION}")


def check_links(errors: list[str]) -> None:
    all_refs = {p.name for p in SKILLS.glob("*/references/*.md")}
    docs = [ROOT / "README.md", ROOT / "CHANGELOG.md", *sorted(SKILLS.rglob("*.md"))]
    for doc in docs:
        if not doc.exists() or "/assets/" in doc.as_posix():
            continue  # assets are templates for generated projects, not docs of this repo
        rel = doc.relative_to(ROOT)
        skill_dir = next((p for p in doc.parents if p.parent == SKILLS), None)
        for n, line in enumerate(doc.read_text(encoding="utf-8").splitlines(), start=1):
            for target in LINK_RE.findall(line):
                if re.match(r"^(https?:|mailto:|#)", target):
                    continue
                if not (doc.parent / target.split("#")[0]).exists():
                    errors.append(f"{rel}:{n}: broken link {target}")
            for ref in REF_RE.findall(line):
                name = ref.split("/", 1)[1]
                local = skill_dir is not None and (skill_dir / ref).exists()
                if local or (SKILLS / "agentbase-build" / ref).exists() or name in all_refs:
                    continue
                if PLATFORM_SKILL_RE.search(line):
                    continue  # a platform skill's reference
                errors.append(f"{rel}:{n}: {ref} not found in this skill set")


def check_versions(errors: list[str]) -> None:
    versions = {
        ".claude-plugin/plugin.json": json.loads((ROOT / ".claude-plugin/plugin.json").read_text())["version"],
        ".claude-plugin/marketplace.json": json.loads((ROOT / ".claude-plugin/marketplace.json").read_text())[
            "plugins"
        ][0]["version"],
        ".codex-plugin/plugin.json": json.loads((ROOT / ".codex-plugin/plugin.json").read_text())["version"],
    }
    scaffold = (SKILLS / "agentbase-build-scaffold/scripts/scaffold.sh").read_text()
    m = re.search(r'^STANDARD_VERSION="([^"]+)"', scaffold, re.M)
    versions["scaffold.sh STANDARD_VERSION"] = m.group(1) if m else "?"
    m = re.search(r"^## (\d+\.\d+\.\d+)", (ROOT / "CHANGELOG.md").read_text(encoding="utf-8"), re.M)
    versions["CHANGELOG.md (latest)"] = m.group(1) if m else "?"
    if len(set(versions.values())) != 1:
        errors.append("version mismatch: " + ", ".join(f"{k}={v}" for k, v in versions.items()))
    else:
        print(f"ok  version {next(iter(versions.values()))} everywhere")


def main() -> int:
    errors: list[str] = []
    check_frontmatter(errors)
    check_links(errors)
    check_versions(errors)
    for e in errors:
        print(f"ERROR {e}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
