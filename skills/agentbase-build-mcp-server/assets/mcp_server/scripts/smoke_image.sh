#!/usr/bin/env bash
# Smoke-tests a BUILT image of this server (needs Docker):
#   1. fails closed: with no authentication configured the container must refuse to start;
#   2. with an API key it serves GET /health and answers 401 to POST /mcp without a token.
#   bash scripts/smoke_image.sh <image:tag> [host-port]        # e.g. `make docker-build docker-smoke`
# CI runs it too (.github/workflows/ci.yml), so the check you run here is the one that gates a merge.
set -euo pipefail

IMAGE="${1:?image, e.g. my-server:dev}"
PORT="${2:-18080}"
NAME="smoke-$$"
cleanup() { docker rm -f "$NAME" >/dev/null 2>&1 || true; }
trap cleanup EXIT

running() { [ "$(docker inspect -f '{{.State.Running}}' "$NAME")" = true ]; }

# MCP_ALLOW_SQLITE: the image refuses a SQLite store in prod (the Runtime filesystem is not persistent); these
# throwaway containers have no data to lose, and the refusal must not hide what is tested here
docker run -d --name "$NAME" -e MCP_ALLOW_SQLITE=true "$IMAGE" >/dev/null
for _ in $(seq 1 15); do running || break; sleep 1; done
if running; then
  echo "✖ the image kept running with no authentication configured — it must refuse to start" >&2
  exit 1
fi
echo "✔ refuses to start without authentication (exit $(docker inspect -f '{{.State.ExitCode}}' "$NAME"))"
cleanup

hash=$(printf 'smoke-key' | { sha256sum 2>/dev/null || shasum -a 256; } | cut -d' ' -f1)
docker run -d --name "$NAME" -p "127.0.0.1:$PORT:8080" -e MCP_ALLOW_SQLITE=true \
  -e MCP_AUTH_MODE=api_key -e "MCP_API_KEY_SHA256=[\"$hash\"]" "$IMAGE" >/dev/null
ok=0
for _ in $(seq 1 30); do
  if curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then ok=1; break; fi
  sleep 1
done
if [ "$ok" != 1 ]; then
  docker logs "$NAME" >&2 || true
  echo "✖ GET /health did not answer" >&2
  exit 1
fi
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "http://127.0.0.1:$PORT/mcp" \
  -H 'content-type: application/json' -d '{}')
if [ "$code" != 401 ]; then
  echo "✖ POST /mcp without a token answered $code, expected 401" >&2
  exit 1
fi
echo "✔ serves /health and answers 401 without a token"
