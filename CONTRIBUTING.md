# Contributing

The skills are instructions a coding agent follows; the templates under `skills/*/assets/` are the code it ships.
A change is good when an agent that follows the skill produces a project that passes its own checks.

## Before you open a pull request

```bash
uv run -q --python 3.12 --no-project python3 .github/scripts/check_skills.py   # format + drift checks
bash skills/agentbase-build-scaffold/scripts/scaffold.sh demo-agent /tmp/demo --with-web   # runs the backend suite
cd /tmp/demo && make lint && make security-scan && make web-test
```

CI (`.github/workflows/skills-ci.yml`) runs the same checks, the MCP server template suite and the upgrade of older
projects.

## Rules

- **Fix it in the owning skill.** Each module belongs to one skill (table in
  `skills/agentbase-build-scaffold/assets/root/AGENTS.md`). Code and the SKILL.md that describes it change together;
  the drift checks catch settings, trace names, make targets and paths that no longer match.
- **Every behaviour change has a test** in the template's own suite, and a security-relevant one also a
  `make security-scan` check when it can be detected statically.
- **Effectiveness over brevity.** Keep the knowledge a coding agent needs (pitfalls seen in practice, why a rule
  exists). Change a skill's `description` only with the routing eval: `python3 evals/run_triggers.py --runs 2`
  (`evals/README.md`) — a new trigger phrase can steal requests from another skill.
- **Never commit secrets**, real tokens or personal data, also not in tests or eval datasets.
- **Versioning:** user-visible changes go under a new version in `CHANGELOG.md` (with an *Upgrade* note when a
  project must do something), and the version is bumped together in the plugin manifests and
  `scaffold.sh` (`STANDARD_VERSION`); `check_skills.py` refuses a mismatch.

Security issues: see `SECURITY.md` — report privately, not in a pull request.
