"""Strict KEY=VALUE env files — the only format every AgentBase deploy path reads the same way.

The platform's `runtime.sh --env-file` (/agentbase-deploy) and `docker run --env-file` take each line LITERALLY,
while python-dotenv (the local `.env`) is lenient. A file that works locally can therefore break on the Runtime:
- quotes are kept: `AUTH_AUDIENCE="api://agent"` ⇒ the audience includes the quotes ⇒ every JWT is rejected;
- an inline comment is kept: `HITL_TOOLS=[] # none` ⇒ invalid JSON;
- `export KEY=…` produces a key named "export KEY"; `${VAR}` is expanded by python-dotenv only.
Rules: one KEY=VALUE per line, no quotes, no inline comments, no `export`, no `${…}`; blank lines and full-line
`#` comments are fine. Never GREENNODE_* (injected by the Runtime / reserved).

    python deploy/env_file.py check <file>...       # lint; prints line numbers and key names, never values
    python deploy/env_file.py export <file> --keys 'LLM_*,REFLECTION_*' [--exclude LLM_API_KEY]
                                                     # non-empty KEY=VALUE lines (e.g. for $GITHUB_ENV)

`make check-env ENV_FILE=src/backend/.env.<env>` runs `check`. Standard library only (CI runs it without a venv).
"""

from __future__ import annotations

import argparse
import fnmatch
import re
import sys
from pathlib import Path

PLACEHOLDER = "<set-in-deploy>"  # a secret the deploy fills in (CI secret store / your git-ignored .env.<env>)
DEPLOY_ENVS = ("dev", "staging", "prod")
_LINE_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


class EnvFileError(ValueError):
    pass


def parse(path: str | Path) -> tuple[dict[str, str], list[str]]:
    """Return (values, errors). Errors name the line and key, never the value."""
    values: dict[str, str] = {}
    errors: list[str] = []
    raw = Path(path).read_bytes()
    if b"\r\n" in raw:
        errors.append(f"{path}: Windows line endings (CRLF) — the platform keeps '\\r' in every value")
    for n, line in enumerate(raw.decode("utf-8").replace("\r\n", "\n").split("\n"), start=1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        where = f"{path}:{n}"
        if line.startswith("export "):
            errors.append(f"{where}: remove `export` — the platform would create a key named 'export …'")
            continue
        m = _LINE_RE.match(line)
        if not m:
            errors.append(f"{where}: not KEY=VALUE (no spaces around the key or '=')")
            continue
        key, value = m.groups()
        if key in values:
            errors.append(f"{where}: {key} is set twice")
        if key.startswith("GREENNODE_"):
            errors.append(f"{where}: {key} — GREENNODE_* is injected by the Runtime / reserved, remove it")
        if value[:1] in ("'", '"'):
            errors.append(f"{where}: {key} is quoted — quotes become part of the value on the platform")
        if re.search(r"\s#", value):
            errors.append(f"{where}: {key} has an inline comment — move it to its own line")
        if "${" in value:
            errors.append(f"{where}: {key} uses ${{…}} — only python-dotenv expands it; write the value")
        if value != value.rstrip():
            errors.append(f"{where}: {key} ends with whitespace — kept by the platform, stripped locally")
        values[key] = value
    return values, errors


def check(path: str) -> int:
    values, errors = parse(path)
    warnings: list[str] = []
    is_template = path.endswith(".example")
    app_env = values.get("APP_ENV", "")
    if app_env not in DEPLOY_ENVS:
        errors.append(
            f"{path}: APP_ENV must be one of {', '.join(DEPLOY_ENVS)} (got {app_env or 'nothing'}) — "
            "the Runtime refuses APP_ENV=local"
        )
    unfilled = sorted(k for k, v in values.items() if v == PLACEHOLDER)
    if unfilled and not is_template:
        errors.append(f"{path}: secrets still {PLACEHOLDER}: {', '.join(unfilled)}")
    if "AGENT_VERSION" in values:
        warnings.append(
            f"{path}: AGENT_VERSION overrides the image's build-arg (Langfuse release = image tag) — remove it"
        )
    for w in warnings:
        print(f"WARN  {w}")
    for e in errors:
        print(f"ERROR {e}")
    if errors:
        return 1
    extra = f"; secrets to fill at deploy: {', '.join(unfilled)}" if unfilled else ""
    print(f"OK    {path} ({len(values)} keys{extra})")
    return 0


def select(values: dict[str, str], keys: str, exclude: str = "") -> dict[str, str]:
    inc = [p.strip() for p in keys.split(",") if p.strip()]
    exc = [p.strip() for p in exclude.split(",") if p.strip()]
    return {
        k: v
        for k, v in values.items()
        if v
        and v != PLACEHOLDER
        and any(fnmatch.fnmatchcase(k, p) for p in inc)
        and not any(fnmatch.fnmatchcase(k, p) for p in exc)
    }


def load_strict(path: str | Path) -> dict[str, str]:
    values, errors = parse(path)
    if errors:
        raise EnvFileError("; ".join(errors))
    return values


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check", help="lint deploy env files")
    c.add_argument("files", nargs="+")
    e = sub.add_parser("export", help="print selected non-empty KEY=VALUE lines")
    e.add_argument("file")
    e.add_argument("--keys", required=True, help="comma-separated globs, e.g. 'LLM_*,REFLECTION_*'")
    e.add_argument("--exclude", default="", help="comma-separated globs to skip")
    args = ap.parse_args(argv)
    if args.cmd == "check":
        return max(check(f) for f in args.files)
    try:
        values = load_strict(args.file)
    except EnvFileError as err:
        print(f"ERROR {err}", file=sys.stderr)
        return 1
    for k, v in select(values, args.keys, args.exclude).items():
        print(f"{k}={v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
