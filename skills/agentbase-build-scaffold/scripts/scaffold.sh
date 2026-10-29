#!/usr/bin/env bash
# Scaffold a standard AgentBase agent project by combining the assets of the agentbase-build-* skills.
#
#   bash scaffold.sh <project-name> <target-dir> [--with-frontend] [--no-sync]
#
# Each skill owns part of the code (assets/backend/... mirrors the exact paths in src/backend). ALL backend skills
# are required: the modules import each other (tools → a2a, graph → reflection/hitl, service → mcp/hitl…); optional
# components are switched off via env, not removed. Only agentbase-build-identity (app/identity.py + its test) may
# be dropped when no tool uses outbound credentials.
#   agentbase-build-scaffold : pyproject, main.py, config, service, graph, Dockerfile, tests
#   agentbase-build-llm      : app/llm
#   agentbase-build-memory   : app/memory (short-term, long-term, compression)
#   agentbase-build-tracing  : app/observability, app/prompts/__init__.py (Langfuse v4)
#   agentbase-build-mcp      : app/tools, app/knowledge, mcp_servers.json, tests/test_mcp.py
#   agentbase-build-auth     : app/auth, tests/test_auth.py
#   agentbase-build-identity : app/identity.py (outbound credentials: Static/Delegated API key, OAuth2 M2M/3LO)
#   agentbase-build-hitl     : app/hitl.py, tests/test_hitl.py
#   agentbase-build-eval     : app/reflection.py, evals/, tests/test_reflection.py, tests/test_eval.py
#   agentbase-build-a2a      : app/a2a (A2A server + client tools), a2a_agents.json, tests/test_a2a.py
#   agentbase-build-security-audit : scripts/security_scan.py (make security-scan), tests/test_security_scan.py
#   agentbase-build-frontend : src/frontend (Expo React Native) — only with --with-frontend
#   agentbase-build-web      : src/web (Next.js web chat + BFF) — only with --with-web
# Root files (agentbase-build-scaffold/assets/root): Makefile, README.md, AGENTS.md + CLAUDE.md, .gitignore, .agentbase-state.json,
#   .github/workflows/ci.yml, deploy/{agent.yaml.tpl, render_runtime_spec.py, env_file.py} and
#   src/backend/.env.{dev,staging,prod}.example (from env.deploy.example — committed non-secret config per environment).
# .agentbase-template.json records what the template wrote; scripts/upgrade.py upgrades a project to newer skills.
set -eEuo pipefail # -E: the ERR trap below also fires inside functions

STANDARD_VERSION="0.5.0" # the agentbase-build standard (CHANGELOG.md, plugin manifests) — bump together
SKILLS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BACKEND_SKILLS=(agentbase-build-scaffold agentbase-build-llm agentbase-build-memory agentbase-build-tracing
  agentbase-build-mcp agentbase-build-auth agentbase-build-identity agentbase-build-hitl agentbase-build-eval agentbase-build-a2a
  agentbase-build-security-audit)
DEPLOY_ENVS=(dev staging prod)

USAGE="Usage: scaffold.sh <project-name> <target-dir> [--with-frontend] [--with-web] [--no-sync]"
if [[ $# -lt 2 ]]; then
  echo "$USAGE" >&2; exit 2
fi
NAME="$1"; TARGET="$2"
shift 2
WITH_FE=0; WITH_WEB=0; SYNC=1
for a in "$@"; do
  case "$a" in
    --with-frontend) WITH_FE=1 ;;
    --with-web) WITH_WEB=1 ;;
    --no-sync) SYNC=0 ;;
    *) echo "Unknown flag: $a" >&2; exit 2 ;;
  esac
done

if [[ -z "$NAME" || -z "$TARGET" ]]; then
  echo "$USAGE" >&2; exit 2
fi
# 3–40 chars, lowercase/digits/hyphen, starts with a letter, does not end with '-' (valid docker repo, Langfuse tag)
if [[ ! "$NAME" =~ ^[a-z][a-z0-9-]{1,38}[a-z0-9]$ ]]; then
  echo "project-name must match ^[a-z][a-z0-9-]{1,38}[a-z0-9]$ (3–40 chars: lowercase, digits, '-'; not ending in '-')" >&2; exit 2
fi
if [[ -e "$TARGET/src/backend" ]]; then
  echo "$TARGET/src/backend already exists — stopping to avoid overwriting." >&2; exit 1
fi
for s in "${BACKEND_SKILLS[@]}"; do
  [[ -d "$SKILLS_DIR/$s/assets/backend" ]] || { echo "Missing skill $s in $SKILLS_DIR" >&2; exit 1; }
