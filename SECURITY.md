# Security policy

These skills generate code that runs in production: authentication, tenant isolation, approval of actions, secrets
handling. A weakness in a template is copied into every agent built from it, so please report it privately.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting: **Security** tab of this repository → **Report a vulnerability**.
Do not open a public issue or pull request for it. Include:

- the skill and file (e.g. `skills/agentbase-build-auth/assets/backend/app/auth/inbound.py`), and the version
  (`standard_version` in a generated project's `.agentbase-state.json`, or the plugin version);
- how to reproduce it — ideally a failing test in the generated project;
- the impact (whose data or which action is exposed, under which configuration).

Never include real secrets, tokens or personal data; use placeholders.

## What to expect

- Acknowledgement within 3 working days, a first assessment within 10.
- Fixes land in the owning skill with a regression test (and a `make security-scan` check when the mistake can be
  detected statically), and are released in a new version listed in `CHANGELOG.md`. Projects pick them up with
  `skills/agentbase-build-scaffold/scripts/upgrade.py`.
- We credit reporters in the changelog unless you prefer otherwise.

## Scope

In scope: the skills' instructions and everything under `skills/*/assets/` and `skills/*/scripts/` — the code they
generate. Out of scope: the GreenNode AgentBase platform itself, Langfuse, and third-party packages (report those
to their maintainers; tell us if a template uses them unsafely).

For auditing an agent you built, see `/agentbase-build-security-audit`.
