#!/usr/bin/env bash
# Scaffold a standard AgentBase agent project by combining the assets of the agentbase-build-* skills.
#
#   bash scaffold.sh <project-name> <target-dir> [--with-frontend] [--no-sync]
#
# Each skill owns part of the code (assets/backend/... mirrors the exact paths in src/backend):
#   agentbase-build-scaffold : pyproject, main.py, config, service, graph, Dockerfile, tests
#   agentbase-build-llm      : app/llm
#   agentbase-build-memory   : app/memory (short-term, long-term, compression)
#   agentbase-build-tracing  : app/observability, app/prompts/__init__.py (Langfuse v4)
#   agentbase-build-mcp      : app/tools, mcp_servers.json, tests/test_mcp.py
#   agentbase-build-auth     : app/auth, tests/test_auth.py
#   agentbase-build-identity : app/identity.py (outbound credentials: Static/Delegated API key, OAuth2 M2M/3LO)
#   agentbase-build-hitl     : app/hitl.py, tests/test_hitl.py
#   agentbase-build-eval     : app/reflection.py, evals/, tests/test_reflection.py, tests/test_eval.py
#   agentbase-build-a2a      : app/a2a (A2A server + client tools), a2a_agents.json, tests/test_a2a.py
#   agentbase-build-frontend : src/frontend (Expo React Native) — only with --with-frontend
set -euo pipefail

SKILLS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BACKEND_SKILLS=(agentbase-build-scaffold agentbase-build-llm agentbase-build-memory agentbase-build-tracing
  agentbase-build-mcp agentbase-build-auth agentbase-build-identity agentbase-build-hitl agentbase-build-eval agentbase-build-a2a)

USAGE="Usage: scaffold.sh <project-name> <target-dir> [--with-frontend] [--no-sync]"
if [[ $# -lt 2 ]]; then
  echo "$USAGE" >&2; exit 2
fi
NAME="$1"; TARGET="$2"
shift 2
WITH_FE=0; SYNC=1
for a in "$@"; do
  case "$a" in
    --with-frontend) WITH_FE=1 ;;
    --no-sync) SYNC=0 ;;
    *) echo "Unknown flag: $a" >&2; exit 2 ;;
  esac
done

if [[ -z "$NAME" || -z "$TARGET" ]]; then
  echo "$USAGE" >&2; exit 2
fi
if [[ ! "$NAME" =~ ^[a-z][a-z0-9-]{2,39}$ ]]; then
  echo "project-name must match ^[a-z][a-z0-9-]{2,39}$ (lowercase, hyphen)" >&2; exit 2
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
copy_new "$ROOT_TPL/gitignore" "$TARGET/.gitignore"
copy_new "$ROOT_TPL/agentbase-state.json" "$TARGET/.agentbase-state.json"
copy_new "$ROOT_TPL/ci.yml" "$TARGET/.github/workflows/ci.yml"
copy_new "$ROOT_TPL/agent.yaml.tpl" "$TARGET/deploy/agent.yaml.tpl"
copy_new "$ROOT_TPL/render_runtime_spec.py" "$TARGET/deploy/render_runtime_spec.py"
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

# Replace the placeholder — only in files created above that contain it (untouched files keep inode & mtime)
for f in ${CREATED[@]+"${CREATED[@]}"}; do
  if grep -q "__PROJECT_NAME__" "$f" 2>/dev/null; then
    sed -i.bak "s/__PROJECT_NAME__/$NAME/g" "$f" && rm -f "$f.bak"
  fi
done

[[ -f "$TARGET/src/backend/.env" ]] || cp "$TARGET/src/backend/.env.example" "$TARGET/src/backend/.env"

if [[ $SYNC -eq 1 ]]; then
  (cd "$TARGET/src/backend" && uv sync && uv run pytest -q -p no:warnings)
fi

echo "✔ Scaffolded '$NAME' at $TARGET"
if [[ $SYNC -eq 0 ]]; then
  echo "  ⚠ --no-sync: no uv.lock yet — run 'cd src/backend && uv lock && uv sync' before make test / docker-build."
fi
echo "  Next: YOU fill in src/backend/.env (LLM_API_KEY, LLM_MODEL…) and src/backend/.greennode.json"
echo "        (copy .greennode.json.example) — never paste secrets into a chat — then: make check-creds → make dev"
