#!/usr/bin/env bash
# Regenerates the uv.lock files the skills ship: the exact dependency set the tests ran against, so a project
# scaffolded today and one scaffolded in six months start from the same, tested packages (and `uv sync --locked` in
# the Dockerfile and CI has a lock that matches). Run it after changing a pyproject.toml of
# agentbase-build-scaffold/assets/backend or agentbase-build-mcp-server/assets/mcp_server, then commit the locks.
#   bash relock.sh             keep the locked versions; add / drop / re-resolve only what pyproject.toml changed
#   bash relock.sh --upgrade   newest versions pyproject.toml allows — then run the full scaffold + tests before merging
# The project's own name is a placeholder in a shipped lock (__PROJECT_NAME__-backend / __SERVER_NAME__): the scaffold
# replaces it like everywhere else. Needs network access and uv.
set -euo pipefail

UPGRADE=""
case "${1:-}" in
  "") ;;
  --upgrade) UPGRADE="--upgrade" ;;
  *) echo "Usage: relock.sh [--upgrade]" >&2; exit 2 ;;
esac
command -v uv >/dev/null || { echo "uv is required (https://docs.astral.sh/uv/)" >&2; exit 1; }

SKILLS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# Backend: scaffold a throw-away project (placeholders replaced, the shipped lock copied in), lock it, ship the result
bash "$SKILLS_DIR/agentbase-build-scaffold/scripts/scaffold.sh" placeholder-agent "$TMP/agent" --no-sync >/dev/null
(cd "$TMP/agent/src/backend" && uv lock $UPGRADE)
sed 's/placeholder-agent-backend/__PROJECT_NAME__-backend/g' "$TMP/agent/src/backend/uv.lock" \
  > "$SKILLS_DIR/agentbase-build-scaffold/assets/backend/uv.lock"

# MCP server: the same, by hand (its SKILL.md copies the directory and runs sed over these files)
cp -R "$SKILLS_DIR/agentbase-build-mcp-server/assets/mcp_server" "$TMP/mcp"
(
  cd "$TMP/mcp"
  for f in pyproject.toml uv.lock; do
    [[ -f "$f" ]] && sed -i.bak 's/__SERVER_NAME__/placeholder-server/g' "$f" && rm -f "$f.bak"
  done
  uv lock $UPGRADE
)
sed 's/placeholder-server/__SERVER_NAME__/g' "$TMP/mcp/uv.lock" \
  > "$SKILLS_DIR/agentbase-build-mcp-server/assets/mcp_server/uv.lock"

echo "✔ locks written: agentbase-build-scaffold/assets/backend/uv.lock, agentbase-build-mcp-server/assets/mcp_server/uv.lock"
echo "  Commit them with the pyproject.toml change; CI fails when a lock no longer matches its pyproject.toml."
