"""Set this project's secrets YOURSELF, in your own terminal: hidden input, never echoed, printed or logged.

    make set-secret                                  # menu; Enter = the required ones still missing
    make set-secret NAME=LLM_API_KEY                 # a key, a group (iam, llm, langfuse…) or a comma-separated list
    make set-secret ENV_FILE=src/backend/.env.prod   # a deploy env file for /agentbase-deploy (from .env.prod.example)
    make set-secret LIST=1                           # names + set / not set only: no TTY needed, safe for an agent
    <secret-manager command> | make set-secret NAME=LLM_API_KEY STDIN=1    # one value from a pipe, no prompt

Why: everything pasted into a chat with a coding agent is stored by the agent host and may reach third parties.
A coding agent cannot type into a hidden prompt, so it asks YOU to run this in a terminal it does not drive (a new
terminal window or tab, the IDE's terminal, the Terminal pane of the Claude Code desktop app) — not via the chat's
`!` shell escape. Without a TTY it refuses rather than read a value that would be echoed. STDIN=1 exists for
secret managers (`op read …`, `pass show …`, `security find-generic-password -w …`): the value flows through the
pipe and never appears on a command line. Never `echo <secret> |` (shell history, process list).

What can be set — derived from the project's files; the SPECS tables below only add hints and checks:
  .greennode.json  every key of .greennode.json.example (local IAM client_id / client_secret, agent_identity)
  the env file     SPECS keys its template (.env.example, .env.<env>.example) declares, every `<set-in-deploy>`
                   key, secret-looking ${VAR} references of enabled entries in mcp_servers.json / a2a_agents.json,
                   and any other NAME you pass explicitly
Writes are atomic, mode 600, keep every other line / key, and are refused for files git does not ignore and for
`*.example` templates. Values are checked against the strict env-file format (deploy/env_file.py) and for obvious
wrong pastes (quotes, spaces, `Bearer `, a whole NAME=value line, the same value as another secret…).
Standard library only, Python >= 3.9 (macOS ships 3.9 as python3): it runs before `uv sync`. The agentbase-build
skills ship this file twice, identical (agent backend and MCP server templates; their CI compares them).
"""

from __future__ import annotations

import argparse
import fnmatch
import getpass
import json
import os
import re
import secrets
import subprocess
import sys
import warnings
from dataclasses import dataclass
from pathlib import Path

# src/backend of an agent, or the MCP server's directory
BASE = Path(__file__).resolve().parent.parent
JSON_FILE = ".greennode.json"
REF_FILES = ("mcp_servers.json", "a2a_agents.json")
PLACEHOLDER = "<set-in-deploy>"  # deploy/env_file.py: a secret the deploy fills in
MIN_SECRET_CHARS = 8
ATTEMPTS = 3
_ASSIGN_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")
_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")  # a new env key: UPPER_CASE only (else: a typo?)
_REF_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_SECRETISH_RE = re.compile(r"(?:^|_)(?:KEY|SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIALS?)(?:_|$)")


class SetSecretError(Exception):
    """A refusal: the message names files and keys, never a value."""


@dataclass(frozen=True)
class Spec:
    group: str
    hint: str
    secret: bool = True  # hidden input
    required: bool = False
    strict: bool = True  # credential checks: min length, not a URL / JSON
    url: bool = False
    generate: bool = False  # Enter on an empty prompt = a random value
    wrong: tuple[tuple[str, str], ...] = ()  # (prefix, why it is the wrong value)


