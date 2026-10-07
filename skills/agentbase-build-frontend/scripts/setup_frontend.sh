#!/usr/bin/env bash
# Creates an Expo React Native app (TypeScript) and overlays the standard AgentBase agent code.
# Always needs network access (npx create-expo-app), even with --no-install.
#   bash setup_frontend.sh <project-name> <frontend-dir> [--no-install] [--force]
# Existing app (<frontend-dir>/package.json present): only the template files that are MISSING are added — your
# App.tsx / src/… are never overwritten. --force overwrites them with the template (review the diff afterwards).
set -euo pipefail

SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TPL="$SKILL_DIR/assets/frontend"
NAME="${1:?project-name}"; DIR="${2:?frontend-dir}"; INSTALL=1; FORCE=0
for a in "${@:3}"; do
  case "$a" in
    --no-install) INSTALL=0 ;;
    --force) FORCE=1 ;;
    *) echo "Unknown flag: $a" >&2; exit 2 ;;
  esac
done

if [[ -e "$DIR/package.json" ]]; then
  if [[ $FORCE -eq 1 ]]; then
    echo "$DIR already has package.json — --force: overwriting it with the template files." >&2
  else
    echo "$DIR already has package.json — adding only the missing template files (existing files are kept; --force overwrites)." >&2
  fi
else
  command -v npx >/dev/null || { echo "Node.js >= 20 (npx) is required" >&2; exit 1; }
  npx --yes create-expo-app@latest "$DIR" --template blank-typescript --no-install
  FORCE=1 # fresh app: the template replaces create-expo-app's App.tsx
fi

# Copy file by file (portable no-clobber: `cp -n` exit codes differ between BSD and GNU coreutils >= 9.2)
COPIED=()
while IFS= read -r -d '' f; do
  f="${f#./}"
  if [[ -e "$DIR/$f" && $FORCE -eq 0 ]]; then
    echo "  kept existing $f" >&2
    continue
  fi
  mkdir -p "$(dirname "$DIR/$f")"
  cp "$TPL/$f" "$DIR/$f"
  COPIED+=("$f")
done < <(cd "$TPL" && find . -type f -print0)

# Replace the project-name placeholder in the files copied above only (scaffold.sh does it too; this covers standalone use)
for f in ${COPIED[@]+"${COPIED[@]}"}; do
  sed -i.bak "s/__PROJECT_NAME__/$NAME/g" "$DIR/$f" && rm -f "$DIR/$f.bak"
done
[[ -f "$DIR/.env" ]] || cp "$DIR/.env.example" "$DIR/.env"

if [[ $INSTALL -eq 0 ]]; then
  echo "⚠ --no-install: required packages are NOT installed yet. Run when online: make fe-setup" >&2
  echo "  (= cd $DIR && npm install && npx expo install expo-auth-session expo-web-browser expo-crypto expo-secure-store expo-constants)" >&2
fi
if [[ $INSTALL -eq 1 ]]; then
  (cd "$DIR" && npm install && npx expo install \
    expo-auth-session expo-web-browser expo-crypto expo-secure-store expo-constants)
fi

# Scheme for the OIDC redirect (app.json) — kept if already set
if command -v node >/dev/null && [[ -f "$DIR/app.json" ]]; then
  node -e '
    const fs=require("fs"); const p=process.argv[1]; const j=JSON.parse(fs.readFileSync(p));
    j.expo.scheme = j.expo.scheme || process.argv[2]; fs.writeFileSync(p, JSON.stringify(j,null,2));
  ' "$DIR/app.json" "$NAME"
fi
echo "✔ Frontend at $DIR — edit $DIR/.env then 'npx expo start'"
