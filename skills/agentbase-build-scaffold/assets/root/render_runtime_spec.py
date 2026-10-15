"""Complete the deploy manifest's env and build the `grn agentbase runtime update` spec from it.

    python deploy/render_runtime_spec.py <agent.yaml> <out runtime.yaml> \
        --env-file src/backend/.env.<env>.example [--env-file <extra secrets file>] \
        [--secrets LLM_API_KEY,LANGFUSE_PUBLIC_KEY,...]

1. `runtime.env` of <agent.yaml> (rendered by envsubst: APP_ENV, AGENT_VERSION) is merged with the committed,
   non-secret config (`--env-file`, strict KEY=VALUE — see deploy/env_file.py) and with the secrets named in
   `--secrets`, read from the process environment (the CI secret store). Each key has ONE source: a key set to two
   different values ⇒ error (catches a secret committed in the env file). Empty values and `<set-in-deploy>`
   placeholders are filled by the other sources; a placeholder left at the end ⇒ error. <agent.yaml> is rewritten in
   place (`grn agentbase deploy up` reads it).
2. `grn agentbase deploy up` only CREATES a runtime; an existing one is left as-is ("converging, not re-applying"),
   so CI updates it with `grn agentbase runtime update <id> --file <runtime.yaml>` — a FULL-spec replacement (new
   version, DEFAULT endpoint rolls forward). <runtime.yaml> is derived from the merged manifest, so image/env/flavor
   have one source of truth. Field names follow grn v1.13.0 `UpdateAgentRuntimeRequest` (+ `name`, which the file
   validator requires). That request has NO networkConfig / allowedCidrs / inboundAuth: CI refuses to update a
   runtime that uses them (.github/workflows/ci.yml).

`runtime update` does not accept `imageAuth: auto`: pull credentials come from VCR_USERNAME / VCR_PASSWORD (the vCR
robot account). Both outputs contain secrets — write them to a temp dir and delete them after use, never commit them.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from env_file import DEPLOY_ENVS, PLACEHOLDER, load_strict  # noqa: E402


def _env_value(key: str, value: object) -> str:
    """Env values as `deploy up` would send them: null ⇒ "" (pydantic-settings then uses the default thanks to
    env_ignore_empty), str/int/float as text; bools/lists/maps are rejected instead of becoming "True"/"[...]"."""
    if value is None:
        return ""
    if isinstance(value, bool) or not isinstance(value, str | int | float):
        raise ValueError(f"runtime.env.{key}: quote the value in the manifest (got {type(value).__name__})")
    return str(value)


def _real(value: str) -> bool:
    return value not in ("", PLACEHOLDER)


def merge_env(sources: list[tuple[str, dict[str, str]]]) -> dict[str, str]:
    """Merge (source name, values) pairs; a key may get a real value from one source only."""
    merged: dict[str, str] = {}
    origin: dict[str, str] = {}
    for name, values in sources:
        for key, value in values.items():
            old = merged.get(key)
            if old is None or (_real(value) and not _real(old)):
                merged[key], origin[key] = value, name  # first sight, or a real value fills ""/placeholder
            elif _real(value) and value != old:
                raise ValueError(f"{key} is set by both {origin[key]} and {name} — keep one source")
            elif value == PLACEHOLDER and old == "":
                merged[key], origin[key] = value, name  # a later source marks the key as a required secret
            # an empty value never overrides anything
    return merged


def validate_env(env: dict[str, str]) -> None:
    reserved = sorted(k for k in env if k.startswith("GREENNODE_"))
    if reserved:
        raise ValueError(f"remove {', '.join(reserved)} — injected by the Runtime / reserved")
    if env.get("APP_ENV") not in DEPLOY_ENVS:
        raise ValueError(f"APP_ENV must be one of {', '.join(DEPLOY_ENVS)} (the Runtime refuses local)")
    unfilled = sorted(k for k, v in env.items() if v == PLACEHOLDER)
    if unfilled:
        raise ValueError(f"no value for {', '.join(unfilled)} — add the secret(s) to the CI secret store")


def complete_manifest(manifest: dict, env_files: list[str], secret_names: list[str]) -> dict:
    rt = manifest["runtime"]
    sources = [(f, load_strict(f)) for f in env_files]
    sources.append(("the manifest", {k: _env_value(k, v) for k, v in (rt.get("env") or {}).items()}))
    sources.append(("the CI secrets", {k: os.environ.get(k, "") for k in secret_names}))
    env = merge_env(sources)
    validate_env(env)
    rt["env"] = env
    return manifest


def build_spec(manifest: dict, vcr_username: str, vcr_password: str) -> dict:
    rt = manifest["runtime"]
    return {
        "name": manifest["name"],
        "description": manifest.get("description", ""),
        "imageUrl": rt["image"],
        "imageAuth": {"enabled": True, "username": vcr_username, "password": vcr_password},
        "command": list(rt.get("command") or []),
        "args": list(rt.get("args") or []),
        "environmentVariables": {k: _env_value(k, v) for k, v in (rt.get("env") or {}).items()},
        "flavorId": rt["flavorId"],
        "autoscaling": rt["autoscaling"],
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("manifest", help="rendered agent.yaml (rewritten in place with the merged env)")
    ap.add_argument("spec", help="output: runtime update spec")
    ap.add_argument("--env-file", action="append", default=[], help="strict KEY=VALUE file (repeatable)")
    ap.add_argument("--secrets", default="", help="comma-separated env var names read from the environment")
    args = ap.parse_args(argv)
    with open(args.manifest, encoding="utf-8") as f:
        manifest = yaml.safe_load(f)
    secret_names = [s.strip() for s in args.secrets.split(",") if s.strip()]
    try:
        complete_manifest(manifest, args.env_file, secret_names)
    except ValueError as e:  # includes EnvFileError; messages name keys, never values
        print(f"ERROR {e}", file=sys.stderr)
        return 1
    with open(args.manifest, "w", encoding="utf-8") as f:
        yaml.safe_dump(manifest, f, sort_keys=False, allow_unicode=True)
    spec = build_spec(manifest, os.environ["VCR_USERNAME"], os.environ["VCR_PASSWORD"])
    with open(args.spec, "w", encoding="utf-8") as f:
        yaml.safe_dump(spec, f, sort_keys=False, allow_unicode=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