done
# Check prerequisites BEFORE writing anything (a failure later would leave a half-built project that a re-run refuses)
if [[ $SYNC -eq 1 ]]; then
  command -v uv >/dev/null || { echo "uv is required: https://docs.astral.sh/uv/ (or pass --no-sync)" >&2; exit 1; }
fi
if [[ $WITH_FE -eq 1 && ! -f "$TARGET/src/frontend/package.json" ]]; then
  command -v npx >/dev/null || { echo "--with-frontend needs Node.js (npx) — install it or drop the flag" >&2; exit 1; }
fi
if [[ $WITH_WEB -eq 1 && ! -f "$TARGET/src/web/package.json" ]]; then
  command -v npx >/dev/null || { echo "--with-web needs Node.js (npx) — install it or drop the flag" >&2; exit 1; }
fi

# Which revision of the skills built this project (recorded in .agentbase-state.json for later reviews/upgrades)
# (git SHA only when the skills are a checkout of their own repo — not when copied into another project's git tree)
SCAFFOLD_VERSION="$STANDARD_VERSION"
REPO_DIR="$(cd "$SKILLS_DIR/.." && pwd -P)"
if [[ "$(git -C "$REPO_DIR" rev-parse --show-toplevel 2>/dev/null)" == "$REPO_DIR" ]] \
  && sha=$(git -C "$REPO_DIR" rev-parse --short HEAD 2>/dev/null); then
  SCAFFOLD_VERSION="$STANDARD_VERSION+git.$sha"
  [[ -z "$(git -C "$REPO_DIR" status --porcelain -- skills 2>/dev/null)" ]] || SCAFFOLD_VERSION="$SCAFFOLD_VERSION.dirty"
fi

# Tell the user how to recover if anything below fails (set -e stops at the first error)
PHASE=files
on_error() {
  local code=$?
  [[ $BASH_SUBSHELL -eq 0 ]] || return 0 # report once, from the main shell
  if [[ $PHASE == files ]]; then
    echo "✖ Scaffold stopped while writing files (exit $code) — $TARGET is incomplete. Fix the error above, remove" >&2
    echo "  $TARGET/src/backend (and any root files listed above as created), then re-run the same command." >&2
  else
    echo "✖ The project files are complete, but '$PHASE' failed (exit $code). Fix the error above, then run:" >&2
    echo "    cd \"$TARGET/src/backend\" && uv sync && uv run pytest" >&2
  fi
}
trap on_error ERR

# Files THIS run wrote — the only ones the placeholder pass may touch (never the user's existing files)
CREATED=()
copy_new() {  # copy_new <template> <destination>: never overwrite; say so when an existing file is kept
  if [[ -e "$2" ]]; then
    echo "⚠ $2 already exists — kept it (compare with the template $1)." >&2
    return 0
  fi
  mkdir -p "$(dirname "$2")"
  cp "$1" "$2"
  CREATED+=("$2")
}

mkdir -p "$TARGET/src/backend"
for s in "${BACKEND_SKILLS[@]}"; do
  cp -R "$SKILLS_DIR/$s/assets/backend/." "$TARGET/src/backend/"
done
while IFS= read -r -d '' f; do CREATED+=("$f"); done < <(find "$TARGET/src/backend" -type f -print0)

ROOT_TPL="$SKILLS_DIR/agentbase-build-scaffold/assets/root"
if [[ -f "$TARGET/Makefile" ]] && ! cmp -s "$ROOT_TPL/Makefile" "$TARGET/Makefile"; then
  copy_new "$ROOT_TPL/Makefile" "$TARGET/Makefile.agentbase"
  echo "⚠ $TARGET/Makefile already exists — kept it; the standard targets are in Makefile.agentbase (merge them)." >&2
elif [[ ! -f "$TARGET/Makefile" ]]; then
  cp "$ROOT_TPL/Makefile" "$TARGET/Makefile"
  CREATED+=("$TARGET/Makefile")
