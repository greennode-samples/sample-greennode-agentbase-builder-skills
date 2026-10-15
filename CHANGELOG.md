# Changelog

Versions of the agentbase-build standard. The version is stamped in `.claude-plugin/plugin.json`,
`.claude-plugin/marketplace.json`, `.codex-plugin/plugin.json` and `skills/agentbase-build-scaffold/scripts/scaffold.sh`
(`STANDARD_VERSION`, checked by `.github/workflows/skills-ci.yml`). Every generated project records it in
`.agentbase-state.json` (`standard_version`, plus `scaffold_version` = the skills' git SHA when available), so a
review can tell which standard a project was built from.

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

## 0.2.0

Baseline before the enterprise review.
