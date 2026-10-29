#!/usr/bin/env bash
# Creates an Expo React Native app (TypeScript) and overlays the standard AgentBase agent code.
# Creating the app always needs network access (create-expo downloads the template), even with --no-install.
#   bash setup_frontend.sh <project-name> <frontend-dir> [--no-install] [--force]
# Existing app (<frontend-dir>/package.json present): only the template files that are MISSING are added — your
# App.tsx / src/… are never overwritten. --force overwrites them with the template (review the diff afterwards).
#
# Environment overrides (defaults = the combination this skill is tested with, see SKILL.md):
#   EXPO_SDK=57                 template blank-typescript@sdk-<EXPO_SDK>
#   CREATE_EXPO_VERSION=5.0.1   create-expo CLI version
#   VITEST_VERSION=4.1.12       unit test runner for the shared chat logic (devDependency, exact version)
#   APP_ID=<reverse-domain id>  ios.bundleIdentifier + android.package (default com.example.<name without '-'>)
set -euo pipefail

TESTED_SDK=57
EXPO_SDK="${EXPO_SDK:-$TESTED_SDK}"
CREATE_EXPO_VERSION="${CREATE_EXPO_VERSION:-5.0.1}"
VITEST_VERSION="${VITEST_VERSION:-4.1.12}"
INSTALL_PKGS=(expo-auth-session expo-web-browser expo-crypto expo-secure-store expo-constants)

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

# NAME goes into sed, app.json (name/slug) and is the URL scheme of the OIDC redirect (<name>://auth)
if [[ ! "$NAME" =~ ^[a-z][a-z0-9-]{1,38}[a-z0-9]$ ]]; then
  echo "project-name must match ^[a-z][a-z0-9-]{1,38}[a-z0-9]\$ (lowercase letters, digits, '-')" >&2; exit 2
fi
APP_ID="${APP_ID:-com.example.${NAME//-/}}"
if [[ ! "$APP_ID" =~ ^[a-zA-Z][a-zA-Z0-9]*(\.[a-zA-Z][a-zA-Z0-9]*)+$ ]]; then
  echo "APP_ID must be a reverse-domain id valid on iOS and Android, e.g. vn.example.myagent (got '$APP_ID')" >&2; exit 2
