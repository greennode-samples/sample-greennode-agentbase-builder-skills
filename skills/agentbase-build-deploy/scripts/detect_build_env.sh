#!/usr/bin/env bash
# Read-only check of how this machine / project can BUILD and PUSH the agent image to the registry (vCR):
# which container engine is installed and running (Docker Desktop, Rancher Desktop, OrbStack, Colima, Podman, nerdctl,
# Finch), whether it can build linux/amd64, and which CI the project uses (GitHub Actions, GitLab CI, ...).
# Prints facts and the ways to build; changes nothing, logs in nowhere, prints no credential (only whether a CLI is
# signed in). Usage: bash detect_build_env.sh [project-dir]   (default: current directory)
set -u
DIR="${1:-.}"
cd "$DIR" 2>/dev/null || { echo "ERROR: $DIR is not a directory" >&2; exit 1; }

have() { command -v "$1" >/dev/null 2>&1; }

# run a command with a time limit (macOS has no `timeout`): a stopped VM makes `docker info` hang. Output goes through a
# file, not a pipe, so a child that outlives the kill cannot keep the caller waiting
limited() {
  local secs="$1"; shift
  local out; out="$(mktemp)"
  "$@" >"$out" 2>/dev/null </dev/null & local pid=$!
  ( sleep "$secs"; kill "$pid" 2>/dev/null ) >/dev/null 2>&1 & local guard=$!
  wait "$pid" 2>/dev/null; local rc=$?
  kill "$guard" 2>/dev/null; wait "$guard" 2>/dev/null
  cat "$out"; rm -f "$out"
  return $rc
}

ENGINES_READY=()   # engines that can build now
echo "== Machine"
ARCH="$(uname -m)"
echo "os/arch: $(uname -s)/$ARCH"
[ "$ARCH" = "arm64" ] || [ "$ARCH" = "aarch64" ] && echo "note: ARM host; AgentBase Runtime runs linux/amd64, so build with --platform linux/amd64 (emulated, slower)"

echo
echo "== Container engines"
if have docker; then
  CTX="$(docker context show 2>/dev/null || echo '?')"
  INFO="$(limited 15 docker info --format '{{.ServerVersion}}|{{.OperatingSystem}}|{{.Name}}' 2>/dev/null)"
  if [ -n "$INFO" ]; then
    SRV="${INFO%%|*}"; REST="${INFO#*|}"; OSN="${REST%%|*}"; HOSTN="${REST#*|}"
    KIND="Docker Engine"
    case "$CTX $OSN $HOSTN" in
      *rancher*|*Rancher*|*lima-rancher*) KIND="Rancher Desktop (dockerd / moby mode)" ;;
      *orbstack*|*OrbStack*) KIND="OrbStack" ;;
      *colima*|*Colima*) KIND="Colima" ;;
      *"Docker Desktop"*|*desktop-linux*|*docker-desktop*) KIND="Docker Desktop" ;;
      *podman*|*Podman*) KIND="Podman (docker-compatible socket)" ;;
    esac
    echo "docker: running, $KIND, server $SRV, context '$CTX'"
    ENGINES_READY+=("docker")
    if docker buildx version >/dev/null 2>&1; then
      PLAT="$(limited 15 docker buildx inspect 2>/dev/null | grep -i '^Platforms:' | head -1)"
      echo "docker buildx: yes. ${PLAT:-Platforms: unknown}"
      case "$PLAT" in *linux/amd64*) ;; *) [ "$ARCH" != "x86_64" ] && echo "warning: linux/amd64 not listed; building for the Runtime may fail (docker run --privileged --rm tonistiigi/binfmt --install amd64)" ;; esac
    else
      echo "docker buildx: not found (docker build --platform linux/amd64 still works with the classic builder on amd64 hosts)"
    fi
  else
    echo "docker: CLI installed (context '$CTX') but the daemon does not answer within 15 s — start Docker Desktop / Rancher Desktop / OrbStack / Colima first"
  fi
else
  echo "docker: CLI not found"
fi

for e in nerdctl podman finch buildah; do
  if have "$e"; then
    case "$e" in
      nerdctl) ok=0; limited 15 nerdctl info >/dev/null 2>&1 && ok=1; extra="(Rancher Desktop in containerd mode, Lima, Colima)" ;;
      podman)  ok=0; limited 20 podman info >/dev/null 2>&1 && ok=1; extra="(on macOS/Windows: podman machine start)" ;;
      finch)   ok=0; limited 15 finch info >/dev/null 2>&1 && ok=1; extra="(finch vm start)" ;;
      buildah) ok=1; extra="(builds only; push with buildah push, no daemon needed)" ;;
    esac
    if [ "$ok" = 1 ]; then echo "$e: running $extra"; ENGINES_READY+=("$e"); else echo "$e: installed but not responding $extra"; fi
  fi
