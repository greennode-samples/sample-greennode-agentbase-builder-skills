"""Justified exceptions for check_drift.py (standard library only). Every entry carries its reason.

Add an entry only when the "drift" is intended — fix the doc or the template otherwise. Keys are exact names
unless the dict name says *_PATTERNS (fnmatch globs).
"""

from __future__ import annotations

# --- Environment variables NOT declared in a pydantic Settings class but legitimately set or cited -------------------
# (Settings fields, names read with os.getenv/os.environ in skill code, ${VAR} references in the JSON config templates,
# env-file keys and CI names are discovered automatically — only list what none of those covers.)
EXTERNAL_ENV: dict[str, str] = {
    "LANGFUSE_SAMPLE_RATE": "read by the Langfuse SDK itself (Langfuse() constructor default)",
    "LANGFUSE_DEBUG": "read by the Langfuse SDK itself (troubleshooting, tracing SKILL.md)",
    "LANGFUSE_MEDIA_UPLOAD_ENABLED": "read by the Langfuse SDK itself (masking notes in trace-schema.md)",
    "NEXT_TELEMETRY_DISABLED": "read by Next.js itself: no anonymous telemetry from the web app's image (its Dockerfile)",
}
EXTERNAL_ENV_PATTERNS: dict[str, str] = {
    "LANGFUSE_INIT_*": "headless init of a self-hosted Langfuse server (docker-compose), tracing SKILL.md Step 1b",
}

# --- Keys that must never be in a deploy env file (env.deploy.example ⇒ .env.<env>.example) --------------------------
DEPLOY_FORBIDDEN: dict[str, str] = {
    "GREENNODE_*": "injected by the AgentBase Runtime (client id/secret, agent identity) or reserved (endpoint URL)",
    "AGENT_VERSION": "baked into the image by `make docker-build` / CI (= image tag = Langfuse release)",
}
# Keys that must never be ACTIVE in the local .env.example: the local IAM pair lives in .greennode.json
LOCAL_ENV_FORBIDDEN: dict[str, str] = {
    "GREENNODE_*": "local dev reads the IAM pair from .greennode.json only (normalize_iam_env); mixing sources ⇒ 401",
}

# --- UPPER_SNAKE tokens that look like config variables but are not -------------------------------------------------
NON_ENV_TOKENS: dict[str, str] = {
    "A2A_OFF_API_KEY": "fake ${VAR} of a DISABLED a2a_agents.json entry in tests/test_set_secret.py (must not be offered)",
    "MCP_ERROR": "prefix of the tool result the agent gets when an MCP tool fails upstream (app/tools/mcp.py)",
    "MCP_SCOPE_ERROR": "prefix of the tool result for an insufficient_scope refusal (app/tools/mcp.py)",
}

# --- Trace names ---------------------------------------------------------------------------------------------------
# Emitted names that need not appear in the tracing SKILL.md tree (normalized: placeholders written as <*>).
TRACE_NOT_IN_TREE: dict[str, str] = {}
# (file path relative to the repo root, the name expression as written) → reason, for a trace name that cannot be a
# literal (e.g. a helper wrapping tracing.step(name)) — the names it receives must then be documented by hand.
TRACE_DYNAMIC_NAMES: dict[tuple[str, str], str] = {}

# --- `make <target>` mentions -------------------------------------------------------------------------------------
# (file path relative to the repo root, target) → reason, for a mention that is right although the Makefile of its
# context does not define the target.
MAKE_TARGET_EXCEPTIONS: dict[tuple[str, str], str] = {
    ("skills/agentbase-build-mcp-server/assets/mcp_server/scripts/set_secret.py", "check-env"): (
        "identical copy of the agent's scripts/set_secret.py (skills-ci checks it); next_step() prints this only when the agent project's file exists (deploy/env_file.py, scripts/check_creds.py), never in the MCP server"
    ),
    ("skills/agentbase-build-mcp-server/assets/mcp_server/scripts/set_secret.py", "check-creds"): (
        "identical copy of the agent's scripts/set_secret.py (skills-ci checks it); next_step() prints this only when the agent project's file exists (deploy/env_file.py, scripts/check_creds.py), never in the MCP server"
    ),
}

# --- Template paths cited in docs ----------------------------------------------------------------------------------
# Paths no skill ships: created by the user, at runtime or by a tool — or named only to say they are not used.
# Matched against the path as written and, for src/backend/… and src/frontend/…, against the part after it.
NON_TEMPLATE_PATHS: dict[str, str] = {
    ".env": "created from .env.example by scaffold.sh / the user (git-ignored)",
    ".greennode.json": "created by the user from .greennode.json.example (git-ignored)",
    "deploy/agent.yaml": "rendered from deploy/agent.yaml.tpl in CI (temp dir, never committed)",
    "deploy/runtime.yaml": "rendered by deploy/render_runtime_spec.py in CI (temp dir, never committed)",
    "src/app": "Expo Router's directory, named in the frontend SKILL only to say this template does not use it",
}
NON_TEMPLATE_PATH_PATTERNS: dict[str, str] = {
    ".env.*": "per-environment files rendered from env.deploy.example by scaffold.sh, or the user's filled copies",
}

# --- Fenced code blocks that copy CI workflow steps -------------------------------------------------------------------
# (file path relative to the repo root, first line number of the block) → reason
CI_SNIPPET_EXCEPTIONS: dict[tuple[str, int], str] = {}
