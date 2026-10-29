"""setup_web.sh --update: template files follow a newer version of the skill, your edits survive (needs node, no network).

    python3 .github/scripts/test_web_update.py

An "existing app" (a package.json is enough) takes the code path that never calls create-next-app. The newer version of
the skill is a copy of this one with a template file changed, one added and one dropped.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / "skills/agentbase-build-web"


def run(skill: Path, web: Path, *flags: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(skill / "scripts/setup_web.sh"), "demo", str(web), *flags], capture_output=True, text=True
    )


def main() -> int:
    errors: list[str] = []

    def check(ok: bool, what: str) -> None:
        if not ok:
            errors.append(what)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        web = tmp_path / "web"
        web.mkdir()
        (web / "package.json").write_text('{"name": "demo", "scripts": {}}')
        first = run(SKILL, web, "--no-install")
        check(first.returncode == 0, f"setup failed: {first.stderr[-300:]}")
        manifest = json.loads((web / ".agentbase-web.json").read_text())["files"]
        check("src/server/session.ts" in manifest and "src/app/layout.tsx" in manifest, "manifest misses template files")
        check("__PROJECT_NAME__" not in (web / "src/app/layout.tsx").read_text(), "placeholder not replaced")

        # you edit one file; the template later changes that file, another one, adds one and drops one
        edited, untouched, dropped = "src/app/layout.tsx", "src/server/session.ts", "src/server/cookies.ts"
        (web / edited).write_text((web / edited).read_text() + "\n// my change\n")
        newer = tmp_path / "skill-next"
        shutil.copytree(SKILL, newer)
        tpl = newer / "assets/web"
        (tpl / edited).write_text((tpl / edited).read_text() + "\n// template change\n")
        (tpl / untouched).write_text((tpl / untouched).read_text() + "\n// security fix\n")
        (tpl / "src/server/added.ts").write_text("export const added = true;\n")
        (tpl / dropped).unlink()

        res = run(newer, web, "--update")
        out = res.stdout
        check(res.returncode == 1, f"a file to merge must exit 1, got {res.returncode}: {res.stderr[-200:]}")
        check((web / untouched).read_text().endswith("// security fix\n"), "an untouched file was not updated")
        check("// my change" in (web / edited).read_text() and "template change" not in (web / edited).read_text(), "your edit was overwritten")
        check("// template change" in (web / f"{edited}.agentbase-new").read_text(), "no .agentbase-new for the edited file")
        check((web / "src/server/added.ts").exists(), "a new template file was not added")
        check((web / dropped).exists() and dropped in out, "a dropped file must be kept and listed")
        for needle in (untouched, edited, "added.ts"):
            check(needle in out, f"report does not mention {needle}")

        # merged by hand: delete the .agentbase-new, run again — nothing left to do for the files that match
        (web / f"{edited}.agentbase-new").unlink()
        (web / edited).write_text((tpl / edited).read_text().replace("__PROJECT_NAME__", "demo"))
        again = run(newer, web, "--update")
        check(again.returncode == 0, f"second update should be clean, got {again.returncode}: {again.stdout[-300:]}")

        # no manifest (an app from before --update): every differing file counts as yours
        (web / ".agentbase-web.json").unlink()
        (web / untouched).write_text((web / untouched).read_text() + "// local\n")
        lost = run(newer, web, "--update")
        check(lost.returncode == 1 and (web / f"{untouched}.agentbase-new").exists(), "without a manifest nothing may be overwritten")
        check("agentbase-web.json is missing" in lost.stderr, "the missing manifest is not explained")

    for e in errors:
        print(f"FAIL  {e}")
    print("ok  setup_web.sh --update" if not errors else f"{len(errors)} failure(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
