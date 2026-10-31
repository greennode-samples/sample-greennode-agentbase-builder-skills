# Evals of this skill set

The repo CI (`check_skills.py`, scaffold + tests) proves the **templates** are right. These evals measure whether a
**coding agent using the skills** gets it right — the thing users actually depend on. Both run headless Claude Code
(`claude -p`) with this repo loaded as a plugin, on your own login and quota, so they are run by hand (before merging
a description / SKILL.md change, or a release), not on every push.

| Eval | Question | Cost | Command |
|---|---|---|---|
| Routing (`triggers.jsonl` + the held-out `triggers_heldout.jsonl`, `run_triggers.py`) | Does the agent load the right skill for a request — English and Vietnamese, build tasks, debugging in an existing project, and requests that belong to the platform `agentbase-*` skills or to no skill? | ~1 model turn per case | `python3 evals/run_triggers.py --runs 2` |
| Routing on other models (`run_triggers_api.py`, same cases + `triggers_heldout2.jsonl`) | The same question for GPT, DeepSeek, GLM, Gemma, Kimi, MiniMax, Qwen… — any model behind an OpenAI-compatible API | ~1 model call per case, simulated (no agent) | `python3 evals/run_triggers_api.py --api-key-file ~/.maas-key --model z-ai/glm-5.2 --runs 2` |
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

## The held-out set, and what a score depends on

`triggers.jsonl` is the set the skill descriptions were tuned on — its 100% was a training score. `triggers_heldout.jsonl`
(40 cases, EN + VI, written after the descriptions were frozen, not used to tune them) estimates what a user who words
things their own way gets:

```bash
python3 evals/run_triggers.py --file evals/triggers_heldout.jsonl --runs 2
```

Rules: tune descriptions and `AGENTS.md` on `triggers.jsonl`; run the held-out set to check; a description change made
because a held-out case failed makes that case a training case — say so, and write new held-out cases.

**A score belongs to an environment**: the model, the Claude Code version and the other skills installed. Every result
file now records them (`environment`), and every number below says which model produced it. The same 3 cases went from
9/9 on Opus 5.5 to 1/12 on Sonnet 5.5 with nothing else changed: on Sonnet the platform's `agentbase-wizard` (which
describes itself as mandatory for *any* build request) and Anthropic's `claude-api` (LLM-judge, prompts) won requests
that Opus routed to this set. Run `--model` explicitly when you compare two scores.

| Set | Model | Result |
|---|---|---|
| held-out, 40 cases × 2 | Sonnet 5.5 (what `claude -p` used on this machine on 2026-10-29) | **90%** (72/80) before fixing anything |
| — misses | | plain "build me an assistant" → `agentbase-wizard` (3 of 4 runs); "conversations get slower and slower" (in a project) and the Vietnamese "how accurate is my agent" → no skill (2 of 2 each); "update the web template" → `agentbase-build-scaffold` (1 of 2) |
| after each fix (those cases are now training cases) | Sonnet 5.5 | slowdown 3/3 (`AGENTS.md` row), Vietnamese eval 3/3 (skill phrases), web update 2/3; plain build requests 4/4 and 4/4 (description) |
| `triggers.jsonl`, 58 cases × 2, after the fixes | Sonnet 5.5 | **97%** — misses: `eval-en` → `claude-api` (a prompt-quality request in a plain directory with no AgentBase in it: arguably right), `mcp-github` → `agentbase-gateway` once |
| `build-en`, `build-vi`, `eval-en` × 3 | Opus 5.5 | 9/9 |

Fixes the held-out run led to: the orchestrator's description now says **"Use this skill, NOT /agentbase-wizard"** (Sonnet: the
plain build requests that mostly failed now route 4/4 and 4/4 on the held-out ones, 3/3 on three tuned ones; the
platform-question negative stays 3/3); an `AGENTS.md` row for "gets slower the
longer a conversation runs"; Vietnamese measuring phrases in the eval skill; the web-template update in the web skill.
Not fixable here: the wizard and `claude-api` are other people's skills — say "use agentbase-build" (README) when both are
installed.

## Other models: does it work beyond Claude?

`run_triggers_api.py` runs the same cases against any OpenAI-compatible endpoint (default: GreenNode MaaS). It does **not**
run an agent: it puts the skills' `name: description` list (this repo's plus the user-level skills you have installed,
like the platform's `agentbase-*` set and its wizard), the project's `AGENTS.md` for in-project cases and one `Skill`
tool into a chat call, and records the first skill the model picks. It measures what the wording can fix. It does not
measure how Codex, Gemini CLI, Cursor or OpenCode load skills — they list and push them differently — so confirm with the
real agent before claiming support for it. `--no-competitors` lists only this repo's skills; `--list-models` shows what a key
can call. Keys come from a file or an env var, never the command line.

`triggers_heldout2.jsonl` (32 cases, short questions and requests, EN + VI, written after the 0.5.0 descriptions were
rewritten) is the second held-out set. Two of its cases (`h2-web-q`, `h2-mcp-q`) led to description changes, so they are
now training cases.

