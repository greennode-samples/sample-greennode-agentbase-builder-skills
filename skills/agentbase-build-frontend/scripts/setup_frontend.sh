#!/usr/bin/env bash
# Creates an Expo React Native app (TypeScript) and overlays the standard AgentBase agent code.
# Always needs network access (npx create-expo-app), even with --no-install.
#   bash setup_frontend.sh <project-name> <frontend-dir> [--no-install]
set -euo pipefail

SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NAME="${1:?project-name}"; DIR="${2:?frontend-dir}"; INSTALL=1
[[ "${3:-}" == "--no-install" ]] && INSTALL=0

if [[ -e "$DIR/package.json" ]]; then
  echo "$DIR already has package.json — only overlaying src/ if missing." >&2
else
  command -v npx >/dev/null || { echo "Node.js >= 20 (npx) is required" >&2; exit 1; }
  npx --yes create-expo-app@latest "$DIR" --template blank-typescript --no-install
fi

cp -R "$SKILL_DIR/assets/frontend/." "$DIR/"
[[ -f "$DIR/.env" ]] || cp "$DIR/.env.example" "$DIR/.env"

if [[ $INSTALL -eq 0 ]]; then
  echo "⚠ --no-install: required packages are NOT installed yet. Run when online: make fe-setup" >&2
  echo "  (= cd $DIR && npm install && npx expo install expo-auth-session expo-web-browser expo-crypto expo-secure-store expo-constants)" >&2
fi
if [[ $INSTALL -eq 1 ]]; then
  (cd "$DIR" && npm install && npx expo install \
    expo-auth-session expo-web-browser expo-crypto expo-secure-store expo-constants)
fi

# Scheme cho OIDC redirect (app.json)
if command -v node >/dev/null && [[ -f "$DIR/app.json" ]]; then
  node -e '
    const fs=require("fs"); const p=process.argv[1]; const j=JSON.parse(fs.readFileSync(p));
    j.expo.scheme = j.expo.scheme || process.argv[2]; fs.writeFileSync(p, JSON.stringify(j,null,2));
  ' "$DIR/app.json" "$NAME"
fi
echo "✔ Frontend at $DIR — edit $DIR/.env then 'npx expo start'"