done
have rdctl && echo "rdctl: Rancher Desktop is installed (container engine: $(rdctl list-settings 2>/dev/null | grep -i -m1 '\"name\"' | sed 's/.*: *//; s/[\",]//g' || echo unknown))"
[ ${#ENGINES_READY[@]} -eq 0 ] && echo "no container engine is ready on this machine"

echo
echo "== Project and CI"
if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  URL="$(git remote get-url origin 2>/dev/null || true)"
  # keep only the host: never echo a URL that may embed a token
  HOST="$(printf '%s' "$URL" | sed -E 's#^[a-z+]+://([^@/]*@)?##; s#^[^@]+@##; s#[:/].*$##')"
  CI_HOST="none"
  case "$HOST" in
    github.com|*.github.com|*github*) CI_HOST="GitHub" ;;
    *gitlab*) CI_HOST="GitLab" ;;
    *bitbucket*) CI_HOST="Bitbucket" ;;
    dev.azure.com|*visualstudio.com) CI_HOST="Azure DevOps" ;;
    "") CI_HOST="none" ;;
    *) CI_HOST="other ($HOST)" ;;
  esac
  echo "git remote host: ${HOST:-no origin remote} → $CI_HOST"
else
  echo "not a git repository"
  CI_HOST="none"
fi
FOUND=()
for f in .github/workflows/*.yml .github/workflows/*.yaml; do [ -f "$f" ] && { FOUND+=("GitHub Actions (.github/workflows)"); break; }; done
[ -f .gitlab-ci.yml ] && FOUND+=("GitLab CI (.gitlab-ci.yml)")
[ -f Jenkinsfile ] && FOUND+=("Jenkins (Jenkinsfile)")
[ -f bitbucket-pipelines.yml ] && FOUND+=("Bitbucket Pipelines")
[ -f azure-pipelines.yml ] && FOUND+=("Azure Pipelines")
[ ${#FOUND[@]} -gt 0 ] && printf 'CI config in the project: %s\n' "$(IFS=';'; echo "${FOUND[*]}" | sed 's/;/; /g')" || echo "CI config in the project: none"
if have gh; then gh auth status >/dev/null 2>&1 && echo "gh: signed in" || echo "gh: installed, not signed in"; fi
if have glab; then glab auth status >/dev/null 2>&1 && echo "glab: signed in" || echo "glab: installed, not signed in"; fi
[ -f src/backend/Dockerfile ] && echo "Dockerfile: src/backend/Dockerfile (standard)" || { [ -f Dockerfile ] && echo "Dockerfile: ./Dockerfile" || echo "Dockerfile: not found"; }

echo
echo "== Ways to build and push (pick one with the user; never choose silently)"
N=0
for e in "${ENGINES_READY[@]+"${ENGINES_READY[@]}"}"; do
  N=$((N+1))
  case "$e" in
    docker)  echo "$N. LOCAL with docker: make docker-build IMAGE=… TAG=… ; bash <platform-scripts>/cr.sh credentials docker-login ; docker push …" ;;
    nerdctl) echo "$N. LOCAL with nerdctl: nerdctl build --platform linux/amd64 -t … src/backend ; nerdctl login <registry> ; nerdctl push … (the Makefile targets call docker: run these by hand)" ;;
    podman)  echo "$N. LOCAL with podman: podman build --platform linux/amd64 -t … src/backend ; podman login <registry> ; podman push … (the Makefile targets call docker)" ;;
    finch)   echo "$N. LOCAL with finch: finch build --platform linux/amd64 -t … src/backend ; finch login <registry> ; finch push …" ;;
    buildah) echo "$N. LOCAL with buildah: buildah bud --platform linux/amd64 -t … src/backend ; buildah login <registry> ; buildah push …" ;;
  esac
done
case "$CI_HOST" in
  GitHub) N=$((N+1)); echo "$N. CI on GitHub Actions: the scaffold's .github/workflows/ci.yml builds, pushes and deploys (see CI/CD in the skill); no local engine needed" ;;
  GitLab) N=$((N+1)); echo "$N. CI on GitLab: the scaffold ships no GitLab template; a job needs docker (or kaniko / buildah) with the registry credentials as masked CI variables" ;;
esac
if [ "$N" -eq 0 ]; then
  echo "none available. Install Docker Desktop, Rancher Desktop (dockerd mode), OrbStack, Colima or Podman, or push the project to GitHub and use the CI path."
fi
echo
echo "Registry login: this machine's engine and the CI runner each need credentials for the registry; fetch them with the platform scripts (cr.sh), never paste a secret into chat."