| Model (how it was run) | `triggers.jsonl` (58 × 2) | `triggers_heldout2.jsonl` (32 × 2) | Only this repo's skills listed, own-skill cases (tuning / held-out2) |
|---|---|---|---|
| Claude Sonnet 5.5 (Claude Code) | 98% | 92% | — |
| Claude Haiku 5.5 (Claude Code) | 95% (was 79%) | **64%** | — |
| DeepSeek V4 Flash (simulated) | 98% | 100% | 99% / 100% |
| DeepSeek V4 Pro | 98% | 89% | 100% / 100% |
| GLM 5.2 | 100% | 100% | 100% / 100% |
| GLM 5.3 Flash | 97% | 98% | 98% / 100% |
| GPT-5.4 nano | 89% | 88% | 97% / 95% |
| Gemma 4 31B (the only Google model on MaaS; no Gemini) | 100% | 100% | 100% / 100% |
| Kimi K2.6 | 99% | 98% | 100% / 100% |
| MiniMax M3 | 98% | 97% | 97% / 97% |
| Qwen 3.8 Flash | 98% | 100% | 98% / 100% |

Dated 2026-10-30/31, 2 runs per case, with the 10 user-level platform skills installed (26 skills listed). The first run on
held-out2, before the web / mcp fixes, was 83–98% (Kimi and MiniMax were rate limited, HTTP 429, and scored low until rerun
with fewer parallel calls).

What this showed:

- **Descriptions that open with the user's words work across families.** Rewriting all 16 skill descriptions to start with
  what a person asks ("Use when the user wants … even for a short question, load it before answering") took Haiku 5.5 from 79%
  to 95% on the tuning set and left Sonnet where it was (97% → 98%). With only this repo's skills listed, the nine non-Claude
  models score 95–100%.
- **The platform wizard is the main conflict.** `agentbase-wizard` says it is mandatory for any build request; when it is
  installed, plain "build me a chatbot" requests go to it for GPT-5.4 nano, DeepSeek V4 and Kimi at times, whatever our
  description says. Not fixable here (it is another skill): in that case say "use agentbase-build" (README).
- **Small models skip skills for context-free questions.** On held-out2, Haiku answers "can the agent ask me before it wires
  money?" itself instead of loading a skill (64%). The same prompts with one added sentence, "I'm building an agent on GreenNode
  AgentBase.", score 86%. Inside a generated project `AGENTS.md` carries that context (in-project cases pass), so the
  weak spot is a bare question in an empty directory on a small model. Name AgentBase in the request, or work in a generated
  project.
- **Wording does not fix it (tried, reverted).** A third set, `triggers_heldout3.jsonl` (36 cases, half inside a generated
  project, half bare), scored 75% on Haiku 5.5 as written (2 runs per case). Two description changes were measured on it and
  gave nothing, so they were not kept: adding "Assume it is a GreenNode AgentBase agent even if the user does not say so" to
  15 descriptions (76%), and adding "a general or how-to question included" to the load clause (76%). Every miss is the same
  shape: a bare English question about "my agent" in an empty directory (memory, tracing, human approval, MCP, web chat page)
  that Haiku answers itself; the cases inside a project, and the Vietnamese ones that name a concrete tool or endpoint, pass.
  The remaining lever is the request or the project, not the description. Because that set drove those experiments it is
  now training data; a fresh set is needed before another description change is judged.
- **Where it still misses:** `h2-eval-q` and `h2-llm-q` ("how do I know my last prompt change didn't make answers worse?",
  "which model for the router?") → no skill, on Sonnet too; `agentbase-build-mcp` vs `agentbase-build-mcp-server` on GPT-5.4
  nano; `claude-api` takes prompt-quality requests on Claude.

## Baseline (2026-10-28, the default model of that day — not recorded, hence the `environment` field now)

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
| 2026-10-29 | `hr-leave-unprompted` | 14/14 checks | 5.4 min, 37 turns, ≈ $0.8, Sonnet 5.5 | The same request as `hr-leave` **without naming a skill**: the agent still produced a standard project (`.agentbase-template.json`, spec, HITL in dev / staging / prod, 4+ eval items) — on this run the wizard did not take it. One run, one model: routing is probabilistic (see above) |
| 2026-10-29 | `add-ticket-tool` | 13/13 checks | 3.7 min, 24 turns, ≈ $0.5, Sonnet 5.5 | An existing standard project and a one-line request, no skill named: it followed the add-a-tool recipe — the tool in `app/tools/custom.py` with `idempotency_key`, `create_ticket` in `HITL_TOOLS` of all four committed env files (the local `.env` is left to the user: S3 warns), tests, 2+ eval items incl. an injection one. The first four rows ran before results recorded the model |

## Adding cases and scenarios

- Routing: one JSON line per request — `id`, `prompt`, `expect` (list), optional `in_project`, `kind`. Add a case
  for every new skill (EN + VI) and for every confusion you see in real use.
- Scenario: an entry in `scenarios.json` (`prompt` = the business request; `checks` = what must be true). Keep checks
  objective; extend `grade()` in `run_scenarios.py` for a new kind of check.
- Safety: scenarios tell the agent not to touch the platform and deny network tools, but its platform skills are
  installed — run them where no GreenNode credentials are available to the shell (`run_scenarios.py` strips
  `GREENNODE_*` / `GRN_*` from the environment; a credentials file in your home directory is not hidden).
