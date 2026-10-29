# AgentBase Build Skills

A set of [SKILL.md](https://agentskills.io) skills that helps **coding agents (Claude Code, Codex, …) build AI agents on GreenNode AgentBase to a single standard**. Business requirements differ, but the structure and the way you get the LLM, memory, tracing, tools, auth, HITL, evaluation and UI are always the same.

This set **complements** [`vngcloud/greennode-agentbase-skills`](https://github.com/vngcloud/greennode-agentbase-skills). The platform's `agentbase-*` skills handle resource operations (API keys, memory stores, gateways, runtimes…). The `agentbase-build-*` skills here handle the agent's **code & architecture**, and call into the platform skills when needed.

## Skills

| Skill | Responsible for |
|---|---|
| **`/agentbase-build`** | Entry point: standard stack, project structure, 9-step process, hard rules, review mode. References: architecture, Agent Spec (requirements-to-spec), **decision guide (when to enable which component)**, API contract, DoD checklist, security (incl. right-to-erasure runbook), platform coverage, private networking, IAM & team permissions |
| `/agentbase-build-scaffold` | Creates `src/backend` (+ `src/frontend`), env managed with **uv**, Makefile, Dockerfile |
| `/agentbase-build-llm` | LLMs from GreenNode MaaS: **capability tiers** (reasoning/large/small), **flow → tier**, adaptive routing, **fallback model**, streaming/usage, prompt cache |
| `/agentbase-build-memory` | Short-term (`AgentBaseMemoryEvents`), long-term (records + auto-recall + tools), **context compression** |
| `/agentbase-build-tracing` | **[Langfuse](https://github.com/langfuse/langfuse) v4**, aligned with the Langfuse best practices and the official [Langfuse skill](https://github.com/langfuse/skills): per-step trace tree with specific observation types, model/usage/cost/cache/TTFT/thinking, masking, Prompt Management, feedback, run-and-audit loop |
| `/agentbase-build-mcp` | Local tools + **MCP Connectors** (catalog/custom) on the **MCP Gateway** + **Policy Group** (principal `iam:<sub>`, action `connector__tool`), policy-deny guard |
| `/agentbase-build-mcp-server` | **Build an MCP server** with auth: API key / OAuth JWT (2LO, 3LO, inbound forward), scopes, user from token, Protected Resource Metadata |
| `/agentbase-build-a2a` | **A2A**: expose the agent (Agent Card + JSON-RPC) and call other agents as tools, isolate tasks/memory per user — only when decision guide §5 is satisfied |
| `/agentbase-build-auth` | Inbound JWT (OIDC/JWKS), user-spoofing prevention, Runtime inbound auth, Direct/BFF models |
| `/agentbase-build-identity` | **Access Control** (Identity): Static/Delegated API key and OAuth2 providers, agent-wide (M2M) and per-user (3LO consent link, non-blocking) credentials in tools, no secrets in schemas/traces |
| `/agentbase-build-hitl` | **Human-in-the-loop**: `interrupt()` to approve / edit / reject tool calls, resume |
| `/agentbase-build-eval` | **Evaluation loop**: Langfuse Datasets/Experiments, LLM judge, CI gate, online scores, self-eval loop |
| `/agentbase-build-web` | **Next.js web chat + BFF**: server-side OIDC code + PKCE (tokens in a sealed httpOnly cookie, CSRF), IAM-protected Runtime support, SSE streaming, HITL approval cards, 👍👎 feedback, strict nonce CSP |
| `/agentbase-build-frontend` | **Expo React Native**: OIDC PKCE, SSE streaming, HITL approval cards, 👍👎 feedback |
| `/agentbase-build-security-audit` | **Security audit**: `make security-scan` (offline scan of the standard's security invariants, gates CI deploys), and a full source-first audit with Cloudflare's [security-audit skill](https://github.com/cloudflare/security-audit-skill) seeded with the AgentBase threat model (prompt injection, excessive agency, cross-user leaks, MCP/A2A trust); findings fixed in the owning skill with a regression test |
| `/agentbase-build-deploy` | Pre-flight, per-environment env files (strict format, `make check-env`), linux/amd64 image, `/agentbase-deploy` or CI (`grn`) keeping network/security settings, verify, rollback, teardown checklist |

## Repo structure

```
.claude-plugin/              plugin.json, marketplace.json      (Claude Code plugin)
.codex-plugin/               plugin.json                        (Codex plugin)
.agents/plugins/             marketplace.json                   (Codex marketplace)
.github/workflows/           skills-ci.yml — checks THIS repo (scaffold + tests + ruff, SKILL.md frontmatter, links, bash -n,
                             docs ↔ code drift — see Repo checks)
.github/scripts/             check_skills.py, check_drift.py, drift_allowlist.py
CHANGELOG.md                 versions of the standard (also recorded in each generated project's .agentbase-state.json)
skills/
  agentbase-build/           SKILL.md + references/ (architecture, requirements-to-spec, decision-guide, api-contract,
                             checklist, security, platform-coverage, private-networking, iam-permissions)
  agentbase-build-<x>/       SKILL.md + references/ + assets/backend/... (verified sample code)
                             + scripts/ (scaffold.sh, setup_frontend.sh, lf_trace.py)
```

Each skill **owns** its piece of code in `assets/backend/` (at the exact target path inside `src/backend/`). `agentbase-build-scaffold/scripts/scaffold.sh` assembles these pieces into a complete project.

## Repo checks

`python3 .github/scripts/check_skills.py` (from the repo root; Python ≥ 3.12, standard library only, < 1 s) is what the `static` job of `skills-ci.yml` runs on every push and PR — run it before committing (older system Python: `uv run --python 3.12 --no-project python3 .github/scripts/check_skills.py`). It checks SKILL.md frontmatter, internal links and version stamps, then compares the docs with the code (`check_drift.py`):

| Check | Fails when |
|---|---|
| `settings-env` | a `Settings` field of `app/config.py` has no active line in `.env.example` (an empty value means the default); a key of `.env.example` / `env.deploy.example` is read by nothing; a deploy file sets `GREENNODE_*` / `AGENT_VERSION`, or a `<set-in-deploy>` secret that `ci.yml` does not fill; `ci.yml` uses a secret / variable its header does not list, or exports config the eval SKILL does not name; the same pairs for the MCP server and frontend templates |
| `trace-names` | a name the backend emits (`tracing.step` / `event` / `score_trace`, `run_name`, graph node, `get_llm` task) is missing from the tracing SKILL.md tree; that tree, tracing.py's docstring tree or `references/trace-schema.md` names something no code emits; observation types or trace metadata keys disagree |
| `make-targets` | a doc runs `make <target>` that the Makefile of its context (project root, or the MCP server for its skill) does not define |
| `paths` | a cited template path (`app/…`, `tests/…`, `deploy/…`, `src/backend/…`) exists in no skill |
| `env-names` | an UPPER_SNAKE config name in a doc or template is not a setting, env-file key, variable read by code, or CI / Makefile name |
| `build-sync` | the two Dockerfiles diverge (python and uv images, stages, `uv sync` flags, `chown -R`), `uv … --frozen` appears anywhere, a doc cites another python image, or a doc copies CI workflow steps (describe them and point to `ci.yml` instead) |

Every failure names the file, the line and the fix. Fix the doc or the template; when a difference is intended, add it to `.github/scripts/drift_allowlist.py` with its reason (an entry nothing needs any more fails too, so the list stays short).

## Generated project

```
<project>/
├── Makefile  README.md  .agentbase-state.json   # state: standard/scaffold version + resource IDs per environment
├── .agentbase-template.json   # owner class + template hash per generated file (scripts/upgrade.py upgrades the project)
├── .github/workflows/ci.yml   # lint + test, eval gate, manual deploy/redeploy (grn: deploy up first, runtime update after)
├── deploy/agent.yaml.tpl      # AgentBase manifest, rendered in CI
├── deploy/render_runtime_spec.py  # merges .env.<env>.example + CI secrets, derives the `runtime update` spec
├── deploy/env_file.py         # strict KEY=VALUE env-file lint (`make check-env`)
└── src/
    ├── backend/   # uv · LangGraph · greennode-agentbase · Langfuse v4 · MCP · JWT · HITL · evals
    │              # + .env.{dev,staging,prod}.example: committed non-secret config per environment
    └── frontend/  # (optional) Expo React Native
```

```bash
bash skills/agentbase-build-scaffold/scripts/scaffold.sh my-agent ./my-agent --with-frontend
cd my-agent && make test          # then, in YOUR terminal: make set-secret (IAM + LLM key, hidden input),
                                  # `make check-creds`, `make dev`, and in another terminal: make invoke MSG="hello"
```

## Official GreenNode docs

Platform behavior (Runtime, Memory, Identity, MCP Gateway, Policy, MaaS) is documented at **https://docs.greennode.ai**. Each skill ends with an *Official docs* section linking the relevant pages; https://docs.greennode.ai/llms.txt indexes every page, and any page URL + `.md` returns Markdown.

## Prerequisites

To build and run an agent you need these values. **Never paste secrets into the chat with your coding agent** — everything typed there is stored by the agent host. When a step needs one, the agent asks you to run **`make set-secret`** in your own terminal (a new terminal tab, your IDE's terminal, or the Claude Code desktop app's Terminal pane — not the chat's `!` prefix): it reads the value with a hidden prompt, checks it and writes it to the git-ignored file. Then `make check-creds` prints only OK/FAIL and non-secret IDs. Deploy secrets: `gh secret set <NAME> --env <env>` (CI) or `make set-secret ENV_FILE=src/backend/.env.<env>` (`/agentbase-deploy`). Pasted one anyway? Rotate it (`skills/agentbase-build/references/security.md`).

| Value | Get it with | Set it with (your terminal) |
|---|---|---|
| IAM service account `client_id` + `client_secret` (Memory, Identity, MCP Gateway) | `/agentbase` (IAM setup) | `make set-secret NAME=iam` → `src/backend/.greennode.json` |
| `LLM_API_KEY` | `/agentbase-llm` (it can save the key itself) | `make set-secret NAME=LLM_API_KEY` → `src/backend/.env` |
| `LLM_MODEL` (not secret) | `/agentbase-llm` | `src/backend/.env` |
| [Langfuse](https://github.com/langfuse/langfuse) public/secret key (optional) | Langfuse project settings (Langfuse Cloud or self-hosted) | `make set-secret NAME=langfuse` → `src/backend/.env` |

Tools: Python 3.13 + [uv](https://docs.astral.sh/uv/), Docker (deploy), Node.js + npm (frontend only).

## Installation

The skills follow the open [Agent Skills](https://agentskills.io) format (`skills/<name>/SKILL.md`), so any coding agent that reads `SKILL.md` can use them. Pick the method for your agent:

| Coding agent | Recommended method |
|---|---|
| **Claude Code** | Plugin marketplace (below) |
| **Codex CLI** | Plugin marketplace (below) |
| **Cursor, Gemini CLI, GitHub Copilot, OpenCode, Windsurf, Cline, Kiro, …** | [`npx skills`](#any-agent--skills-cli) or [`gh skill`](#any-agent--github-cli) |
| Anything else | [Manual copy](#manual) |

> Install the platform skill set [`vngcloud/greennode-agentbase-skills`](https://github.com/vngcloud/greennode-agentbase-skills) alongside it, using the same method. The `agentbase-build-*` skills call those skills (`/agentbase-deploy`, `/agentbase-gateway`, `/agentbase-identity`…) for platform operations.
>
> Also install the official [Langfuse skill](https://github.com/langfuse/skills) (`npx skills add langfuse/skills --skill langfuse`). `agentbase-build-tracing` uses it for up-to-date Langfuse docs, `langfuse-cli` and its playbooks, and keeps the AgentBase-specific rules (names, types, masking, MaaS) itself.
>
> For security audits, install Cloudflare's [security-audit skill](https://github.com/cloudflare/security-audit-skill) (`npx skills add https://github.com/cloudflare/security-audit-skill --skill security-audit`). `agentbase-build-security-audit` runs its full workflow with the AgentBase threat model and keeps the quick pre-deploy scan itself.

### Claude Code

```bash
claude plugin marketplace add greennode-samples/sample-greennode-agentbase-builder-skills
claude plugin install agentbase-build@agentbase-build
```

You can also run the same steps inside a Claude Code session: `/plugin marketplace add greennode-samples/sample-greennode-agentbase-builder-skills`, then `/plugin install agentbase-build@agentbase-build`. Restart the session after installing.

Update: `claude plugin marketplace update agentbase-build && claude plugin update agentbase-build@agentbase-build`.

### Codex CLI

```bash
codex plugin marketplace add greennode-samples/sample-greennode-agentbase-builder-skills
codex plugin add agentbase-build@agentbase-build
```

Update: `codex plugin marketplace upgrade`. Remove: `codex plugin remove agentbase-build`.

### Any agent — skills CLI

[`skills`](https://github.com/vercel-labs/skills) detects the agents you have installed and links the skills into each agent's folder.

```bash
npx skills add greennode-samples/sample-greennode-agentbase-builder-skills --list                       # show the 16 skills
npx skills add greennode-samples/sample-greennode-agentbase-builder-skills                              # interactive: choose skills + agents (project scope)
npx skills add greennode-samples/sample-greennode-agentbase-builder-skills -g -a cursor -a gemini-cli -y   # all skills, user scope, specific agents
npx skills add greennode-samples/sample-greennode-agentbase-builder-skills --skill agentbase-build-mcp-server -a github-copilot   # a single skill
npx skills update                               # pull new versions
```

Agent IDs: `claude-code`, `codex`, `cursor`, `gemini-cli`, `github-copilot`, `opencode`, `windsurf`, `cline`, `kiro-cli`, …

### Any agent — GitHub CLI

Requires a GitHub CLI version that includes `gh skill` (check with `gh skill --help`).

```bash
gh skill install greennode-samples/sample-greennode-agentbase-builder-skills --all --agent cursor               # project scope (default)
gh skill install greennode-samples/sample-greennode-agentbase-builder-skills --all --agent codex --scope user   # user scope
gh skill install greennode-samples/sample-greennode-agentbase-builder-skills agentbase-build-mcp-server         # a single skill
```

### Manual

Copy or symlink each directory in `skills/` into your agent's skills folder:

| Agent | Project | User (global) |
|---|---|---|
| Claude Code | `.claude/skills/` | `~/.claude/skills/` |
| Codex | `.agents/skills/` | `~/.codex/skills/` |
| Cursor | `.agents/skills/` | `~/.cursor/skills/` |
| Gemini CLI | `.agents/skills/` | `~/.gemini/skills/` |
| GitHub Copilot | `.agents/skills/` | `~/.copilot/skills/` |
| OpenCode | `.agents/skills/` | `~/.config/opencode/skills/` |
| Windsurf | `.windsurf/skills/` | `~/.codeium/windsurf/skills/` |
| Kiro | `.kiro/skills/` | `~/.kiro/skills/` |

```bash
git clone https://github.com/greennode-samples/sample-greennode-agentbase-builder-skills.git
mkdir -p ~/.claude/skills && cp -R sample-greennode-agentbase-builder-skills/skills/* ~/.claude/skills/   # adjust the target folder for your agent
```

### Using the skills

Agents load a skill automatically when your request matches its description. **Invoke `/agentbase-build` explicitly** when you want this standard: the platform's `/agentbase-wizard` describes itself as mandatory for *any* build request, so an unqualified "build me an agent" may land there. Teams can make it the default with one line in the project's `AGENTS.md` / `CLAUDE.md`:

```markdown
To build, change or review an AI agent on GreenNode AgentBase, use the agentbase-build skill (/agentbase-build), not /agentbase-wizard; use the platform agentbase-* skills only for resource operations.
```

Plugin skills may appear namespaced, like `/agentbase-build:agentbase-build` or `agentbase-build:agentbase-build-llm` in Claude Code. Example prompts:

- *"Use agentbase-build to build an HR assistant agent that answers leave-policy questions and creates leave requests."* The orchestrator walks through all 9 steps and calls the other skills.
- *"Use agentbase-build-mcp-server to write an MCP server exposing our orders API, with OAuth 3LO."*
- *"Use agentbase-build-mcp to connect my agent to the GitHub connector through the MCP Gateway."*

## Verified

- **Offline**: a freshly scaffolded project passes its full suite — 570+ backend tests, 48 web tests (agent, per-user isolation, streaming, compression, JWT/api_key incl. client allowlist and Runtime env guard, HITL, reflection, eval, tier/fallback/adaptive routing, MCP guard and auth validation, A2A e2e incl. no LLM self-approval, check-creds never prints secrets, outbound credentials via a fake Identity API incl. non-blocking 3LO and per-user isolation, eval gate, compression invariants, tracing masks); MCP server template passes its e2e suite (fail-closed auth incl. JWT audience and JWKS rotation, per-user isolation, atomic idempotency, tools, local quickstart); Python 3.13, ruff clean — re-checked on every push by `.github/workflows/skills-ci.yml`. Frontend: `tsc` + Android bundle (Expo SDK 57).
- **Real GreenNode** (runtime `test-agent`, v1→v6): api_key auth (401), short/long-term memory on AgentBase Memory, per-user isolation (memory, HITL, feedback, A2A tasks), HITL on real memory, MCP Connector via Gateway + Policy (ALLOW/DENY), A2A Agent Card + task isolation, real model tiers + fallback on MaaS (10 models), prompt cache.
- **Self-hosted [Langfuse](https://github.com/langfuse/langfuse) v4.49**: full trace tree, cost/cache/reasoning/TTFT, prompt versions, scores, dataset + experiment. Traces were used to find and fix a 55s → 11s slowdown (parallel MCP + negative cache, skipping reflection for simple questions, Memory timeouts).
- **Self-hosted Langfuse v4.58 + real MaaS** (tracing standard aligned with the Langfuse best practices): chat, knowledge search, tool call, HITL interrupt/approve, same-session follow-up, streamed answer with reflection, 403 primary ⇒ fallback, feedback — one `agent` per trace, retriever/evaluator types, `llm.<task>.generate` names with the model as attribute, thinking text (`reasoning_content`) on every generation, masked feedback comment; `lf_trace.py --audit` clean on every trace.
- Per-feature details, platform bugs encountered and open items: `skills/agentbase-build/references/platform-coverage.md`.

## To verify on a real environment

- Runtime Inbound Auth (IAM / JWT) and IP Access Control end-to-end with this template (documented, not yet exercised; affects Direct vs BFF, see `agentbase-build-auth`).
- OAuth2 3LO consent round-trip and delegated keys through `app/identity.py` (tested against the SDK with a fake Identity API).
- `grn agentbase deploy up` / `runtime update` and the generated CI workflow on GitHub (deploy logic exercised against a mock `grn`; templates rendered and linted offline only).
- Long-term memory auto-extraction from this template's events (see `agentbase-build-memory`).
- Session rotation (`SESSION_MAX_TURNS`) on real AgentBase Memory, and the CI's build-once / digest steps (`docker buildx imagetools`) against the AgentBase Container Registry.

## Supply chain

The backend and MCP server templates ship a tested `uv.lock` (installed with `uv sync --locked`) and pin their base images by digest; generated projects get a `.github/dependabot.yml` (uv, docker, GitHub Actions, npm) and `make security-scan` S12 warns when a project has none. Details: `agentbase-build-scaffold` → *Supply chain*.

## License, security, contributing

MIT — see `LICENSE`. Report vulnerabilities privately as described in `SECURITY.md`. How to change a skill and what CI checks: `CONTRIBUTING.md`.
