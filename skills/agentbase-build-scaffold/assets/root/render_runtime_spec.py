"""Build the `grn agentbase runtime update` spec from the rendered deploy manifest.

`grn agentbase deploy up` only CREATES a runtime; an existing one is left as-is ("converging, not re-applying"),
so a new image / env would silently not ship. CI therefore updates existing runtimes with
`grn agentbase runtime update <id> --file <spec>` — a FULL-spec replacement (new version, DEFAULT endpoint rolls
forward). This script derives that spec from the same manifest, so image/env/flavor have one source of truth.

    python deploy/render_runtime_spec.py <rendered agent.yaml> <out runtime.yaml>

`runtime update` does not accept `imageAuth: auto`: pull credentials come from VCR_USERNAME / VCR_PASSWORD (the vCR
robot account). The output contains secrets — write it to a temp dir and delete it after use, never commit it.
Field names follow grn v1.13.0 `UpdateAgentRuntimeRequest` (+ `name`, which the file validator requires).
"""

from __future__ import annotations

import os
import sys

import yaml


def _env_value(key: str, value: object) -> str:
    """Env values as `deploy up` would send them: null ⇒ "" (pydantic-settings then uses the default thanks to
    env_ignore_empty), str/int/float as text; bools/lists/maps are rejected instead of becoming "True"/"[...]"."""
    if value is None:
        return ""
    if isinstance(value, bool) or not isinstance(value, str | int | float):
        raise ValueError(f"runtime.env.{key}: quote the value in the manifest (got {type(value).__name__})")
    return str(value)


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


def main(src: str, dst: str) -> int:
    with open(src, encoding="utf-8") as f:
        manifest = yaml.safe_load(f)
    spec = build_spec(manifest, os.environ["VCR_USERNAME"], os.environ["VCR_PASSWORD"])
    with open(dst, "w", encoding="utf-8") as f:
        yaml.safe_dump(spec, f, sort_keys=False, allow_unicode=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
