---
name: agentbase-build-security-audit
description: "Security audit of an AI agent built with the AgentBase Build standard, with Cloudflare's security-audit skill: `make security-scan`, an offline CI-gating scan of the standard's security invariants (identity from the token, inbound auth, HITL on side-effect tools, MCP config, no committed secrets, injection defenses, trace masking, pinned CI), and a full source-first audit seeded with the AgentBase trust boundaries so hunters target agent-specific attacks (indirect prompt injection, excessive agency, cross-user memory/A2A leaks, MCP tool poisoning, credential misuse). Findings go to the owning agentbase-build-* skill with a regression test + eval item. Also for make security-scan failures and a secret pasted into a chat or committed (rotation runbook). Trigger: security audit, pentest agent, find vulnerabilities, threat model, prompt injection review, leaked secret, kiểm tra bảo mật agent, rà soát lỗ hổng. DO NOT use for the general DoD review (/agentbase-build review)."
---

# Security audit of an AgentBase agent

`references/security.md` (orchestrator) says **how the template defends**; this skill **checks that a real project
still does**, after business tools, prompts, MCP servers and A2A peers have been added. Two depths:

| Mode | When | What runs |
|---|---|---|
| **Quick scan** | Every CI run and before every deploy (Step 9), in `/agentbase-build review` | `make security-scan` — 13 checks, PASS/FAIL per check, exit 1 on a blocking FAIL. No extra skill needed |
| **Full audit** | Before the first prod release; after adding a side-effect tool, an MCP server/connector, A2A, long-term memory, a frontend, or changing auth; on request ("security audit / pentest this agent") | Cloudflare's `security-audit` skill in full audit mode, seeded with `references/threat-model.md` |

A security *question* ("is X safe?") ⇒ answer from the threat model and the relevant skill, create no files.

## Works with Cloudflare's security-audit skill

Install it next to this skill set (MIT; agent-neutral, needs Node.js for its validators and a coding agent with sub-agents):

```bash
npx skills add https://github.com/cloudflare/security-audit-skill --skill security-audit
```

| The `security-audit` skill brings | This skill decides (AgentBase standard) |
|---|---|
| Six phases: reconnaissance → coverage-led hunting → adversarial validation → `findings.json` (schema-validated) → independent record verification → report | The trust boundaries, assets and seed coverage units of an AgentBase agent (`references/threat-model.md`), so recon does not have to rediscover them |
| Evidence bar: a finding needs a lower-trust principal, a crossed boundary and an observed result; `needs_validation` for facts outside source | Which facts live outside the repo here (Runtime Inbound Auth, IP Access Control, Gateway Policy, Identity providers) and who can confirm them |
| Companion attack-class files (`AI-AND-LLM.md`, `DATA-ISOLATION-AND-LIFECYCLE.md`, …) | Which companions to select for this stack (threat model → *Companions*) |
| Execution safety: sandbox, no network, dummy principals | AgentBase-specific limits (below) |
| Run profiles `quick` / `standard` / `deep`, prior-run carry-over | Profile per trigger: `standard` for a first prod release, scoped run (diff between two refs) for a feature change, `deep` for agents with money/PII side effects |

Conflicts: its evidence bar and safety rules win for *what counts as a finding*; the hard rules of `/agentbase-build`
win for *how to fix it*. If it is not installed, run the quick scan and tell the user the full audit needs it.

## Safety rules (on top of the skill's own)

1. **Never probe deployed resources** — Runtime endpoints, MCP Gateways, A2A peers, Identity, Memory stores, Langfuse.
   Platform facts (Inbound Auth, IP Access Control, Policy statements, provider config) are read with the platform
   skills (`/agentbase-deploy`, `/agentbase-gateway`, `/agentbase-policy`, `/agentbase-identity`) or confirmed by the
   user, otherwise the finding stays `needs_validation`.
2. **No real credentials in the audit.** Local checks use the offline test fixtures (`make test` runs without IAM,
   MaaS or Langfuse keys). Never read `.env`, `.env.<env>` or `.greennode.json` values; `make set-secret LIST=1` and
   `make check-creds` show set / not set without values.