# Keys of .greennode.json (local dev only — the Runtime injects GREENNODE_CLIENT_ID/SECRET/AGENT_IDENTITY).
JSON_SPECS = {
    "client_id": Spec(
        "iam", "IAM service account client id — /agentbase (IAM setup)", required=True
    ),
    "client_secret": Spec(
        "iam",
        "IAM service account client secret — shown once at creation; lost ⇒ reset it in the IAM console",
        required=True,
    ),
    "agent_identity": Spec(
        "identity",
        "name of the DEV agent identity, only when tools use Identity providers (/agentbase-identity)",
        secret=False,
        strict=False,
    ),
}
# Secrets of env files, offered when the file's template declares them.
ENV_SPECS = {
    "LLM_API_KEY": Spec(
        "llm",
        "AIP MaaS API key — /agentbase-llm creates one (and can save it here itself)",
        required=True,
    ),
    "LANGFUSE_PUBLIC_KEY": Spec(
        "langfuse",
        "Langfuse project → Settings → API Keys (pk-lf-…)",
        wrong=(
            ("sk-lf-", "is the Langfuse SECRET key (sk-lf-…): the public key starts with pk-lf-"),
        ),
    ),
    "LANGFUSE_SECRET_KEY": Spec(
        "langfuse",
        "Langfuse project → Settings → API Keys (sk-lf-…)",
        wrong=(
            ("pk-lf-", "is the Langfuse PUBLIC key (pk-lf-…): the secret key starts with sk-lf-"),
        ),
    ),
    "LANGFUSE_BASE_URL": Spec(
        "langfuse",
        "Langfuse URL (not secret) — https://cloud.langfuse.com or your self-hosted one",
        secret=False,
        strict=False,
        url=True,
    ),
    "FEEDBACK_SIGNING_KEY": Spec(
        "feedback",
        "HMAC key of feedback tokens (optional) — Enter generates a random one",
        generate=True,
    ),
    "MCP_BACKEND_TOKEN": Spec(
        "backend", "service credential for the internal system at MCP_BACKEND_URL (MCP server)"
    ),
}


GROUPS = {s.group for s in (*JSON_SPECS.values(), *ENV_SPECS.values())}


@dataclass
class Entry:
    name: str
    path: Path
    spec: Spec
    required: bool
    declared: bool = True  # False: NAME given explicitly, not in the file or its template


def rel(path: Path) -> str:
    try:
        return os.path.relpath(path, Path.cwd()) if path.is_relative_to(Path.cwd()) else str(path)
    except ValueError:  # another drive (Windows)
        return str(path)


