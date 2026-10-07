# AgentBase Build Skills

A set of [SKILL.md](https://agentskills.io) skills that helps **coding agents (Claude Code, Codex, …) build AI agents on GreenNode AgentBase to a single standard**. Business requirements differ, but the structure and the way you get the LLM, memory, tracing, tools, auth, HITL, evaluation and UI are always the same.

This set **complements** [`vngcloud/greennode-agentbase-skills`](https://github.com/vngcloud/greennode-agentbase-skills). The platform's `agentbase-*` skills handle resource operations (API keys, memory stores, gateways, runtimes…). The `agentbase-build-*` skills here handle the agent's **code & architecture**, and call into the platform skills when needed.

## Skills

| Skill | Responsible for |
|---|---|
| **`/agentbase-build`** | Entry point: standard stack, project structure, 9-step process, hard rules, Agent Spec, **decision guide (when to enable which component)**, API contract, DoD checklist, review |
| `/agentbase-build-scaffold` | Creates `src/backend` (+ `src/frontend`), env managed with **uv**, Makefile, Dockerfile |
| `/agentbase-build-llm` | LLMs from GreenNode MaaS: **capability tiers** (reasoning/large/small), **flow → tier**, adaptive routing, **fallback model**, streaming/usage, prompt cache |
| `/agentbase-build-memory` | Short-term (`AgentBaseMemoryEvents`), long-term (records + auto-recall + tools), **context compression** |
| `/agentbase-build-tracing` | **[Langfuse](https://github.com/langfuse) v4**: detailed per-step trace tree, model/usage/cost/cache/TTFT, masking, Prompt Management, feedback |
| `/agentbase-build-mcp` | Local tools + **MCP Connectors** (catalog/custom) on the **MCP Gateway** + **Policy Group** (principal `iam:<sub>`, action `connector__tool`), policy-deny guard |
| `/agentbase-build-mcp-server` | **Build an MCP server** with auth: API key / OAuth JWT (2LO, 3LO, inbound forward), scopes, user from token, Protected Resource Metadata |
| `/agentbase-build-a2a` | **A2A**: expose the agent (Agent Card + JSON-RPC) and call other agents as tools, isolate tasks/memory per user — only when decision guide §5 is satisfied |
| `/agentbase-build-auth` | Inbound JWT (OIDC/JWKS), user-spoofing prevention, Runtime inbound auth, Direct/BFF models |
| `/agentbase-build-identity` | **Access Control** (Identity): Static/Delegated API key and OAuth2 providers, agent-wide (M2M) and per-user (3LO consent link, non-blocking) credentials in tools, no secrets in schemas/traces |
| `/agentbase-build-hitl` | **Human-in-the-loop**: `interrupt()` to approve / edit / reject tool calls, resume |
| `/agentbase-build-eval` | **Evaluation loop**: Langfuse Datasets/Experiments, LLM judge, CI gate, online scores, self-eval loop |
| `/agentbase-build-frontend` | **Expo React Native**: OIDC PKCE, SSE streaming, HITL approval cards, 👍👎 feedback |
| `/agentbase-build-deploy` | Pre-flight, per-environment env, linux/amd64 image, `/agentbase-deploy`, verify, rollback |

## Repo structure

```
.claude-plugin/              plugin.json, marketplace.json      (Claude Code plugin)
.codex-plugin/               plugin.json                        (Codex plugin)
.agents/plugins/             marketplace.json                   (Codex marketplace)
skills/
  agentbase-build/           SKILL.md + references/ (architecture, spec, api-contract, checklist)
  agentbase-build-<x>/       SKILL.md + references/ + assets/backend/... (verified sample code)
                             + scripts/ (scaffold.sh, setup_frontend.sh)
```

Each skill **owns** its piece of code in `assets/backend/` (at the exact target path inside `src/backend/`). `agentbase-build-scaffold/scripts/scaffold.sh` assembles these pieces into a complete project.

## Generated project

```
<project>/
├── Makefile  README.md  .agentbase-state.json
└── src/
    ├── backend/   # uv · LangGraph · greennode-agentbase · Langfuse v4 · MCP · JWT · HITL · evals
    └── frontend/  # (optional) Expo React Native
```

```bash
bash skills/agentbase-build-scaffold/scripts/scaffold.sh my-agent ./my-agent --with-frontend
cd my-agent && make test && make dev && make invoke MSG="hello"
```

## Official GreenNode docs

Platform behavior (Runtime, Memory, Identity, MCP Gateway, Policy, MaaS) is documented at **https://docs.greennode.ai**. Each skill ends with an *Official docs* section linking the relevant pages; https://docs.greennode.ai/llms.txt indexes every page, and any page URL + `.md` returns Markdown.

## Prerequisites

To build and run an agent you need these values. **Never paste secrets into the chat with your coding agent.** It creates the files from `.example` templates, you fill them in your editor, then you run `make check-creds`, which prints only OK/FAIL and non-secret IDs.

| Value | Get it with | Put it in |
|---|---|---|
| IAM service account `client_id` + `client_secret` (Memory, Identity, MCP Gateway) | `/agentbase` (IAM setup) | `src/backend/.greennode.json` |
| `LLM_API_KEY` + `LLM_MODEL` | `/agentbase-llm` | `src/backend/.env` |
| [Langfuse](https://github.com/langfuse) public/secret key (optional) | Langfuse project settings (Langfuse Cloud or self-hosted) | `src/backend/.env` |

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
npx skills add greennode-samples/sample-greennode-agentbase-builder-skills --list                       # show the 14 skills
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

Agents load a skill automatically when your request matches its description. You can also name the skill explicitly, for example `/agentbase-build` in Claude Code (plugin skills may appear namespaced, like `/agentbase-build:agentbase-build`). Example prompts:

- *"Use agentbase-build to build an HR assistant agent that answers leave-policy questions and creates leave requests."* The orchestrator walks through all 9 steps and calls the other skills.
- *"Use agentbase-build-mcp-server to write an MCP server exposing our orders API, with OAuth 3LO."*
- *"Use agentbase-build-mcp to connect my agent to the GitHub connector through the MCP Gateway."*

## Verified

- **Offline**: a freshly scaffolded project passes 109 tests (agent, per-user isolation, streaming, compression, JWT/api_key incl. client allowlist and Runtime env guard, HITL, reflection, eval, tier/fallback/adaptive routing, MCP guard and auth validation, A2A e2e incl. no LLM self-approval, check-creds never prints secrets); MCP server template passes 21 e2e tests (auth, per-user isolation, tools, local quickstart); Python 3.13, ruff clean. Frontend: `tsc` + Android bundle (Expo SDK 57).
- **Real GreenNode** (runtime `test-agent`, v1→v6): api_key auth (401), short/long-term memory on AgentBase Memory, per-user isolation (memory, HITL, feedback, A2A tasks), HITL on real memory, MCP Connector via Gateway + Policy (ALLOW/DENY), A2A Agent Card + task isolation, real model tiers + fallback on MaaS (10 models), prompt cache.
- **Self-hosted [Langfuse](https://github.com/langfuse) v4.49**: full trace tree, cost/cache/reasoning/TTFT, prompt versions, scores, dataset + experiment. Traces were used to find and fix a 55s → 11s slowdown (parallel MCP + negative cache, skipping reflection for simple questions, Memory timeouts).
- Per-feature details, platform bugs encountered and open items: `skills/agentbase-build/references/platform-coverage.md`.

## To verify on a real environment

- How the AgentBase Runtime endpoint authenticates callers (affects the Direct vs BFF choice, see `agentbase-build-auth`).
- HITL interrupt/resume with real `AgentBaseMemoryEvents` (has run with `InMemorySaver`).
- Traces to a real Langfuse server (structure verified via the OTel exporter).