fi
[[ "$EXPO_SDK" =~ ^[0-9]+$ ]] || { echo "EXPO_SDK must be a number (got '$EXPO_SDK')" >&2; exit 2; }
[[ "$VITEST_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "VITEST_VERSION must be an exact version x.y.z (got '$VITEST_VERSION')" >&2; exit 2; }

check_node() {
  command -v node >/dev/null && command -v npx >/dev/null || { echo "Node.js (with npx) is required" >&2; exit 1; }
  [[ "$EXPO_SDK" == "$TESTED_SDK" ]] || return 0 # other SDKs: their own requirements
  # React Native 0.86 (Expo SDK 57) engines: ^20.19.4 || ^22.13.0 || ^24.3.0 || >=25
  node -e '
    const [M, m, p] = process.versions.node.split(".").map(Number);
    process.exit((M === 20 && (m > 19 || (m === 19 && p >= 4))) || (M === 22 && m >= 13) || (M === 24 && m >= 3) || M >= 25 ? 0 : 1);
  ' || {
    echo "Node $(node -v) is not supported by Expo SDK $TESTED_SDK / React Native 0.86 — use ^20.19.4, ^22.13.0, ^24.3.0 or >= 25" >&2
    exit 1
  }
}

# Present while a freshly created app has not received the template yet: a re-run after a failure then applies the
# template again instead of treating create-expo's own App.tsx as "your existing app".
PENDING="$DIR/.agentbase-setup-pending"
FRESH=0
if [[ -e "$DIR/package.json" && -e "$PENDING" ]]; then
  if [[ ! -f "$DIR/app.json" || ! -f "$DIR/tsconfig.json" ]]; then
    echo "✖ $DIR is an incomplete app from a failed setup — delete it and run this script again." >&2; exit 1
  fi
  echo "$DIR: the previous setup did not finish — applying the template again." >&2
  FORCE=1
  FRESH=1
elif [[ -e "$DIR/package.json" ]]; then
  if [[ $FORCE -eq 1 ]]; then
    echo "$DIR already has package.json — --force: overwriting it with the template files." >&2
  else
    echo "$DIR already has package.json — adding only the missing template files (existing files are kept; --force overwrites)." >&2
  fi
  # create-expo of older runs wrote agent instructions that contradict this template (Expo Router in src/app/) and
  # enabled a third-party Claude Code plugin. They may have been edited since — never deleted here, only reported.
  if grep -qs "Expo has changed" "$DIR/AGENTS.md"; then
    echo "⚠ $DIR/AGENTS.md (+ CLAUDE.md) come from create-expo: they tell coding agents to use Expo Router in src/app/, which this template does not use — review / delete them." >&2
  fi
  if grep -qs "claude-plugins-official" "$DIR/.claude/settings.json"; then
    echo "⚠ $DIR/.claude/settings.json (from create-expo) enables a third-party Claude Code plugin — review / delete it." >&2
  fi
else
  check_node
  HAD_GIT=0
  [[ -e "$DIR/.git" ]] && HAD_GIT=1
  # CI=1: no prompts (inside an existing git repo create-expo would otherwise ASK whether to skip `git init` and
  # wait forever on a non-interactive stdin). --no-agents-md: no AGENTS.md / CLAUDE.md / .claude/settings.json.
  rc=0
  CI=1 npx --yes "create-expo@$CREATE_EXPO_VERSION" "$DIR" --template "blank-typescript@sdk-$EXPO_SDK" \
    --no-install --no-agents-md || rc=$?
  # Outside a git repo create-expo runs `git init` + commit: a nested repo would make the project's root repo store
  # src/frontend as a gitlink (the frontend never committed). Remove only the repo it just created.
  if [[ $HAD_GIT -eq 0 && -d "$DIR/.git" ]]; then
    rm -rf "$DIR/.git"
  fi
  if [[ $rc -ne 0 ]]; then
    if [[ -e "$DIR/package.json" ]]; then : > "$PENDING"; fi
    echo "✖ create-expo failed (exit $rc) — fix the cause (network?) and run this script again." >&2
    exit "$rc"
  fi
  : > "$PENDING"
  FRESH=1
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

# app.json: name/slug (a fresh app is named after the folder, "frontend"), scheme for the OIDC redirect
# (<name>://auth) and the native app ids. Values already set in an existing app are kept.
if [[ -f "$DIR/app.json" ]]; then
  command -v node >/dev/null || { echo "Node.js is required to configure app.json" >&2; exit 1; }
  node -e '
    const fs = require("fs");
    const [p, name, appId, fresh] = process.argv.slice(1);
    const j = JSON.parse(fs.readFileSync(p, "utf8"));
    const e = (j.expo = j.expo || {});
    if (fresh === "1" || !e.name) e.name = name;
    if (fresh === "1" || !e.slug) e.slug = name;
    e.scheme = e.scheme || name;
    e.ios = e.ios || {};
    e.ios.bundleIdentifier = e.ios.bundleIdentifier || appId;
    e.android = e.android || {};
    e.android.package = e.android.package || appId;
    fs.writeFileSync(p, JSON.stringify(j, null, 2) + "\n");
    console.error(`app.json: name=${e.name} scheme=${e.scheme} ios.bundleIdentifier=${e.ios.bundleIdentifier} android.package=${e.android.package}`);
  ' "$DIR/app.json" "$NAME" "$APP_ID" "$FRESH"
  if grep -q '"com\.example\.' "$DIR/app.json"; then
    echo "⚠ app.json uses a com.example.* app id — put your own reverse-domain id in ios.bundleIdentifier / android.package (or create the app with APP_ID=…) BEFORE the first store build: it cannot change after publishing." >&2
  fi
fi
# package.json: test / typecheck scripts (values already set in an existing app are kept)
node -e '
  const fs = require("fs");
  const p = process.argv[1];
  const j = JSON.parse(fs.readFileSync(p, "utf8"));
  j.scripts = j.scripts || {};
  j.scripts.test = j.scripts.test || "vitest run";
  j.scripts.typecheck = j.scripts.typecheck || "tsc --noEmit";
  fs.writeFileSync(p, JSON.stringify(j, null, 2) + "\n");
' "$DIR/package.json"
rm -f "$PENDING" # the template is in place — from now on a re-run treats the app as existing

if [[ $INSTALL -eq 0 ]]; then
  echo "⚠ --no-install: required packages are NOT installed yet. Run when online: make fe-setup" >&2
  echo "  (= cd $DIR && npm install && npx expo install ${INSTALL_PKGS[*]} && npm install --save-dev --save-exact vitest@$VITEST_VERSION)" >&2
else
  check_node
  (cd "$DIR" && npm install && npx expo install "${INSTALL_PKGS[@]}" && npm install --save-dev --save-exact "vitest@$VITEST_VERSION")
fi
echo "✔ Frontend at $DIR (Expo SDK $EXPO_SDK) — edit $DIR/.env then 'npx expo start'"