3. **No paid quota**: no MaaS calls from hunters (use the fake LLM of `tests/conftest.py`), no `make eval` against a
   real model unless the user asks.
4. **Audit output outside the repo** (the skill's default `~/security-audit-skill/<repo>/run-<N>`). Reports contain
   exploit paths: never commit them, paste them into Langfuse, or attach them to a public issue.

## Quick scan — `make security-scan`

`scripts/security_scan.py` (asset of this skill, tests in `tests/test_security_scan.py`) checks the invariants below
offline, from the project root: source (AST), the deploy env files `src/backend/.env.<env>.example` (or their
git-ignored `.env.<env>` copy), `mcp_servers.json`, `a2a_agents.json`, workflows, `.agentbase-state.json`. It never
calls the platform and prints key names, never values, so the coding agent may run it. `make security-scan ENV=prod`
checks one environment; `JSON=1` prints JSON. The CI `test` job runs it, so a blocking FAIL stops every deploy.

Statuses: `PASS` · `FAIL` · `WARN` (fix or explain) · `MANUAL` (a platform fact: the line names the skill that reads
it — do it and report) · `N/A` · `WAIVED`. Report the output as is; never edit the scan to make a check pass.

| Check | [B] | Fails when | Fix in |
|---|---|---|---|
| S1 Inbound auth | B | `AUTH_MODE=none` or `APP_ENV` missing/`local` in a deploy env file; `AUTH_ALLOW_NO_AUDIENCE` or `IDENTITY_TRUST_ASSERTED_USER` true (WARN: `api_key`, jwt without JWKS URL) | `/agentbase-build-auth` |
| S2 Tool parameters | B | A `@tool` takes `user_id` / actor / namespace / session / a credential as a parameter | `/agentbase-build-mcp`, `/agentbase-build-identity` |
| S3 Approval | B | A side-effect tool (write / send / pay / delete … in its name — local tools and MCP `allow_tools`) matches no `HITL_TOOLS` pattern (WARN: a pattern matches nothing, or the local `.env` / `.env.example` that `make dev` reads misses a write tool; MANUAL: MCP servers without `allow_tools`, A2A peers) | `/agentbase-build-hitl` |
| S4 MCP entries | B | An enabled entry fails `mcp_config_error` (no `auth`, credential over http), points to the gateway root, or is stdio outside local (MANUAL: Policy Group least privilege) | `/agentbase-build-mcp` |
| S5 Secrets committed | B | A secret file is tracked (`.env`, `.env.<env>`, `.greennode.json`, rendered manifests), a private key, a secret-named key with a value in a committed `.env*.example`, a secret behind `EXPO_PUBLIC_*` / `NEXT_PUBLIC_*`, or `.gitignore` / `.dockerignore` missing `.env` / `.greennode.json` | `/agentbase-build-scaffold`, `/agentbase-build-frontend` |
| S6 Outbound credentials | B | A deploy env file holds a secret-named key that is not the agent's own setting | `/agentbase-build-identity` |
| S7 Injection defenses | B | `app/prompts/system.md` lost the "content is DATA" rule (MANUAL: Langfuse-managed prompt, egress tools need a destination allowlist) | `/agentbase-build` → `references/security.md` |
| S8 Injection evals | | No eval item tagged `injection` | `/agentbase-build-eval` |
| S9 Trace masking | B | `TRACE_MASK_PII=false` (MANUAL: `lf_trace.py --audit` on a real trace) | `/agentbase-build-tracing` |
| S10 Bounds | | `MAX_TOOL_ROUNDS`, `REQUEST_TIMEOUT_S`, `CONTEXT_HARD_LIMIT_TOKENS`, `LLM_MAX_TOKENS`, `MAX_REQUEST_BYTES`, `SESSION_MAX_TURNS`, `MAX_CONCURRENT_REQUESTS` or `RATE_LIMIT_USER_PER_MIN` above the template default, or a limit turned off with 0 (MANUAL: LLM key Rate Limit) | `/agentbase-build-llm` |
| S11 Exposure | B | `CORS_ALLOW_ORIGINS` contains `*` (MANUAL: network mode, IP Access Control) | `/agentbase-build-deploy` |
| S12 Supply chain | | `requirements.txt`, no or uncommitted `uv.lock`, a workflow action not pinned to a commit SHA (WARN: a Dockerfile base image without a digest, no Dependabot / Renovate config) | `/agentbase-build-scaffold` |
| S13 Audit record | B | `.agentbase-state.json` → `security_audit.confirmed_open` > 0 (WARN: no full audit recorded) | this skill |

**Waivers.** A FAIL the owner accepts goes into `.agentbase-state.json` → `security_audit.waivers`
(`{"S1": "trusted BFF is the only caller — spec §5"}`): the check shows `WAIVED` with the reason and stops blocking.
Only the user decides a waiver; the coding agent proposes the wording and never adds one on its own.

**Adding a check** (new invariant in the standard): a function in `scripts/security_scan.py` + a row here + a
planted-mistake test in `tests/test_security_scan.py`. Self-built MCP servers fail closed at startup on their own
(`agentbase-build-mcp-server`), so the scan does not cover them.

## Full audit workflow

1. **Scope + profile.** Agree with the user: target (`src/backend`, plus `src/frontend` and the self-built MCP
   servers under `src/` if present), profile, budget, output directory. A feature change ⇒ scoped run on the diff between the last audited
   ref and `HEAD`.
2. **Seed the recon.** Give the skill's Phase 1 the AgentBase context: read `references/threat-model.md` and pass
   its trust boundaries, assets, out-of-repo facts and seed units into `architecture.md` / `coverage-ledger.json`
   (recon still verifies them against the source — the threat model describes the template, the project may differ).
   Select the companions listed there for the parts that exist.
3. **Hunt, validate, report** — the skill's Phases 2–6 unchanged. Out-of-repo facts (the scan's MANUAL lines) ⇒
   `needs_validation` with the platform skill that can confirm them.