fi
copy_new "$ROOT_TPL/README.md" "$TARGET/README.md"
copy_new "$ROOT_TPL/AGENTS.md" "$TARGET/AGENTS.md"
copy_new "$ROOT_TPL/CLAUDE.md" "$TARGET/CLAUDE.md"
copy_new "$ROOT_TPL/gitignore" "$TARGET/.gitignore"
copy_new "$ROOT_TPL/agentbase-state.json" "$TARGET/.agentbase-state.json"
copy_new "$ROOT_TPL/ci.yml" "$TARGET/.github/workflows/ci.yml"
copy_new "$ROOT_TPL/agent.yaml.tpl" "$TARGET/deploy/agent.yaml.tpl"
copy_new "$ROOT_TPL/render_runtime_spec.py" "$TARGET/deploy/render_runtime_spec.py"
copy_new "$ROOT_TPL/env_file.py" "$TARGET/deploy/env_file.py"
for env in "${DEPLOY_ENVS[@]}"; do
  dst="$TARGET/src/backend/.env.$env.example"
  sed "s/__APP_ENV__/$env/g" "$ROOT_TPL/env.deploy.example" > "$dst"  # src/backend is new: nothing to overwrite
  CREATED+=("$dst")
done
for secret_file in .env .greennode.json deploy/agent.yaml; do
  if ! grep -qxF "$secret_file" "$TARGET/.gitignore" && ! grep -qxF "/$secret_file" "$TARGET/.gitignore"; then
    echo "⚠ $TARGET/.gitignore does not ignore '$secret_file' — add it (it holds secrets)." >&2
  fi
done

if [[ $WITH_FE -eq 1 ]]; then
  # setup_frontend.sh replaces the placeholder in the files it overlays itself
  bash "$SKILLS_DIR/agentbase-build-frontend/scripts/setup_frontend.sh" "$NAME" "$TARGET/src/frontend" \
    $([[ $SYNC -eq 0 ]] && echo --no-install)
fi
if [[ $WITH_WEB -eq 1 ]]; then
  # setup_web.sh replaces the placeholder itself; src/web is not upgraded by upgrade.py (see template_files.py)
  bash "$SKILLS_DIR/agentbase-build-web/scripts/setup_web.sh" "$NAME" "$TARGET/src/web" \
    $([[ $SYNC -eq 0 ]] && echo --no-install)
fi

# Replace the placeholders — only in files created above that contain them (untouched files keep inode & mtime)
for f in ${CREATED[@]+"${CREATED[@]}"}; do
  if grep -q "__PROJECT_NAME__\|__STANDARD_VERSION__\|__SCAFFOLD_VERSION__" "$f" 2>/dev/null; then
    sed -i.bak -e "s/__PROJECT_NAME__/$NAME/g" -e "s/__STANDARD_VERSION__/$STANDARD_VERSION/g" \
      -e "s/__SCAFFOLD_VERSION__/$SCAFFOLD_VERSION/g" "$f" && rm -f "$f.bak"
  fi
done

# What the template wrote (ownership class + SHA-256 per file) → .agentbase-template.json, so upgrade.py can tell your
# edits from the template's later. Optional: without it, upgrades rebuild the base from the skills' git history.
if command -v python3 >/dev/null 2>&1; then
  python3 "$SKILLS_DIR/agentbase-build-scaffold/scripts/template_files.py" record "$TARGET" "$NAME" "$STANDARD_VERSION" \
    "$SCAFFOLD_VERSION" "$WITH_FE" ${CREATED[@]+"${CREATED[@]}"} \
    || echo "⚠ $TARGET/.agentbase-template.json not written — upgrades will need the skills' git history." >&2
else
  echo "⚠ python3 not found: no $TARGET/.agentbase-template.json — upgrades will need the skills' git history." >&2
fi

[[ -f "$TARGET/src/backend/.env" ]] || cp "$TARGET/src/backend/.env.example" "$TARGET/src/backend/.env"

if [[ $SYNC -eq 1 ]]; then
  cd "$TARGET/src/backend" # no subshell: bash 3.2 (macOS) does not run the ERR trap for a failed ( … )
  PHASE="uv sync"
  uv sync
  PHASE="uv run pytest"
  uv run pytest -q -p no:warnings
  cd - >/dev/null
fi
trap - ERR

echo "✔ Scaffolded '$NAME' at $TARGET (standard $STANDARD_VERSION, skills $SCAFFOLD_VERSION)"
if [[ $SYNC -eq 0 ]]; then
  echo "  ⚠ --no-sync: no uv.lock yet — run 'cd src/backend && uv lock && uv sync' before make test / docker-build."
fi
echo "  Next: in YOUR own terminal (a coding agent cannot type into it), run: make set-secret — hidden prompt for"
echo "        the IAM client_id/secret (→ src/backend/.greennode.json) and LLM_API_KEY (→ src/backend/.env)."
echo "        Never paste secrets into a chat. Set LLM_MODEL in src/backend/.env, then: make check-creds → make dev"