def is_set(value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False
    return not (value.startswith("<") and value.endswith(">"))  # <set-in-deploy>, <IAM … client id>


def parse_env(lines: list[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in lines:
        if line.lstrip().startswith("#"):
            continue
        m = _ASSIGN_RE.match(line.rstrip("\r\n"))
        if not m:
            continue
        value = m.group(2).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]  # python-dotenv (local .env) unquotes
        else:
            value = re.sub(r"\s+#.*$", "", value)
        values[m.group(1)] = value
    return values


def template_for(path: Path) -> Path | None:
    # .env → .env.example, .env.prod → .env.prod.example
    example = path.with_name(path.name + ".example")
    return example if example.is_file() else None


def atomic_write(path: Path, text: str) -> None:
    """Temp file (0600, named .env.* so every .gitignore here covers a leftover) + fsync + rename."""
    target = path.resolve()  # a symlinked file: write its target, keep the link
    tmp = target.with_name(f".env.set-secret-{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, target)
    except BaseException:
        if tmp.exists():
            tmp.unlink()
        raise


class EnvFile:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.template = template_for(path)
        self.exists = path.is_file()
        if path.exists() and not self.exists:
            raise SetSecretError(f"{rel(path)} is not a regular file")
        source = path if self.exists else self.template
        self.lines = source.read_text(encoding="utf-8").splitlines(keepends=True) if source else []
        tpl = self.template.read_text(encoding="utf-8").splitlines() if self.template else []
        self.template_values = parse_env(tpl)

    def values(self) -> dict[str, str]:
        return parse_env(self.lines)

    def set(self, key: str, value: str) -> int:
        n = 0
        for i, line in enumerate(self.lines):
            body = line.rstrip("\r\n")
            m = _ASSIGN_RE.match(body)
            if m and m.group(1) == key and not line.lstrip().startswith("#"):
                self.lines[i] = f"{key}={value}" + line[len(body) :]  # keeps the line ending
                n += 1
        if n == 0:
            if self.lines and not self.lines[-1].endswith("\n"):
                self.lines[-1] += "\n"
            self.lines.append(f"{key}={value}\n")
        atomic_write(self.path, "".join(self.lines))
        return n


class JsonFile:
    def __init__(self, path: Path, example: Path) -> None:
        self.path = path
        self.exists = path.is_file()
        source = path if self.exists else example
        try:
            data = json.loads(source.read_text(encoding="utf-8"))
        except ValueError as e:  # the message carries line/column only, never content
            line = getattr(e, "lineno", "?")
            raise SetSecretError(
                f"{rel(source)} is not valid JSON (line {line}) — fix it in your editor, or delete it to start "
                f"again from {example.name}"
            ) from None
        if not isinstance(data, dict):
            raise SetSecretError(f"{rel(source)} must hold a JSON object {{…}}")
        if not self.exists:  # never copy the .example's <placeholders>
            data = {k: (v if is_set(v) or not isinstance(v, str) else "") for k, v in data.items()}
        self.data = data
        self.keys = list(data)  # what can be set: the .example's keys, then the file's own
        if self.exists and example.is_file() and example != path:
            try:
                tpl = json.loads(example.read_text(encoding="utf-8"))
            except ValueError:
                tpl = {}
            if isinstance(tpl, dict):
                self.keys = [*tpl, *(k for k in data if k not in tpl)]

    def values(self) -> dict[str, str]:
        return {k: v for k, v in self.data.items() if isinstance(v, str)}

    def set(self, key: str, value: str) -> int:
        self.data[key] = value
        atomic_write(self.path, json.dumps(self.data, indent=2, ensure_ascii=False) + "\n")
        return 1


def referenced_vars(base: Path) -> dict[str, str]:
    """Secret-looking ${VAR} of enabled entries in mcp_servers.json / a2a_agents.json → the file naming it."""
    found: dict[str, str] = {}
    for fname in REF_FILES:
        try:
            data = json.loads((base / fname).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        section = (data.get("servers") or data.get("agents")) if isinstance(data, dict) else None
        for item in section.values() if isinstance(section, dict) else []:
            if isinstance(item, dict) and item.get("enabled", True) is not False:
                for name in _REF_RE.findall(json.dumps(item)):
                    if _SECRETISH_RE.search(name):
                        found.setdefault(name, fname)
    return found


class Project:
    def __init__(self, base: Path, env_path: Path) -> None:
        if env_path.name.endswith(".example"):
            raise SetSecretError(
                f"{rel(env_path)} is a committed template — secrets go into its git-ignored copy "
                f"(ENV_FILE={rel(env_path.with_name(env_path.name[: -len('.example')]))})"
            )
        if not env_path.parent.is_dir():
            raise SetSecretError(f"directory {rel(env_path.parent)} does not exist")
        self.base = base
        self.deploy = env_path.name != ".env"  # .env.<env>: a deploy env file (never IAM)
        self.env = EnvFile(env_path)
        example = base / (JSON_FILE + ".example")
        json_path = base / JSON_FILE
        self.json = None
        if not self.deploy and (example.is_file() or json_path.is_file()):
            self.json = JsonFile(json_path, example if example.is_file() else json_path)
        self.entries = self._catalog()

    def _catalog(self) -> list[Entry]:
        entries: list[Entry] = []
        if self.json is not None:
            for key in self.json.keys:
                spec = JSON_SPECS.get(key, Spec("other", f"key of {JSON_FILE}", strict=False))
                entries.append(Entry(key, self.json.path, spec, spec.required))
        env = self.env
        declared = {**env.template_values, **env.values()}
        for name, spec in ENV_SPECS.items():
            if name in declared:
                required = (
                    env.template_values.get(name) == PLACEHOLDER if self.deploy else spec.required
                )
                entries.append(Entry(name, env.path, spec, required))
        known = {e.name for e in entries}
        for name, value in env.template_values.items():
            if value == PLACEHOLDER and name not in known:
                hint = "required secret of this environment (<set-in-deploy> in the template)"
                entries.append(Entry(name, env.path, Spec("deploy", hint, strict=False), True))
        known = {e.name for e in entries}
        for name, fname in referenced_vars(self.base).items():
            if name not in known:
                spec = Spec("refs", f"referenced as ${{{name}}} in {fname}", strict=False)
                entries.append(Entry(name, env.path, spec, False))  # declared by that file
        return entries

    def current(self, entry: Entry) -> str:
        store = self.json if self.json is not None and entry.path == self.json.path else self.env
        return store.values().get(entry.name, "")

    def other_secrets(self, entry: Entry) -> dict[str, str]:
        out = {}
        for e in self.entries:
            value = self.current(e)
            if e.name != entry.name and e.spec.secret and is_set(value):
                out[e.name] = value
        return out

    def save(self, entry: Entry, value: str) -> str:
        is_json = self.json is not None and entry.path == self.json.path
        store = self.json if is_json else self.env
        created = not store.exists
        n = store.set(entry.name, value)
        store.exists = True
        note = ""
        if created:
            tpl = self.env.template if not is_json else self.base / (JSON_FILE + ".example")
            note = f" (created from {tpl.name})" if tpl and tpl.is_file() else " (new file)"
        elif n > 1:
            note = f" — {entry.name} was on {n} lines, all updated: remove the duplicates"
        return f"{rel(entry.path)}{note}"

    def resolve(self, tokens: list[str]) -> list[Entry]:
        """Key names (any case; GREENNODE_CLIENT_SECRET = client_secret), group names, or a new UPPER_CASE key."""
        groups: dict[str, list[Entry]] = {}
        for e in self.entries:
            groups.setdefault(e.spec.group, []).append(e)
        by_name = {e.name.lower(): e for e in self.entries}
        picked: list[Entry] = []
        for token in tokens:
            low = token.lower()
            if low.startswith("greennode_") and low[len("greennode_") :] in by_name:
                low = low[len("greennode_") :]
            if low in groups:
                found = groups[low]
            elif low in by_name:
                found = [by_name[low]]
            else:
                found = [self._undeclared(token)]
            picked.extend(e for e in found if e not in picked)
        return picked

    def _undeclared(self, token: str) -> Entry:
        low = token.lower()
        if low in JSON_SPECS or low.startswith("greennode_") or low == "iam":
            if self.deploy or low.startswith("greennode_"):
                raise SetSecretError(
                    f"{token}: IAM credentials never go in an env file — the Runtime injects GREENNODE_*; for "
                    f"local dev they live in {JSON_FILE}: make set-secret NAME=iam (without ENV_FILE)"
                )
            raise SetSecretError(f"{token}: this project has no {JSON_FILE}.example")
        if low in GROUPS:
            raise SetSecretError(
                f"{token}: none of its keys is declared in {rel(self.env.path)} or its template"
            )
        if not _NAME_RE.match(token):
            choices = sorted({e.spec.group for e in self.entries} | {e.name for e in self.entries})
            raise SetSecretError(
                f"unknown name {token!r} — choose from {', '.join(choices) or '(nothing declared)'}, "
                "or pass an UPPER_CASE variable name to add"
            )
        declared = token in self.env.values() or token in self.env.template_values
        if token in ENV_SPECS:
            spec = ENV_SPECS[token]
        elif declared:  # e.g. LLM_MODEL: a plain prompt unless the name looks like a secret
            hint = f"declared in {rel(self.env.path)} or its template"
            spec = Spec("other", hint, secret=bool(_SECRETISH_RE.search(token)), strict=False)
        else:
            spec = Spec("other", f"new key for {rel(self.env.path)}", strict=False)
        return Entry(token, self.env.path, spec, False, declared=declared)


# ---------------------------------------------------------------- checks


def normalize(raw: str) -> tuple[str, str]:
    value = raw.strip()
    return value, ("surrounding whitespace removed" if value != raw else "")


def problem(entry: Entry, value: str, others: dict[str, str], names: list[str]) -> str | None:
    """Why `value` is not acceptable for `entry`, or None. Never includes the value."""
    spec = entry.spec
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        return "contains control characters (a terminal paste escape sequence?) — paste it again"
    if re.match(r"(?i)(?:bearer|basic|token)\s", value):
        return "starts with an auth scheme ('Bearer …') — paste only the key itself"
    line_re = (
        r"(?i)\s*(?:export\s+)?[\"']?(?:" + "|".join(map(re.escape, names)) + r")[\"']?\s*[=:]"
    )
    if names and re.match(line_re, value):
        return "looks like a whole NAME=value line — paste only the value"
    if value[0] in "'\"" or value[-1] in "'\"":
        return "is wrapped in quotes — paste it without them (env files keep quotes as part of the value)"
    if re.search(r"\s", value):
        return "contains whitespace — paste only the value"
    if "${" in value:
        return "contains ${…} — python-dotenv would expand it; paste the literal value"
    if value.startswith("<") and value.endswith(">"):
        return "is still a placeholder"
    if spec.url and not re.match(r"https?://[^/\s]+", value):
        return "must be an http(s):// URL"
    if spec.secret and spec.strict:
        if re.match(r"(?i)https?://", value):
            return "is a URL — this field takes the key itself"
        if value[0] in "{[":
            return "looks like JSON — paste only the value of the key"
        if len(value) < MIN_SECRET_CHARS:
            return f"has only {len(value)} characters — copy the whole value"
    for prefix, why in spec.wrong:
        if value.startswith(prefix):
            return why
    if spec.secret:
        for name, other in others.items():
            if other == value:
                return f"is the same value as {name} — wrong one pasted?"
    return None


# ---------------------------------------------------------------- git


def _git(cwd: Path, *args: str) -> int:
    try:
        return subprocess.run(
            ["git", "-C", str(cwd), *args], capture_output=True, timeout=15, check=False
        ).returncode
    except (OSError, subprocess.SubprocessError):
        return -1


def _gitignore_says(path: Path, root: Path) -> bool | None:
    """Outside a git repo: evaluate the .gitignore files from `root` down to the file (last match wins)."""
    verdict = None
    dirs = [path.parent, *path.parent.parents]
    dirs = list(reversed(dirs[: dirs.index(root) + 1])) if root in dirs else [path.parent]
    for d in dirs:
        gi = d / ".gitignore"
        if not gi.is_file():
            continue
        relpath = path.relative_to(d).as_posix()
        for raw in gi.read_text(encoding="utf-8", errors="replace").splitlines():
            pat = raw.strip()
            if not pat or pat.startswith("#") or pat.endswith("/"):
                continue
            negate = pat.startswith("!")
            pat = pat.lstrip("!")
            if pat.startswith("**/"):
                pat = pat[3:]
            hit = (
                fnmatch.fnmatchcase(relpath, pat.lstrip("/"))
                if "/" in pat
                else fnmatch.fnmatchcase(path.name, pat)
            )
            if hit:
                verdict = not negate
    return verdict


def ensure_ignored(path: Path) -> None:
    """Refuse to write a secret file git would commit (a symlink is judged by its own name, like git does)."""
    path = Path(os.path.abspath(path))
    where = path.parent
    if _git(where, "rev-parse", "--show-toplevel") == 0:
        if _git(where, "ls-files", "--error-unmatch", "--", path.name) == 0:
            raise SetSecretError(
                f"{rel(path)} is tracked by git: `git rm --cached {rel(path)}` and commit — if it was ever "
                "committed, ROTATE every secret it held (it stays in the history)"
            )
        if _git(where, "check-ignore", "-q", "--", path.name) == 0:
            return
        verdict = False
    else:
        cwd = Path.cwd()
        verdict = _gitignore_says(path, cwd if path.is_relative_to(cwd) else path.parent)
    if not verdict:
        raise SetSecretError(
            f"{rel(path)} is not git-ignored — add `{path.name}` to .gitignore first (the scaffold's .gitignore "
            "ignores .env, .env.* and .greennode.json), then run this again"
        )


# ---------------------------------------------------------------- terminal I/O (patched in tests)


def stdin_is_tty() -> bool:
    return sys.stdin is not None and sys.stdin.isatty()


def read_hidden(prompt: str) -> str:
    """getpass from the terminal; if echo cannot be turned off, stop instead of reading an echoed value."""
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            return getpass.getpass(prompt)
        except getpass.GetPassWarning:
            raise SetSecretError("this terminal cannot hide input — nothing was read") from None


def confirm(prompt: str) -> bool:
    return input(prompt).strip().lower() in ("y", "yes")


# ---------------------------------------------------------------- modes


def print_status(project: Project, numbered: bool = False) -> None:
    files: dict[Path, list[tuple[int, Entry]]] = {}
    for i, e in enumerate(project.entries, start=1):
        files.setdefault(e.path, []).append((i, e))
    for path, items in files.items():
        store = (
            project.json if project.json is not None and path == project.json.path else project.env
        )
        state = "" if store.exists else "  (not created yet)"
        print(f"{rel(path)}{state}")
        for i, e in items:
            status = "set" if is_set(project.current(e)) else "not set"
            tags = ", ".join(
                t
                for t in (
                    "required" if e.required else "optional",
                    "" if e.spec.secret else "not secret",
                    "" if e.declared else "new key",
                )
                if t
            )
            num = f"{i:>3}  " if numbered else "  "
            print(f"{num}{e.name:<24} {status:<8} {e.spec.group:<9} {tags}")
    if not project.entries:
        print("  (no known secret here — pass NAME=<VARIABLE> explicitly)")


def no_tty_message(
    names: list[str], entries: list[Entry], env_path: Path, default_env: Path
) -> str:
    env = f" ENV_FILE={rel(env_path)}" if env_path.resolve() != default_env.resolve() else ""
    cmd = "make set-secret" + (f" NAME={','.join(names)}" if names else "") + env
    one = entries[0].name if len(entries) == 1 else "<KEY>"
    return (
        "✖ set-secret reads secrets with a hidden prompt, so it must run in YOUR interactive terminal — this one\n"
        "  has no TTY (a coding agent's shell, the chat's `!` command, a pipe or CI). Open a terminal in\n"
        f"  {Path.cwd()} and run:\n"
        f"      {cmd}\n"
        "  A coding agent cannot type into that prompt — and never paste the value into the chat instead.\n"
        f"  From a secret manager instead: <command printing the value> | make set-secret NAME={one}{env} STDIN=1"
    )


def ask(project: Project, entry: Entry, names: list[str]) -> str | None:
    print(f"\n{entry.name} — {entry.spec.hint}")
    current = project.current(entry)
    if not entry.declared and not confirm(
        f"  {entry.name} is not declared in {rel(entry.path)} or its template — add it? [y/N] "
    ):
        return None
    if entry.spec.secret and is_set(current):
        if not confirm(f"  {entry.name} is already set ({len(current)} chars). Replace it? [y/N] "):
            print("  kept")
            return None
    others = project.other_secrets(entry)
    for attempt in range(1, ATTEMPTS + 1):
        if entry.spec.secret:
            skip = "generate" if entry.spec.generate else "skip"
            raw = read_hidden(f"  value (hidden, Enter = {skip}): ")
        else:
            raw = input(
                f"  value [{current}] (Enter = keep): " if current else "  value (Enter = skip): "
            )
        if not raw.strip():
            if entry.spec.generate:
                print("  generated a random value")
                return secrets.token_urlsafe(32)
            print("  kept" if is_set(current) else "  skipped")
            return None
        value, note = normalize(raw)
        why = problem(entry, value, others, names)
        if why is None:
            if note:
                print(f"  ({note})")
            return value
        retry = " — try again" if attempt < ATTEMPTS else f" — skipped after {ATTEMPTS} attempts"
        print(f"  ✖ {entry.name} {why}{retry}")
    return None


def choose(project: Project) -> list[Entry]:
    print_status(project, numbered=True)
    missing = [e for e in project.entries if e.required and not is_set(project.current(e))]
    hint = ", ".join(e.name for e in missing) or "nothing — all required keys are set"
    print(
        f"\nWhich ones? numbers, names or groups, comma-separated · Enter = required not set ({hint}) · q = quit"
    )
    while True:
        answer = input("> ").strip()
        if answer.lower() in ("q", "quit", "exit"):
            return []
        if not answer:
            if missing:
                return missing
            print("Nothing required is missing — pick numbers or names, or q.")
            continue
        picked: list[Entry] = []
        try:
            for t in (t for t in re.split(r"[,\s]+", answer) if t):
                if t.isdigit() and not 0 < int(t) <= len(project.entries):
                    raise SetSecretError(f"no number {t} in the list")
                found = [project.entries[int(t) - 1]] if t.isdigit() else project.resolve([t])
                picked.extend(e for e in found if e not in picked)
            return picked
        except SetSecretError as e:
            print(f"✖ {e}")


def next_step(project: Project) -> str:
    if project.deploy:
        if Path("deploy/env_file.py").is_file():
            return f"make check-env ENV_FILE={rel(project.env.path)}"
        return "review the file in your editor"
    if (project.base / "scripts" / "check_creds.py").is_file():
        return "make check-creds   (verifies them; prints OK/FAIL only — safe to share with your coding agent)"
    return "make dev"


def run_interactive(project: Project, entries: list[Entry]) -> int:
    print(
        "Values you type are hidden: nothing is shown or printed, not even its length until it is saved.\n"
        "Paste (Cmd+V / Ctrl+Shift+V), then Enter."
    )
    if not entries:
        entries = choose(project)
    if not entries:
        return 0
    for path in {e.path for e in entries}:
        ensure_ignored(path)
    names = [e.name for e in project.entries] + [e.name for e in entries]
    saved = 0
    for entry in entries:
        value = ask(project, entry, names)
        if value is None:
            continue
        where = project.save(entry, value)
        saved += 1
        shown = f"set ({len(value)} chars)" if entry.spec.secret else f"= {value}"
        print(f"  ✔ {entry.name} {shown} → {where}")
    if saved:
        print(f"\n{saved} saved (mode 600, git-ignored). Next: {next_step(project)}")
    return 0


def run_stdin(project: Project, entries: list[Entry], yes: bool) -> int:
    if len(entries) != 1:
        raise SetSecretError(
            f"STDIN=1 takes exactly one key (got {len(entries)}): NAME=<KEY> STDIN=1"
        )
    if stdin_is_tty():
        raise SetSecretError(
            "STDIN=1 reads a pipe — in a terminal, drop STDIN=1 to get the hidden prompt"
        )
    entry = entries[0]
    data = sys.stdin.read()
    if data.endswith("\n"):
        data = data[:-1]
    if data.endswith("\r"):
        data = data[:-1]
    if "\n" in data or "\r" in data:
        raise SetSecretError(
            f"{entry.name}: stdin holds more than one line — pipe exactly one value"
        )
    value, _ = normalize(data)
    if not value:
        raise SetSecretError(f"{entry.name}: stdin was empty — nothing changed")
    names = [e.name for e in project.entries] + [entry.name]
    why = problem(entry, value, project.other_secrets(entry), names)
    if why:
        raise SetSecretError(f"{entry.name} {why} — nothing changed")
    current = project.current(entry)
    if not yes and (is_set(current) or not entry.declared):
        reason = (
            "is already set" if is_set(current) else "is not declared in the file or its template"
        )
        raise SetSecretError(f"{entry.name} {reason} — add YES=1 to write it anyway")
    ensure_ignored(entry.path)
    where = project.save(entry, value)
    shown = f"set ({len(value)} chars)" if entry.spec.secret else f"= {value}"
    print(f"✔ {entry.name} {shown} → {where}. Next: {next_step(project)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--name", default="", help="keys / groups, comma- or space-separated")
    ap.add_argument("--env-file", default="", help="env file to write (default: <base>/.env)")
    ap.add_argument("--list", action="store_true", help="show names and set / not set, then exit")
    ap.add_argument(
        "--stdin", action="store_true", help="read ONE value from a pipe (secret managers)"
    )
    ap.add_argument("--yes", action="store_true", help="with --stdin: replace an existing value")
    ap.add_argument("--base", default="", help=argparse.SUPPRESS)  # tests: project directory
    args = ap.parse_args(argv)
    base = Path(args.base).resolve() if args.base else BASE
    default_env = base / ".env"
    env_path = Path(args.env_file) if args.env_file else default_env
    names = [t for t in re.split(r"[,\s]+", args.name) if t]
    try:
        project = Project(base, env_path)
        if args.list:
            print_status(project)
            print("Set them in YOUR terminal: make set-secret [NAME=<key|group>] (hidden input)")
            return 0
        entries = project.resolve(names)
        if args.stdin:
            return run_stdin(project, entries, args.yes)
        if not stdin_is_tty():
            print(no_tty_message(names, entries, env_path, default_env), file=sys.stderr)
            return 2
        return run_interactive(project, entries)
    except SetSecretError as e:
        print(f"✖ {e}", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print(
            "\nStopped — keys saved before this point are kept, nothing else changed.",
            file=sys.stderr,
        )
        return 130


if __name__ == "__main__":
    sys.exit(main())
