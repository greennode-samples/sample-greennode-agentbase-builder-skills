# Evals of this skill set

The repo CI (`check_skills.py`, scaffold + tests) proves the **templates** are right. These evals measure whether a
**coding agent using the skills** gets it right — the thing users actually depend on. Both run headless Claude Code
(`claude -p`) with this repo loaded as a plugin, on your own login and quota, so they are run by hand (before merging
a description / SKILL.md change, or a release), not on every push.

| Eval | Question | Cost | Command |
|---|---|---|---|
| Routing (`triggers.jsonl`, `run_triggers.py`) | Does the agent load the right skill for a request — English and Vietnamese, build tasks, debugging in an existing project, and requests that belong to the platform `agentbase-*` skills or to no skill? | ~1 model turn per case | `python3 evals/run_triggers.py --runs 2` |
| End to end (`scenarios.json`, `run_scenarios.py`) | From a business request, does the agent produce a project that passes `make test`, `make lint`, `make security-scan` and the scenario's checks (tools, HITL coverage, MCP config, knowledge, eval items, tests)? | a full coding session per scenario | `python3 evals/run_scenarios.py --only hr-leave --keep` |

Grading is objective (skill names, exit codes, files), never an LLM's opinion. Results go to `evals/results/`
(git-ignored). `run_scenarios.py --grade <dir> --scenario <id>` grades any project, e.g. one a person built.

## Reading the routing eval

- `PASS` every run picked an expected skill · `FLAKY` some runs did · `FAIL` none did. `expect` lists every
  acceptable skill; `none` = answering without a skill is right.
- `"in_project": true` cases run inside a freshly scaffolded project (with its `AGENTS.md`): requests about an
  existing agent are judged where they happen.
- Fix a miss where the agent reads it: the skill's `description` (when to use / not use), or the project's
  `AGENTS.md` (module → skill and symptom → skill tables, `skills/agentbase-build-scaffold/assets/root/AGENTS.md`).
  Then rerun the whole file — a description change can steal cases from another skill.

## Baseline (2026-10-28, default model)

| Set | Before | After |
|---|---|---|
| 41 base cases | 93% (2 harness artefacts, 1 real miss) | 100% |
| 13 hard cases (ambiguous, debugging in a project) | 54% | 85% with `AGENTS.md` alone, 100% with `AGENTS.md` + troubleshooting clauses in descriptions |
| All 56 cases | — | 98% (the one miss was a wrong expectation, fixed), then 100% |
| 58 cases incl. multi-tenant, roles and web UI | — | 100% |

Main finding: debugging requests inside an existing project ("401 since we switched to Auth0", "user B sees user A's
history", "security scan failed") loaded **no skill** — the agent went straight to the code without the standard's
knowledge. Generated projects now ship `AGENTS.md` (+ `CLAUDE.md` importing it) that maps modules and symptoms to skills.

## End-to-end runs

| Date | Scenario | Result | Agent effort | What it taught |
|---|---|---|---|---|
| 2026-10-28 | `hr-leave` | 13/13 checks | 12.7 min, 83 turns, ≈ $4.6 | Good code (user from `config`, HITL in every env, health reason traced as its length only, 18 eval items incl. 2 injection). Two template gaps fixed: `test_security_scan.py` overwrote `HITL_TOOLS` (broke once a project had a write tool); the local `.env` kept `HITL_TOOLS=[]` so `make dev` ran writes unapproved — S3 now warns about `.env` / `.env.example` |
| 2026-10-29 | `crm-mcp` | 22/22 checks | 12.5 min, 88 turns, ≈ $4.7 | Config only, as asked: `crm` connector with `auth: iam` and the 4 `allow_tools`, writes in HITL of every env, reads not, the Policy Group statement left in the spec, evals run against a dev gateway only. Template gap: the scan test planted a server named `crm` and clashed with the real one (agent renamed it) — all planted names are now `probe…`. The local-`.env` S3 warning missed MCP write tools — now covered |
| 2026-10-29 | `faq-docs` | 9/9 checks | 18.5 min, 98 turns, ≈ $5.8 | Two IT documents, a `search_knowledge` tool, a filter for planted instructions plus a separate `injection.jsonl` (5 items, pass bar 1.0) against a tampered document kept out of the image, `must_not_contain` check, both datasets gating deploys. No template test needed editing |

## Adding cases and scenarios

- Routing: one JSON line per request — `id`, `prompt`, `expect` (list), optional `in_project`, `kind`. Add a case
  for every new skill (EN + VI) and for every confusion you see in real use.
- Scenario: an entry in `scenarios.json` (`prompt` = the business request; `checks` = what must be true). Keep checks
  objective; extend `grade()` in `run_scenarios.py` for a new kind of check.
- Safety: scenarios tell the agent not to touch the platform and deny network tools, but its platform skills are
  installed — run them where no GreenNode credentials are available to the shell (`run_scenarios.py` strips
  `GREENNODE_*` / `GRN_*` from the environment; a credentials file in your home directory is not hidden).
