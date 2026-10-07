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
| `/agentbase-build-tracing` | **Langfuse v4**: detailed per-step trace tree, model/usage/cost/cache/TTFT, masking, Prompt Management, feedback |
| `/agentbase-build-mcp` | Local tools + **MCP Connectors** (catalog/custom) on the **MCP Gateway** + **Policy Group** (principal `iam:<sub>`, action `connector__tool`), policy-deny guard |
| `/agentbase-build-mcp-server` | **Build an MCP server** with auth: API key / OAuth JWT (2LO, 3LO, inbound forward), scopes, user from token, Protected Resource Metadata |
| `/agentbase-build-a2a` | **A2A**: expose the agent (Agent Card + JSON-RPC) and call other agents as tools, isolate tasks/memory per user — only when decision guide §5 is satisfied |
| `/agentbase-build-auth` | Inbound JWT (OIDC/JWKS), user-spoofing prevention, outbound Identity, Direct/BFF models |
| `/agentbase-build-hitl` | **Human-in-the-loop**: `interrupt()` to approve / edit / reject tool calls, resume |
| `/agentbase-build-eval` | **Evaluation loop**: Langfuse Datasets/Experiments, LLM judge, CI gate, online scores, self-eval loop |
| `/agentbase-build-frontend` | **Expo React Native**: OIDC PKCE, SSE streaming, HITL approval cards, 👍👎 feedback |
| `/agentbase-build-deploy` | Pre-flight, per-environment env, linux/amd64 image, `/agentbase-deploy`, verify, rollback |

## Repo structure

```
.claude-plugin/              plugin.json, marketplace.json
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

## Installation

**Claude Code (plugin):**

```bash
claude plugin marketplace add greennode-samples/sample-greennode-agentbase-builder-skills
```

Then in Claude Code: `/plugin install agentbase-build@agentbase-build`.

**Codex CLI:** `codex plugin marketplace add greennode-samples/sample-greennode-agentbase-builder-skills` then `codex plugin add agentbase-build@agentbase-build`.

**Manual:** copy or symlink each directory in `skills/` into `~/.claude/skills/` (or the project's `.claude/skills/`). Installing the platform skill set `greennode-agentbase-skills` alongside is recommended.

## Verified

- **Offline**: a freshly scaffolded project passes 62 tests (agent, per-user isolation, streaming, compression, JWT/api_key, HITL, reflection, eval, tier/fallback/adaptive routing, MCP guard, A2A e2e); MCP server template passes 7 e2e tests; Python 3.13, ruff clean. Frontend: `tsc` + Android bundle (Expo SDK 57).
- **Real GreenNode** (runtime `test-agent`, v1→v6): api_key auth (401), short/long-term memory on AgentBase Memory, per-user isolation (memory, HITL, feedback, A2A tasks), HITL on real memory, MCP Connector via Gateway + Policy (ALLOW/DENY), A2A Agent Card + task isolation, real model tiers + fallback on MaaS (10 models), prompt cache.
- **Self-hosted Langfuse v4.49**: full trace tree, cost/cache/reasoning/TTFT, prompt versions, scores, dataset + experiment. Traces were used to find and fix a 55s → 11s slowdown (parallel MCP + negative cache, skipping reflection for simple questions, Memory timeouts).
- Per-feature details, platform bugs encountered and open items: `skills/agentbase-build/references/platform-coverage.md`.

## To verify on a real environment

- How the AgentBase Runtime endpoint authenticates callers (affects the Direct vs BFF choice, see `agentbase-build-auth`).
- HITL interrupt/resume with real `AgentBaseMemoryEvents` (has run with `InMemorySaver`).
- Traces to a real Langfuse server (structure verified via the OTel exporter).
