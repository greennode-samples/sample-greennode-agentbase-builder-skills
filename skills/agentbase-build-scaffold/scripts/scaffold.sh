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
#   agentbase-build-hitl     : app/hitl.py, tests/test_hitl.py
#   agentbase-build-eval     : app/reflection.py, evals/, tests/test_reflection.py
#   agentbase-build-a2a      : app/a2a (A2A server + client tools), a2a_agents.json, tests/test_a2a.py
#   agentbase-build-frontend : src/frontend (Expo React Native) — only with --with-frontend
set -euo pipefail

SKILLS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BACKEND_SKILLS=(agentbase-build-scaffold agentbase-build-llm agentbase-build-memory agentbase-build-tracing
  agentbase-build-mcp agentbase-build-auth agentbase-build-hitl agentbase-build-eval agentbase-build-a2a)

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

mkdir -p "$TARGET/src/backend"
for s in "${BACKEND_SKILLS[@]}"; do
  cp -R "$SKILLS_DIR/$s/assets/backend/." "$TARGET/src/backend/"
done

ROOT_TPL="$SKILLS_DIR/agentbase-build-scaffold/assets/root"
if [[ -f "$TARGET/Makefile" ]] && ! cmp -s "$ROOT_TPL/Makefile" "$TARGET/Makefile"; then
  cp "$ROOT_TPL/Makefile" "$TARGET/Makefile.agentbase"
  echo "⚠ $TARGET/Makefile already exists — kept it; the standard targets are in Makefile.agentbase (merge them)." >&2
else
  cp "$ROOT_TPL/Makefile" "$TARGET/Makefile"
fi
[[ -f "$TARGET/README.md" ]] || cp "$ROOT_TPL/README.md" "$TARGET/README.md"
[[ -f "$TARGET/.gitignore" ]] || cp "$ROOT_TPL/gitignore" "$TARGET/.gitignore"
[[ -f "$TARGET/.agentbase-state.json" ]] || cp "$ROOT_TPL/agentbase-state.json" "$TARGET/.agentbase-state.json"

if [[ $WITH_FE -eq 1 ]]; then
  bash "$SKILLS_DIR/agentbase-build-frontend/scripts/setup_frontend.sh" "$NAME" "$TARGET/src/frontend" \
    $([[ $SYNC -eq 0 ]] && echo --no-install)
fi

# Thay placeholder
find "$TARGET" -type f \( -name '*.py' -o -name '*.md' -o -name '*.toml' -o -name '*.json' \
  -o -name '*.ts' -o -name '*.tsx' -o -name '*.example' -o -name 'Makefile' -o -name 'Makefile.agentbase' -o -name 'Dockerfile' \) \
  -not -path '*/node_modules/*' -not -path '*/.venv/*' -print0 |
  while IFS= read -r -d '' f; do
    sed -i.bak "s/__PROJECT_NAME__/$NAME/g" "$f" && rm -f "$f.bak"
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
