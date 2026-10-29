# Changelog

Versions of the agentbase-build standard. The version is stamped in `.claude-plugin/plugin.json`,
`.claude-plugin/marketplace.json`, `.codex-plugin/plugin.json` and `skills/agentbase-build-scaffold/scripts/scaffold.sh`
(`STANDARD_VERSION`, checked by `.github/workflows/skills-ci.yml`). Every generated project records it in
`.agentbase-state.json` (`standard_version`, plus `scaffold_version` = the skills' git SHA when available), so a
review can tell which standard a project was built from.

## 0.5.0 — 2026-10-29

Upgrade: run `skills/agentbase-build-scaffold/scripts/upgrade.py` on a 0.4.0 project. Your local `.env` is never
touched: the new settings `SESSION_MAX_TURNS`, `MAX_REQUEST_BYTES`, `MAX_CONCURRENT_REQUESTS`,
`RATE_LIMIT_USER_PER_MIN`, `RATE_LIMIT_TENANT_PER_MIN` use their defaults until you set them (compare with
`.env.example`); check `HITL_TOOLS` there too (`make security-scan` S3). Then
move eval items that need deployed services into `src/backend/evals/datasets/live/` — `make eval` and CI now gate
every dataset in `evals/datasets/`. Clients must adopt the `session_id` returned by the agent (session rotation).

Fixes from the production-readiness review below (instructions a coding agent followed into a failure)
- The eval gate ran `smoke.jsonl` only: `make eval` and CI now run every `evals/datasets/*.jsonl` (each gated, worst
  exit code; `ltm.jsonl` with LTM on). Items needing deployed services go in `evals/datasets/live/` (run by hand).
- Web BFF: the "absolute" session length was sliding (each token refresh resealed a full period, an 8 h session
  lived 24 h with hourly use). The session now carries `authTime` and is resealed for the time left.
- Docs: the checklist's `os.environ` grep flagged the untouched template as a BLOCKER; the decision guide mapped a
  BFF to `api_key` (the web BFF forwards the user's token — 401 when followed); the MCP example had no `local` env
  (`make dev` loaded no gateway tools); the erasure runbook hashed ids without the tenant and missed rotated sessions
  and MCP server data; `lf_trace.py` needs cwd `src/backend`; `HITL_TOOLS` belongs in every env file; scaffold Step 1
  offers `--with-web` and the mobile skill no longer claims web; stale counts (backend skills, MCP server tests,
  marketplace list, reflection latency).
- Repo: LICENSE (MIT, as the manifests declare), SECURITY.md (private vulnerability reporting), CONTRIBUTING.md.

Production hardening — the tier-1 findings of an independent production-readiness review
- **An approved action ran twice** (reproduced): a request ending while the approved tool ran left the call dangling;
  the bridge's "interrupted" placeholder made the model call it again and the user approved a 2nd card. The next
  request now closes such calls as `Outcome unknown:` (event `hitl.outcome_unknown`): the model must not repeat them,
  the card is gone. `hitl.idempotency_key(config, tool_call_id)` gives every approved call a key stable across
  replicas and retries — side-effect tools pass it downstream (HITL skill, checklist, AGENTS.md rule).
- **Admission control** (`app/limits.py`, per replica): POST bodies over `MAX_REQUEST_BYTES` (1 MiB) ⇒ 413 before
  parsing (a 50 MB body used to be parsed whole); more than `MAX_CONCURRENT_REQUESTS` (32) agent requests ⇒ 503 +
  `Retry-After`; chat/resume over `RATE_LIMIT_USER_PER_MIN` (30) per verified user / `RATE_LIMIT_TENANT_PER_MIN` per
  tenant ⇒ 429 + `retry_after_s`. The wait for a busy session is bounded by `REQUEST_TIMEOUT_S` (409), it used to be
  unbounded. `LLM_MAX_TOKENS` defaults to 8192 (was uncapped; 0 = no cap).
- **Memory cost no longer grows with the session**: the graph counts `turns`; an HTTP chat past `SESSION_MAX_TURNS`
  (50) continues in a new session seeded with the summary and messages (span `memory.session_rotated`), returned in
  `session_id`. The web and mobile reducers adopt it (shared `chatState.ts`, +1 test). The bridge reads every event of
  a session per turn: 6.9 MB at turn 40 in the review.
- **Release safety** (generated `ci.yml`): an image is built once per commit and promoted — a tag is never rebuilt or
  overwritten (prod used to rebuild on a newer base image and re-point the tag under running replicas); the digest
  is recorded in the job summary and checked after the deploy. A rollback (`image_tag`) no longer re-runs the eval, so
  it works while the LLM is down; staging/prod deploy only from `main` or a tag.
- `make security-scan` S10 covers the new limits (raised above the default, or turned off with 0). Tests: +10 backend
  (`test_limits.py`, HITL outcome-unknown + idempotency key), +3 scan, +1 web.

Effectiveness of the skills for coding agents (measured, `evals/`)
- `evals/run_triggers.py` + `triggers.jsonl` (58 cases, EN + VI, build tasks, debugging inside a scaffolded project,
  platform and no-skill requests): which skill a headless Claude Code loads. `evals/run_scenarios.py` +
  `scenarios.json`: a coding agent builds 3 agents from business requests offline; graded on make test / lint /
  security-scan, HITL coverage, MCP config, knowledge, eval items, tests.
- Finding: debugging requests in an existing project loaded no skill (hard cases 54%). Generated projects now ship
  `AGENTS.md` (+ `CLAUDE.md` importing it, both seeds): module → skill and symptom → skill tables, platform skills,
  rules. Descriptions of auth, mcp, llm, memory, tracing, deploy, hitl, security-audit name their troubleshooting
  cases. Routing: 100% of 58 cases (hard cases 100%).
- End to end: hr-leave 13/13, faq-docs 9/9, crm-mcp 22/22 (12–19 min, ≈ $5 each). Fixed from what the runs showed:
  `test_security_scan.py` planted names that real projects use (`crm`, `send_*`, overwritten `HITL_TOOLS`) and broke
  their `make test` — every planted tool / server / file is now `probe…` and patterns are appended; the local
  `.env` keeps `HITL_TOOLS=[]` from scaffold time, so `make dev` ran writes unapproved — S3 warns, for local tools
  and MCP `allow_tools`.

Product coverage
- New skill `agentbase-build-web`: Next.js web chat in `src/web` with a BFF (server-side OIDC code + PKCE, sealed
  httpOnly cookie, CSRF, IAM to the Runtime, nonce CSP), SSE, HITL cards, feedback; contract / SSE / chat state /
  linkify byte-identical with the Expo app (skills-ci `cmp`). `scaffold.sh --with-web`, `make web-setup / web-dev /
  web-test`; `src/web` is outside upgrade.py (refresh with `setup_web.sh --force`).
- Organizations and roles: `AUTH_TENANT_CLAIM` scopes every user id to its tenant (`u-<sha256([iss, tenant, sub])>`,
  so memory, HITL, A2A tasks, feedback and traces are per tenant), `AUTH_ALLOWED_TENANTS`, `AUTH_ROLES_CLAIM`,
  `TOOL_ROLES` (role-gated tools removed before the model sees them, fail closed); `tenant_id` / `roles` in
  `config["configurable"]`, `tenant:<id>` trace tag; `tests/test_tenancy.py` (13 tests).

Security audit
- New skill `agentbase-build-security-audit`: a CI-gating quick scan and a full source-first audit with Cloudflare's `security-audit` skill, seeded with
  `references/threat-model.md` (assets, trust boundaries T1–T10, seed coverage units U1–U12, facts outside the repo,
  companion selection). Findings are fixed in the owning skill with a regression test / eval item.
- `make security-scan` (`scripts/security_scan.py`, asset of the new skill, 18 tests, also run inside generated projects): 13 offline checks S1–S13
  (inbound auth, tool parameters, HITL coverage incl. MCP `allow_tools`, MCP entries, committed secrets, outbound
  credentials, injection defenses and evals, trace masking, bounds, CORS, supply chain, open audit findings), PASS /
  FAIL / WARN / MANUAL per check, key names only (never values), waivers in `.agentbase-state.json` →
  `security_audit.waivers`. Generated projects get it through `scaffold.sh`; the CI `test` job runs it, so a blocking
  FAIL stops every deploy. `.agentbase-state.json` template gains `security_audit`.
- `/agentbase-build`: Step 9 requires `make security-scan` to pass, security review routed from review mode;
  `references/checklist.md` → *Security* points to the scan instead of repeating its rules; `references/security.md`
  points to the new skill.

## 0.4.0 — 2026-10-16

Upgrade path for generated projects
- `skills/agentbase-build-scaffold/scripts/upgrade.py` brings a project to the current template: per file a 3-way merge
  of the template as generated for that project (rebuilt from the skills' git history), the project's file and the
  new template. Dry run by default; `--apply` only on a clean git worktree; conflicts stay as diff3 markers (exit 1).
  Secrets, `uv.lock` and create-expo files are never read.
- `scaffold.sh` writes `.agentbase-template.json`: per generated file its owner (managed / seed / state) and the
  template's hash. Seeds (README, system prompt, knowledge, eval datasets, `mcp_servers.json`, `a2a_agents.json`)
  stay yours; template changes to them arrive as `<file>.agentbase-new`.
- 0.3.x projects are matched by SHA or version; older ones need `--from <commit>` (`--guess-from` ranks candidates);
  plugin installs without `.git` use `--skills-repo`. skills-ci job `upgrade` runs the whole path.

Secrets never go through the chat
- `make set-secret` (generated projects and the MCP server template): hidden input in the user's own terminal,
  validated (env-file format, `Bearer ` prefixes, whole `KEY=value` lines, swapped Langfuse keys…), written to the
  git-ignored `.greennode.json` / `.env` / `.env.<env>` (mode 600, rest of the file kept), never printed. `LIST=1`
  shows set / not set (safe for a coding agent), `STDIN=1` takes one value from a password manager.
- Every credential step of the skills asks the user to run it (or `gh secret set NAME --env <env>` for CI) instead
  of pasting; `make check-creds` hints name the `make set-secret NAME=…` to run. `references/security.md`: runbook
  for rotating a secret that was pasted into a chat. The MCP server's API-key generator no longer prints the key.
- Fix: `make test` failed once `src/backend/.greennode.json` existed (SDK cache in `test_mcp.py`).

Latency of long turns
- Self-eval no longer blocks long or reasoning answers: `REFLECTION_MODE` (auto / gate / background) scores them
  after `done` in the same Langfuse trace — bounded (`REFLECTION_BACKGROUND_TIMEOUT_S`, `…_MAX_CONCURRENCY`),
  independent of the client connection, drained at shutdown. Auto = background on the reasoning tier, above
  `REFLECTION_GATE_MAX_CHARS` (1200) or without retries.
- `LLM_THINKING_BUDGET` per task or tier (`thinking_budget: N` / `enable_thinking: false`, measured on MaaS); router
  and judge run without thinking by default (router 6.9 → 1.5 s, judge 16.5 → 4.6 s); the router prompt treats
  calculations as complex.
- SSE `status` event (`thinking` / `tool` / `checking`, safe labels, no arguments) and a status line under the
  pending reply in the frontend template; clients that ignore unknown events keep working.

Docs ↔ code drift checks
- `check_skills.py` runs `.github/scripts/check_drift.py` (stdlib, < 1 s, Python ≥ 3.12): settings vs `.env.example`
  / `env.deploy.example` / `ci.yml`, emitted trace names vs the tracing tree (both ways, with types), `make` targets,
  cited paths, config variable names, Dockerfile / uv parity, no CI step copies in docs. Exceptions live in
  `drift_allowlist.py` with a reason; unused entries fail.
- Fixed what it found: 7 trace names missing from the tree, `trace-schema.md` metadata keys, wrong variable names in
  `compression.md` and the A2A client docstring, `MCP_JWT_ALGORITHMS` missing from the MCP server `.env.example`.
- MCP server: `MCP_JWT_ALGORITHMS` is asymmetric only (HS*/none refused at startup), like the agent.

## 0.3.1 — 2026-10-15

CI (generated `ci.yml` and this repo's `skills-ci.yml`)
- Runner pinned to `ubuntu-24.04`: `ubuntu-latest` moves to Ubuntu 26 from 2026-10-19 without a change in the repo.
- Actions moved off the deprecated Node.js 20 runtime and pinned to commit SHAs (immutable; organizations can enforce
  SHA pinning): `actions/checkout` v7.0.1, `astral-sh/setup-uv` v10.3.0 (no floating major tags since v8). No input
  changes were needed: the v8–v10 breaking changes (custom manifest format, cache pruning default, no cache on
  `pull_request_target` / `workflow_run` / `release`) don't apply to these workflows.

## 0.3.0 — 2026-10 (enterprise review)

Deploy, CI and packaging
- CI deploy keeps network/security settings: before `grn agentbase runtime update` (whose body has no
  `networkConfig` / `allowedCidrs` / `inboundAuth`), the job reads the latest version and refuses VPC runtimes, IP
  Access Control and Runtime Inbound Auth; after the update it fails on any drift and checks the new image is live.
  Runtime lookup paginates (`totalPage`); `uv sync --locked`; `image_tag` input redeploys an existing image (rollback).
- Per-environment config is committed as `src/backend/.env.{dev,staging,prod}.example` and merged with CI secrets by
  `deploy/render_runtime_spec.py` (one source per key; leftover `<set-in-deploy>` fails); the eval job uses the same
  tiers/fallbacks. New `deploy/env_file.py` + `make check-env`: the platform reads env files literally (quotes,
  inline comments, `export` break them).
- Langfuse `release` = image tag: `ARG/ENV AGENT_VERSION` in the Dockerfile, passed by `make docker-build` and CI.
- Dockerfile: multi-stage (uv only in the builder), no `chown -R` layer; `.dockerignore` uses `**/` patterns.
- `pyproject.toml`: every directly imported package declared (openai, starlette, opentelemetry-api; test-only ones in
  `dev`), upper bounds at the next major (`greennode-agentbase` < 1.1).
- `scaffold.sh`: project names may not end with `-`; recovery hints on failure; records `standard_version` /
  `scaffold_version`; `.agentbase-state.json` gains `environments.{dev,staging,prod}`.
- Deploy skill: `/agentbase-deploy` only pushes + creates/updates (no root Dockerfile), re-sends VPC settings,
  update-based rollback, teardown checklist (gateways, Policy Groups, connectors, orphaned SAs, API key hashes).
- This repo: `.github/workflows/skills-ci.yml` (scaffold → tests + ruff, frontmatter ≤ 1024 chars, links, `bash -n`).

Orchestrator and references
- Platform scripts run with cwd `src/backend`; never the paste-secret option when credentials are missing.
- Policy principals: developer (`make check-creds`) vs each environment's runtime (Step 9b, after deploy); separate
  dev/prod gateways; plan for 10 policies per group; one developer service account per person.
- Which secrets may live in the runtime env file vs AgentBase Identity; right-to-erasure runbook; data residency
  (Third-party MaaS models, Langfuse Cloud default); all backend modules are required (disable via env).
- Architecture: structured output through `get_llm` (JSON pattern), reflect → agent retry loop, `CONVERSATION_TASKS`
  and `LLM_MODULES` in HARD RULE 3; tracing tree lists `a2a`, `hitl_unmatched` and `identity.<kind>` spans.

Agent runtime (scaffold, auth, HITL, A2A, identity)
- Only app-written errors reach the caller verbatim; platform 4xx (e.g. Memory/Identity 403 naming the principal)
  become a generic 502, LLM configuration errors (401/402/403/404) a distinct 502 pointing at `make check-creds`.
- A chat while an approval is pending returns 409 with the pending `interrupt` (HTTP `details.interrupt`, SSE
  `error.interrupt`); a resume for an earlier card is refused by `interrupt_id` and by the decision ids.
- HITL decisions are validated (400, never 500); a decision for a call is honored even if `HITL_TOOLS` changed; the
  rejected-call message names the user as the decider and forbids a reworded retry (a real run re-asked and told
  the user "the memory system refused").
- `thread_config` exposes the raw `session_id` (A2A `contextId` no longer leaks `<user>::<session>`); A2A decision
  words with nothing pending fail deterministically; startup warning when A2A timeouts don't fit.
- Inbound JWT: bounded JWKS fetch (5 s, 30 s refetch cooldown, 10 s total ⇒ 503), asymmetric algorithms only, https
  issuer/JWKS outside local, auth header limited to `Authorization` / `X-GreenNode-AgentBase-Custom-*`; a local
  `AUTH_MODE=none` run binds 127.0.0.1.
- `make check-creds` pings every tier's primary and fallback model (a model not enabled for the key is a FAIL).

LLM, memory, reflection, eval
- Thinking text is stripped from returned messages (it reached the checkpoint); tracing still records it.
- Per-task output caps (`LLM_TASK_MAX_TOKENS`), judge on the small tier, `REFLECTION_TIMEOUT_S`,
  `get_llm(task, schema=…)` for structured output, tier typos rejected at startup.
- Each turn gets a context message with the current date/time (`APP_TIMEZONE`) and recalled notes framed as data;
  the system prompt has "Dates & time" and "Memory" rules (no unprompted `remember`, no invented dates).
- `remember` guarded (length cap, duplicates); `LTM_MIN_SCORE` 0.5 sent as `scoreThreshold`.
- Eval: forbidden tools always fail, empty dataset exits 2, concurrency 1 / min pass rate 0.8 by default,
  timestamped run names.

MCP (agent side and MCP server template)
- Transport/HTTP failures reach the LLM as `MCP_ERROR (<server>): <summary>` without URL or query (API keys in
  connector URLs leaked into traces); policy denials tracked per (server, tool); `"prefix": false`; https required
  for `iam` / `user_jwt` servers; IAM token single-flight with one retry on 401; `log_iam_principal()` at startup.
- `search_knowledge` handles Vietnamese (NFC, case and diacritic folding, IDF ranking).
- MCP server: client-credentials (2LO) tokens carry no user; clean upstream errors; scopes fail closed; SQLite
  refused outside local; multi-stage image with read-only `/app`.

Frontend (Expo)
- 409 + `interrupt` re-renders the approval card; the first done/interrupt/error ends a turn; Edit and per-call
  reject reasons on the card; Stop button and idle timeout; sign-out revokes the refresh token and ends the IdP
  session; release builds refuse a missing / non-https agent URL.
- Setup pins `create-expo@5.0.1` (Expo SDK 57), no nested `.git`, no prompt hang, validated project name.

Tracing (Langfuse v4)
- Session page readable as a conversation: the root observation carries the user message and the answer text
  (HITL turns as readable approval lines), the "Conversation" saved view filters root observations, generation
  I/O in OpenAI chat format with tools and thinking, plain chains without I/O, stable `<domain>.<action>` names.
- Fixed SDK 4.18 events losing their parent in async graphs; `lf_trace.py --audit` / `--session` for review.

## 0.2.0

Baseline before the enterprise review.
