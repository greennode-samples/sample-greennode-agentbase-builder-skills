#!/usr/bin/env bash
# Creates a Next.js web app (App Router, TypeScript, src/ dir) and overlays the standard AgentBase web chat + BFF code.
# Creating the app always needs network access (create-next-app downloads its template), even with --no-install.
#   bash setup_web.sh <project-name> <web-dir> [--no-install] [--force]
# Existing app (<web-dir>/package.json present): only the template files that are MISSING are added — your
# src/… files are never overwritten. --force overwrites them with the template (review the diff afterwards).
#
# Environment overrides (defaults = the combination this skill is tested with, see SKILL.md):
#   CREATE_NEXT_APP_VERSION=16.4.3   create-next-app version = the Next.js version it pins in package.json
#   VITEST_VERSION=4.1.12            unit test runner (devDependency, exact version)
set -euo pipefail

TESTED_NEXT=16.4.3
CREATE_NEXT_APP_VERSION="${CREATE_NEXT_APP_VERSION:-$TESTED_NEXT}"
VITEST_VERSION="${VITEST_VERSION:-4.1.12}"

SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TPL="$SKILL_DIR/assets/web"
NAME="${1:?project-name}"; DIR="${2:?web-dir}"; INSTALL=1; FORCE=0
for a in "${@:3}"; do
  case "$a" in
    --no-install) INSTALL=0 ;;
    --force) FORCE=1 ;;
    *) echo "Unknown flag: $a" >&2; exit 2 ;;
  esac
done

# NAME goes into sed and package.json
if [[ ! "$NAME" =~ ^[a-z][a-z0-9-]{1,38}[a-z0-9]$ ]]; then
  echo "project-name must match ^[a-z][a-z0-9-]{1,38}[a-z0-9]\$ (lowercase letters, digits, '-')" >&2; exit 2
fi
[[ "$CREATE_NEXT_APP_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] \
  || { echo "CREATE_NEXT_APP_VERSION must be an exact version x.y.z (got '$CREATE_NEXT_APP_VERSION')" >&2; exit 2; }
[[ "$VITEST_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] \
  || { echo "VITEST_VERSION must be an exact version x.y.z (got '$VITEST_VERSION')" >&2; exit 2; }

check_node() {
  command -v node >/dev/null && command -v npx >/dev/null || { echo "Node.js (with npx) is required" >&2; exit 1; }
  # Next.js 16 engines: node >= 20.9.0
  node -e '
    const [M, m] = process.versions.node.split(".").map(Number);
    process.exit(M > 20 || (M === 20 && m >= 9) ? 0 : 1);
  ' || { echo "Node $(node -v) is not supported by Next.js 16 — use >= 20.9 (LTS 22 or 24 recommended)" >&2; exit 1; }
}

# Present while a freshly created app has not received the template yet: a re-run after a failure then applies the
# template again instead of treating create-next-app's own page.tsx as "your existing app".
PENDING="$DIR/.agentbase-setup-pending"
FRESH=0
if [[ -e "$DIR/package.json" && -e "$PENDING" ]]; then
  if [[ ! -f "$DIR/tsconfig.json" ]]; then
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
else
  check_node
  # CI=1 + every option given + stdin closed: no prompt can hang a non-interactive run. --disable-git: no nested repo
  # (the project's root repo would store src/web as a gitlink). --no-agents-md / --no-agent-feedback: no coding-agent
  # files or telemetry prompts. --empty: no demo page. No Tailwind / ESLint: the template ships plain CSS + tsc.
  rc=0
  CI=1 npx --yes "create-next-app@$CREATE_NEXT_APP_VERSION" "$DIR" --ts --app --src-dir --empty \
    --no-tailwind --no-eslint --no-biome --no-react-compiler --import-alias "@/*" \
    --use-npm --skip-install --disable-git --no-agents-md --no-agent-feedback --yes </dev/null || rc=$?
  if [[ $rc -ne 0 ]]; then
    if [[ -e "$DIR/package.json" ]]; then : > "$PENDING"; fi
    echo "✖ create-next-app failed (exit $rc) — fix the cause (network?) and run this script again." >&2
    exit "$rc"
  fi
  : > "$PENDING"
  FRESH=1
  FORCE=1 # fresh app: the template replaces create-next-app's page, layout and next.config.ts
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

# Replace the project-name placeholder in the files copied above only
for f in ${COPIED[@]+"${COPIED[@]}"}; do
  sed -i.bak "s/__PROJECT_NAME__/$NAME/g" "$DIR/$f" && rm -f "$DIR/$f.bak"
done
# Next.js reads .env.local (git-ignored by create-next-app's `.env*` rule); .env.example must stay committable
[[ -f "$DIR/.env.local" ]] || cp "$DIR/.env.example" "$DIR/.env.local"
if [[ -f "$DIR/.gitignore" ]] && ! grep -qxF '!.env.example' "$DIR/.gitignore"; then
  printf '\n# AgentBase: the documented, secret-free template is committed\n!.env.example\n' >> "$DIR/.gitignore"
fi

# package.json: name, test / typecheck scripts (values already set in an existing app are kept)
command -v node >/dev/null || { echo "Node.js is required to configure package.json" >&2; exit 1; }
node -e '
  const fs = require("fs");
  const [p, name, fresh] = process.argv.slice(1);
  const j = JSON.parse(fs.readFileSync(p, "utf8"));
  if (fresh === "1" || !j.name) j.name = name;
  j.scripts = j.scripts || {};
  j.scripts.test = j.scripts.test || "vitest run";
  j.scripts.typecheck = j.scripts.typecheck || "tsc --noEmit";
  fs.writeFileSync(p, JSON.stringify(j, null, 2) + "\n");
' "$DIR/package.json" "$NAME" "$FRESH"
rm -f "$PENDING" # the template is in place — from now on a re-run treats the app as existing

if [[ $INSTALL -eq 0 ]]; then
  echo "⚠ --no-install: packages are NOT installed yet. Run when online:" >&2
  echo "  cd $DIR && npm install && npm install --save-dev --save-exact vitest@$VITEST_VERSION" >&2
else
  check_node
  (cd "$DIR" && npm install --no-fund --no-audit && npm install --no-fund --no-audit --save-dev --save-exact "vitest@$VITEST_VERSION")
fi
echo "✔ Web app at $DIR (create-next-app $CREATE_NEXT_APP_VERSION) — edit $DIR/.env.local then 'npm run dev'"