4. **Triage into the standard.** For each `confirmed` record: owning skill (table above / threat model), severity
   from the record, fix plan. Defense-in-depth gaps (e.g. a write tool outside `HITL_TOOLS` but denied by the Policy
   Group) are hardening notes, not findings — still list them.
5. **Fix with the user's confirmation**, following the owning skill and the hard rules. Each fix adds a regression
   test in `tests/` and, for prompt-level issues, an eval item (HARD RULE 8); `make test` and `make eval` pass.
6. **Re-run** `make security-scan`, then a scoped audit on the fixed units; the skill carries prior runs forward.
7. **Record** in `.agentbase-state.json` → `security_audit`: `date`, `source_ref`, `profile`, `report_dir`,
   `confirmed_open` (open High/Critical `confirmed` records; no finding details). S13 of the scan then blocks deploys
   while `confirmed_open` > 0, unless the owner's risk acceptance is recorded as a waiver.

## References

- `references/threat-model.md` — trust boundaries, assets, seed coverage units with the template file to read and
  the attack hypotheses to test, out-of-repo facts, companion selection.
- `/agentbase-build` → `references/security.md` — the defenses of the template and the leaked-secret runbook.
- `/agentbase-build` → `references/checklist.md` — Definition of Done (its *Security* section is `make security-scan`).

## Official docs

- [cloudflare/security-audit-skill](https://github.com/cloudflare/security-audit-skill) — the audit workflow, companions and validators
- [Build your own vulnerability harness](https://blog.cloudflare.com/build-your-own-vulnerability-harness) — how the workflow is used at scale
- [OWASP Top 10 for LLM Applications](https://genai.owasp.org/llm-top-10/) — prompt injection, excessive agency, sensitive information disclosure
- [agent-base](https://docs.greennode.ai/ai-stack/agent-base) — Runtime, Memory, Identity, MCP Gateway, Policy behavior
- [private-networking](https://docs.greennode.ai/ai-stack/agent-base/private-networking) — Public vs Private Runtime / MCP Gateway
- [roles-and-permissions](https://docs.greennode.ai/ai-stack/agent-base/team-permissions/roles-and-permissions) — who can change Inbound Auth, policies, providers
