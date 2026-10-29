"""Drift checks: the docs of this skill set vs its code and templates (standard library only).

Run by check_skills.py (CI job `static`); also alone: `python3 .github/scripts/check_drift.py`.
Every finding is `<file>:<line>: [<check>] <what is wrong> — <what to do>`. Fix the doc or the template; when the
difference is intended, add an entry WITH its reason to .github/scripts/drift_allowlist.py.

settings-env  app/config.py Settings ↔ .env.example (every field, as an active KEY= line; empty = the default) ↔
              env.deploy.example (subset of .env.example; no GREENNODE_* / AGENT_VERSION; APP_ENV templated) ↔ ci.yml
              (`<set-in-deploy>` keys in `--secrets`; `export --keys` globs match settings and are named by the eval
              SKILL; every secrets.* / vars.* listed in the header comment); MCP server settings.py ↔ its .env.example;
              frontend (Expo) and web (Next.js) templates: process.env ↔ their .env.example.
trace-names   names emitted by the backend templates (tracing.step/event/score_trace, run_name, graph nodes, get_llm,
              trace_request) ↔ the Step 2 tree of agentbase-build-tracing/SKILL.md, both ways and with observation
              types; tracing.py's docstring tree ⊆ that tree; names cited in references/trace-schema.md are emitted;
              every _CHAIN_TYPES entry is in its observation-type table; _trace_meta() keys ↔ its `metadata` row.
make-targets  `make <target>` in docs exists in the Makefile of its context (MCP server skill ⇒ its Makefile, else
              the project root Makefile).
paths         template paths cited in docs (app/…, tests/…, scripts/…, deploy/…, src/backend/…) exist in a skill.
env-names     UPPER_SNAKE config names cited in docs and templates exist (Settings, env files, code, CI, Makefiles).
build-sync    the scaffold and MCP server Dockerfiles share python/uv images, stages, uv flags (no `chown -R`) and
              pin every base image by the same digest; `uv … --frozen` nowhere (`--locked`); docs cite the same python
              image; no CI steps copied into docs.
locks         the uv.lock each template ships matches its pyproject.toml (project entry, direct and dev dependencies
              with their specifiers): a changed dependency needs scripts/relock.sh. CI's `uv lock --check` is the
              authority; this catches it before any network.
allowlist     every exception in drift_allowlist.py is still needed.
"""

from __future__ import annotations

import ast
import fnmatch
import re
import sys
import tomllib
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

import drift_allowlist as allow

ROOT = Path(__file__).resolve().parents[2]
SKILLS = ROOT / "skills"
SCAFFOLD = SKILLS / "agentbase-build-scaffold"
ROOT_TPL = SCAFFOLD / "assets/root"
AGENT_CONFIG = SCAFFOLD / "assets/backend/app/config.py"
BACKEND_ENV = SCAFFOLD / "assets/backend/.env.example"
DEPLOY_ENV = ROOT_TPL / "env.deploy.example"
CI_YML = ROOT_TPL / "ci.yml"
ROOT_MAKEFILE = ROOT_TPL / "Makefile"
MCP_SKILL = SKILLS / "agentbase-build-mcp-server"
MCP_SERVER = MCP_SKILL / "assets/mcp_server"
FRONTEND = SKILLS / "agentbase-build-frontend/assets/frontend"
WEB = SKILLS / "agentbase-build-web/assets/web"  # Next.js web chat + BFF (src/web), checked like FRONTEND
# Each JS/TS template → the file that reads its config (named in the settings-env hint)
JS_TEMPLATES = {FRONTEND: "src/config/env.ts", WEB: "src/config/env.ts (NEXT_PUBLIC_*) or src/server/config.ts (BFF_*)"}
TRACING_SKILL = SKILLS / "agentbase-build-tracing/SKILL.md"
TRACING_PY = SKILLS / "agentbase-build-tracing/assets/backend/app/observability/tracing.py"
TRACE_SCHEMA = SKILLS / "agentbase-build-tracing/references/trace-schema.md"
DOCKERFILES = (SCAFFOLD / "assets/backend/Dockerfile", MCP_SERVER / "Dockerfile")
SECRET_PLACEHOLDER = "<set-in-deploy>"
ALLOWLIST = ".github/scripts/drift_allowlist.py"

# Where a generated project's root files come from: `copy_new|cp "$ROOT_TPL/<template>" "$TARGET/<path>"` in scaffold.sh
ROOT_MAP = dict(
    (dst, src)
    for src, dst in re.findall(
        r'(?:copy_new|cp)\s+"\$ROOT_TPL/([^"]+)"\s+"\$TARGET/([^"]+)"',
        (SCAFFOLD / "scripts/scaffold.sh").read_text(encoding="utf-8"),
    )
)


def rel(p: Path) -> str:
    return p.relative_to(ROOT).as_posix()


def read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def reporter(errors: list[str], check: str) -> Callable[[Path, int, str], None]:
    """err(path, line, message) appends `<file>:<line>: [<check>] <message>`."""

    def err(path: Path, line: int, msg: str) -> None:
        errors.append(f"{rel(path)}:{line}: [{check}] {msg}")

    return err


def line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


# Exception lists of drift_allowlist.py: an entry no check needs any more is reported, so the list stays honest.
# (DEPLOY_FORBIDDEN / LOCAL_ENV_FORBIDDEN are rules, not exceptions.)
EXCEPTION_LISTS = (
    "EXTERNAL_ENV",
    "EXTERNAL_ENV_PATTERNS",
    "NON_ENV_TOKENS",
    "TRACE_NOT_IN_TREE",
    "TRACE_DYNAMIC_NAMES",
    "MAKE_TARGET_EXCEPTIONS",
    "NON_TEMPLATE_PATHS",
    "NON_TEMPLATE_PATH_PATTERNS",
    "CI_SNIPPET_EXCEPTIONS",
)
_used: set[tuple[str, object]] = set()


def allowed(name: object, *lists: str) -> str | None:
    """Reason of the drift_allowlist.py entry covering `name` in one of `lists` (exact key, or fnmatch glob key),
    else None."""
    for list_name in lists:
        for key, reason in getattr(allow, list_name).items():
            glob = isinstance(key, str) and isinstance(name, str) and any(ch in key for ch in "*?[")
            if key == name or (glob and fnmatch.fnmatchcase(name, key)):  # type: ignore[arg-type]
                _used.add((list_name, key))
                return reason
    return None


def check_allowlist(errors: list[str]) -> None:
    err = reporter(errors, "allowlist")
    path = Path(allow.__file__)
    lines = read(path).splitlines()
    for list_name in EXCEPTION_LISTS:
        for key in getattr(allow, list_name):
            if (list_name, key) not in _used:
                needle = repr(key[0] if isinstance(key, tuple) else key)[1:-1]
                n = next((i for i, t in enumerate(lines, 1) if needle in t and not t.lstrip().startswith("#")), 1)
                err(path, n, f"{list_name}[{key!r}] is not needed any more (nothing matches it) — remove the entry")


def text_files(*bases: Path) -> list[Path]:
    """Text files under `bases` (each once, even when bases overlap)."""
    files = {
        p
        for base in bases
        for p in base.rglob("*")
        if p.is_file() and not {"node_modules", "__pycache__"} & set(p.parts) and p.suffix not in {".png", ".lock"}
    }
    return sorted(files)


def parse_py(path: Path) -> ast.Module:
    try:
        return ast.parse(read(path))
    except SyntaxError as e:  # the templates are Python 3.13 code, parsed here by the interpreter running this
        raise SystemExit(
            f"{rel(path)}:{e.lineno}: SyntaxError under Python {sys.version.split()[0]} — the drift "
            "checks parse the templates with this interpreter: use python3 >= 3.12 (CI: ubuntu-24.04's "
            "3.12), or a newer one if the templates adopted newer syntax"
        ) from e


def py_files(*bases: Path) -> Iterator[Path]:
    for base in bases:
        yield from (p for p in sorted(base.rglob("*.py")) if "__pycache__" not in p.parts)


def doc_files() -> list[Path]:
    """The docs of this repo (CHANGELOG.md is history: it names old things on purpose)."""
    return [
        ROOT / "README.md",
        *sorted(SKILLS.glob("*/SKILL.md")),
        *sorted(SKILLS.glob("*/references/*.md")),
        ROOT_TPL / "README.md",  # the generated project's README
    ]


FENCE_RE = re.compile(r"^\s*(```|~~~)")
CODE_SPAN_RE = re.compile(r"`([^`\n]+)`")


@dataclass
class MdLine:
    n: int
    text: str
    in_fence: bool

    def code(self) -> list[str]:
        """Code on this line: the whole line inside a fence, else its inline code spans."""
        return [self.text] if self.in_fence else CODE_SPAN_RE.findall(self.text)


def md_lines(path: Path) -> list[MdLine]:
    out, fence = [], False
    for n, line in enumerate(read(path).splitlines(), start=1):
        if FENCE_RE.match(line):
            fence = not fence
            continue
        out.append(MdLine(n, line, fence))
    return out


def fenced_blocks(path: Path) -> list[tuple[int, list[tuple[int, str]]]]:
    """(line of the opening fence, [(n, line)…]) for every fenced block."""
    blocks, cur, start = [], None, 0
    for n, line in enumerate(read(path).splitlines(), start=1):
        if FENCE_RE.match(line):
            if cur is None:
                cur, start = [], n
            else:
                blocks.append((start, cur))
                cur = None
        elif cur is not None:
            cur.append((n, line))
    return blocks


# --------------------------------------------------------------------------------------------- env & settings model
ENV_LINE_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
UPPER_RE = r"[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+"


def env_keys(path: Path) -> dict[str, tuple[int, str]]:
    """Active KEY=VALUE lines → (line, value)."""
    out: dict[str, tuple[int, str]] = {}
    for n, line in enumerate(read(path).splitlines(), start=1):
        if m := ENV_LINE_RE.match(line):
            out.setdefault(m.group(1), (n, m.group(2)))
    return out


def settings_fields(path: Path, cls: str = "Settings") -> dict[str, int]:
    """Env var name → line of each field of a pydantic-settings class (env_prefix and aliases applied)."""
    tree = parse_py(path)
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == cls:
            break
    else:
        raise SystemExit(f"{rel(path)}: class {cls} not found — update check_drift.py")
    prefix = ""
    out: dict[str, int] = {}
    for stmt in node.body:
        target = getattr(stmt, "target", None) or (getattr(stmt, "targets", None) or [None])[0]
        if not isinstance(target, ast.Name):
            continue
        if target.id == "model_config":
            for kw in getattr(stmt.value, "keywords", []):
                if kw.arg == "env_prefix" and isinstance(kw.value, ast.Constant):
                    prefix = kw.value.value
            continue
        if not isinstance(stmt, ast.AnnAssign) or "ClassVar" in ast.unparse(stmt.annotation):
            continue
        env = None
        if isinstance(stmt.value, ast.Call) and ast.unparse(stmt.value.func).endswith("Field"):
            for kw in stmt.value.keywords:  # validation_alias/alias bypass env_prefix in pydantic-settings
                if kw.arg in ("validation_alias", "alias") and isinstance(kw.value, ast.Constant):
                    env = str(kw.value.value).upper()
        out[env or f"{prefix}{target.id}".upper()] = stmt.lineno
    return out


@dataclass
class EnvWorld:
    """Every env/config name the repo knows, by source."""

    agent: dict[str, int]
    mcp: dict[str, int]
    code_reads: dict[str, tuple[Path, int]]  # os.getenv / os.environ / process.env in skill code
    backend_reads: set[str]  # the subset read by the agent backend templates
    json_refs: set[str]  # ${VAR} in the JSON config templates (mcp_servers.json, a2a_agents.json)
    env_files: dict[Path, dict[str, tuple[int, str]]]
    ci_store: set[str]  # secrets.* / vars.* of the generated ci.yml (GitHub secrets and variables)
    ci: set[str]  # every other name in ci.yml: env keys, shell variables
    other: set[str]  # Makefile / Dockerfile / manifest template variables
    constants: set[str]  # UPPER names assigned in Python/TS code (constants, not env vars)

    def known(self, name: str) -> bool:
        return (
            name in self.agent
            or name in self.mcp
            or name in self.code_reads
            or name in self.json_refs
            or name in self.ci
            or name in self.ci_store
            or name in self.other
            or any(name in keys for keys in self.env_files.values())
            or allowed(name, "EXTERNAL_ENV", "EXTERNAL_ENV_PATTERNS") is not None
        )


def _env_reads_py(path: Path) -> Iterator[tuple[str, int]]:
    for node in ast.walk(parse_py(path)):
        arg = None
        if isinstance(node, ast.Call) and node.args:
            func = ast.unparse(node.func)
            if func in ("os.getenv", "getenv", "os.environ.get", "environ.get", "os.environ.setdefault"):
                arg = node.args[0]
        elif isinstance(node, ast.Subscript) and ast.unparse(node.value) in ("os.environ", "environ"):
            arg = node.slice
        elif isinstance(node, ast.Compare) and any(ast.unparse(c) == "os.environ" for c in node.comparators):
            arg = node.left
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            yield arg.value, node.lineno


def _py_constants(path: Path) -> set[str]:
    out = set()
    for node in ast.walk(parse_py(path)):
        targets = node.targets if isinstance(node, ast.Assign) else [getattr(node, "target", None)]
        for t in targets:
            if isinstance(t, ast.Name) and re.fullmatch(r"_?[A-Z][A-Z0-9_]*", t.id):
                out.add(t.id.lstrip("_"))
    return out


_WORLD: EnvWorld | None = None


def env_world() -> EnvWorld:
    global _WORLD
    if _WORLD is not None:
        return _WORLD
    code_reads: dict[str, tuple[Path, int]] = {}
    backend_reads: set[str] = set()
    constants: set[str] = set()
    for py in py_files(SKILLS):
        for name, n in _env_reads_py(py):
            code_reads.setdefault(name, (py, n))
            if "/assets/backend/" in py.as_posix():
                backend_reads.add(name)
        constants |= _py_constants(py)
    for ts in [p for base in JS_TEMPLATES for p in (*base.rglob("*.ts"), *base.rglob("*.tsx"))]:
        text = read(ts)
        for m in re.finditer(r"process\.env\.([A-Z][A-Z0-9_]*)", text):
            line = text[text.rfind("\n", 0, m.start()) + 1 : m.start()].lstrip()
            if not line.startswith(("//", "*", "/*")):  # comments describe the pattern (process.env.EXPO_PUBLIC_X)
                code_reads.setdefault(m.group(1), (ts, line_of(text, m.start())))
        constants |= set(re.findall(r"\bconst\s+([A-Z][A-Z0-9_]*)\s*[:=]", text))
    json_refs = {m for p in SKILLS.glob("*/assets/**/*.json") for m in re.findall(r"\$\{([A-Z][A-Z0-9_]*)\}", read(p))}
    ci_text = read(CI_YML)
    ci_store = set(re.findall(r"\b(?:secrets|vars)\.([A-Z][A-Z0-9_]*)", ci_text))
    ci = set(re.findall(r"^\s*([A-Z][A-Z0-9_]*):\s", ci_text, re.M))  # env: keys
    ci |= set(re.findall(r"(?<![\w$])([A-Z][A-Z0-9_]*)=", ci_text))  # shell assignments, KEY= in >> $GITHUB_ENV
    ci |= set(re.findall(r"\$\{?([A-Z][A-Z0-9_]*)", ci_text))  # shell references
    other: set[str] = set()
    for mk in (ROOT_MAKEFILE, MCP_SERVER / "Makefile"):
        text = read(mk)
        other |= set(re.findall(r"^([A-Z][A-Z0-9_]*)\s*[?:+]?=", text, re.M))
        other |= set(re.findall(r"\$\(([A-Z][A-Z0-9_]*)\)", text))
        other |= set(re.findall(r"\b([A-Z][A-Z0-9_]*)=\$", text))  # MCP_URL=$(URL) on a recipe line
    for df in DOCKERFILES:
        text = read(df)
        other |= set(re.findall(r"^ARG\s+([A-Z][A-Z0-9_]*)", text, re.M))
        for env_line in re.findall(r"^ENV\s+(.*(?:\\\n.*)*)", text, re.M):
            other |= set(re.findall(r"([A-Z][A-Z0-9_]*)=", env_line))
    other |= set(re.findall(r"\$\{([A-Z][A-Z0-9_]*)\}", read(ROOT_TPL / "agent.yaml.tpl")))
    for sh in SKILLS.glob("*/scripts/*.sh"):  # variables of the skill scripts, incl. env overrides (EXPO_SDK=<n>)
        other |= set(re.findall(r"\$\{?([A-Z][A-Z0-9_]*)", read(sh)))
    env_files = {
        p: env_keys(p)
        for p in (BACKEND_ENV, DEPLOY_ENV, MCP_SERVER / ".env.example", *(t / ".env.example" for t in JS_TEMPLATES))
    }
    _WORLD = EnvWorld(
        agent=settings_fields(AGENT_CONFIG),
        mcp=settings_fields(MCP_SERVER / "settings.py"),
        code_reads=code_reads,
        backend_reads=backend_reads,
        json_refs=json_refs,
        env_files=env_files,
        ci_store=ci_store,
        ci=ci,
        other=other,
        constants=constants,
    )
    return _WORLD


# ------------------------------------------------------------------------------------------------ 1. settings-env
def check_settings_env(errors: list[str]) -> None:
    err = reporter(errors, "settings-env")
    w = env_world()
    local, deploy = w.env_files[BACKEND_ENV], w.env_files[DEPLOY_ENV]
    n0 = len(errors)

    for env, n in w.agent.items():
        if env not in local:
            err(
                AGENT_CONFIG,
                n,
                f"setting {env} is not in {rel(BACKEND_ENV)} — add an active "
                f"`{env}=<local default>` line in the matching section (that file lists EVERY variable; an empty "
                "value means the default of app/config.py)",
            )
    for key, (n, _) in local.items():
        if reason := allowed(key, "LOCAL_ENV_FORBIDDEN"):
            err(BACKEND_ENV, n, f"{key} must not be set in .env.example: {reason} — remove the line")
        elif not (
            key in w.agent
            or key in w.backend_reads
            or key in w.json_refs
            or allowed(key, "EXTERNAL_ENV", "EXTERNAL_ENV_PATTERNS")
        ):
            err(
                BACKEND_ENV,
                n,
                f"{key} is read by nothing: not a field of {rel(AGENT_CONFIG)}, "
                "not read with os.getenv in a backend template, not a ${VAR} of mcp_servers.json/a2a_agents.json "
                f"— remove it, add the Settings field, or allowlist it in {ALLOWLIST} (EXTERNAL_ENV) with the "
                "component that reads it",
            )
    for key, (n, _value) in deploy.items():
        if reason := allowed(key, "DEPLOY_FORBIDDEN"):
            err(DEPLOY_ENV, n, f"{key} must not be in a deploy env file: {reason} — remove it")
        elif key not in local:
            err(
                DEPLOY_ENV,
                n,
                f"{key} is not in {rel(BACKEND_ENV)} (the full list of variables) — "
                "add it there with its local default first, or remove it here",
            )
    if deploy.get("APP_ENV", (0, ""))[1] != "__APP_ENV__":
        err(
            DEPLOY_ENV,
            deploy.get("APP_ENV", (1, ""))[0],
            "APP_ENV must be `APP_ENV=__APP_ENV__` (scaffold.sh renders dev/staging/prod; the Runtime refuses local)",
        )
    if deploy.get("AUTH_MODE", (0, ""))[1] == "none":
        err(DEPLOY_ENV, deploy["AUTH_MODE"][0], "AUTH_MODE=none is refused outside APP_ENV=local")

    ci_text = read(CI_YML)
    m = re.search(r"render_runtime_spec\.py.*?--secrets\s+([A-Z0-9_,]+)", ci_text, re.S)
    ci_secrets = set(m.group(1).split(",")) if m else set()
    if not m:
        err(CI_YML, 1, "no `render_runtime_spec.py … --secrets A,B` call found — update check_drift.py")
    for key, (n, value) in deploy.items():
        if value == SECRET_PLACEHOLDER and key not in ci_secrets:
            err(
                DEPLOY_ENV,
                n,
                f"{key}={SECRET_PLACEHOLDER} but CI only fills the `--secrets` of "
                f"render_runtime_spec.py in {rel(CI_YML)} (line {line_of(ci_text, m.start(1)) if m else '?'}) — "
                "add it there + to that step's env from `secrets.`, or make it optional (empty value)",
            )
    for key in sorted(ci_secrets - set(local)):
        err(
            CI_YML,
            line_of(ci_text, m.start(1)),
            f"--secrets names {key}, which is not a variable of {rel(BACKEND_ENV)} — fix the name or add the variable",
        )
    export_re = r"env_file\.py export\b(?:[^\n]|\\\n)*?--keys\s+'([^']+)'(?:\s+--exclude\s+'([^']+)')?"
    for m in re.finditer(export_re, ci_text):
        for glob in (g.strip() for g in ",".join(filter(None, m.groups())).split(",")):
            if glob and not any(fnmatch.fnmatchcase(k, glob) for k in w.agent):
                err(
                    CI_YML,
                    line_of(ci_text, m.start()),
                    f"`env_file.py export` pattern {glob} matches "
                    f"no Settings field of {rel(AGENT_CONFIG)} — fix or drop it",
                )
        # The eval SKILL describes the gate in prose (no copy of the job): it must name what the job exports
        eval_skill = SKILLS / "agentbase-build-eval/SKILL.md"
        eval_text = read(eval_skill)
        cited = {t for span in CODE_SPAN_RE.findall(eval_text) for t in re.split(r"[\s,]+", span)}
        at = eval_text.find("env_file.py export")
        for glob in (g.strip() for g in m.group(1).split(",")):
            if glob not in cited:
                err(
                    eval_skill,
                    line_of(eval_text, at) if at >= 0 else 1,
                    f"the eval job of {rel(CI_YML)}:{line_of(ci_text, m.start())} exports `{glob}` from the evaluated "
                    "environment's config, but this SKILL's CI description does not name it — add it to that list",
                )

    # Every GitHub secret / variable the workflow reads is listed in its header comment (what to configure)
    header = "\n".join(t for t in ci_text.splitlines() if t.startswith("#"))
    for m in re.finditer(r"\b(secrets|vars)\.([A-Z][A-Z0-9_]*)", ci_text):
        if not re.search(rf"\b{m.group(2)}\b", header):
            err(
                CI_YML,
                line_of(ci_text, m.start()),
                f"`{m.group(0)}` is used but not listed in the header comment of this workflow (the list of secrets "
                "and variables to configure) — add it under the right scope (repository or GitHub Environment)",
            )

    # MCP server template: settings.py (env_prefix MCP_) ↔ its .env.example
    mcp_env_path = MCP_SERVER / ".env.example"
    mcp_env = w.env_files[mcp_env_path]
    for env, n in w.mcp.items():
        if env not in mcp_env:
            err(
                MCP_SERVER / "settings.py",
                n,
                f"setting {env} is not in {rel(mcp_env_path)} — add "
                f"`{env}=<default>` with a comment (an empty value means the default)",
            )
    for key, (n, _) in mcp_env.items():
        if key not in w.mcp and not allowed(key, "EXTERNAL_ENV", "EXTERNAL_ENV_PATTERNS"):
            err(
                mcp_env_path,
                n,
                f"{key} is not a field of {rel(MCP_SERVER / 'settings.py')} — remove it or add the field",
            )
    # JS/TS templates (Expo frontend, Next.js web): process.env.* ↔ their .env.example
    fe_keys = 0
    for tpl, reader in JS_TEMPLATES.items():
        fe_env_path = tpl / ".env.example"
        fe_env = w.env_files[fe_env_path]
        fe_keys += len(fe_env)
        # code_reads keeps the first reader of a name: match by the template's own files
        fe_reads = {
            m.group(1): (ts, line_of(read(ts), m.start()))
            for ts in (*tpl.rglob("*.ts"), *tpl.rglob("*.tsx"))
            for m in re.finditer(r"process\.env\.([A-Z][A-Z0-9_]*)", read(ts))
            if m.group(1) in w.code_reads
        }
        for key, (path, n) in fe_reads.items():
            if key not in fe_env and key != "NODE_ENV":
                err(path, n, f"{key} is read here but missing from {rel(fe_env_path)} — add it with a comment")
        for key, (n, _) in fe_env.items():
            if key not in fe_reads:
                err(
                    fe_env_path,
                    n,
                    f"{key} is not read by the {tpl.name} template (process.env.{key}) — remove it or read it in {reader}",
                )
    if len(errors) == n0:
        print(
            f"ok  settings-env: {len(w.agent)} agent settings, {len(local)}/{len(deploy)} keys in "
            f".env.example/env.deploy.example, {len(w.mcp)} MCP server settings, {fe_keys} frontend/web keys"
        )


# ------------------------------------------------------------------------------------------------- 2. trace-names
OBS_TYPES = set("agent tool chain retriever evaluator embedding guardrail generation span event".split())
_SEG = r"(?:<[A-Za-z_]+>|[a-z0-9_]+)"
NAME = rf"(?:<[A-Za-z_]+>|[a-z][a-z0-9_]*)(?:\.{_SEG})*"
NAME_FULL_RE = re.compile(rf"^{NAME}$")
NAME_IN_TEXT_RE = re.compile(rf"(?<![\w.<>/-]){NAME}(?![\w<>-])")
FILE_EXT_RE = re.compile(r"\.(?:py|ya?ml|json|jsonl|md|toml|tpl|sh|ts|tsx|txt|example|lock)$")


def shape(name: str) -> str:
    """Placeholders are interchangeable: f"identity.{kind}" ⇔ `identity.<kind>` ⇔ identity.<*>."""
    return re.sub(r"<[^<>]*>", "<*>", name)


def shape_matches(pattern_shape: str, concrete: str) -> bool:
    rx = "^" + re.escape(pattern_shape).replace(re.escape("<*>"), r"[^.]+") + "$"
    return re.match(rx, concrete) is not None


@dataclass
class Emitted:
    kind: str  # obs | score | trace
    scope: str  # app (one /invocations turn) | eval (evals/, its own traces)
    types: set[str] = field(default_factory=set)
    sites: list[tuple[Path, int]] = field(default_factory=list)


def chain_types() -> list[tuple[str, str]]:
    for node in ast.walk(parse_py(TRACING_PY)):
        target = getattr(node, "target", None) or (getattr(node, "targets", None) or [None])[0]
        if isinstance(target, ast.Name) and target.id == "_CHAIN_TYPES":
            return [tuple(e.value for e in t.elts) for t in node.value.elts]  # type: ignore[union-attr,misc]
    raise SystemExit(f"{rel(TRACING_PY)}: _CHAIN_TYPES not found — update check_drift.py")


def emitted_names(errors: list[str]) -> dict[str, Emitted]:
    err = reporter(errors, "trace-names")
    ctypes = chain_types()
    out: dict[str, Emitted] = {}
    files = [
        (p, "app") for b in SKILLS.glob("*/assets/backend") for p in [*py_files(b / "app"), b / "main.py"] if p.exists()
    ]
    files += [(p, "eval") for b in SKILLS.glob("*/assets/backend/evals") for p in py_files(b)]

    def emit(name: str, kind: str, typ: str | None, scope: str, path: Path, n: int) -> None:
        e = out.setdefault(shape(name), Emitted(kind, scope))
        if scope == "app":
            e.scope = "app"
        if typ:
            e.types.add(typ)
        e.sites.append((path, n))

    def run_type(name: str) -> str:
        for prefix, typ in ctypes:
            if name.startswith(prefix):
                return typ
        return "generation" if name.endswith(".generate") else "chain"  # get_llm names generations `….generate`

    def scan(path: Path, scope: str) -> None:
        tree = parse_py(path)
        assigns: dict[str, ast.AST] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                assigns.setdefault(node.targets[0].id, node.value)

        def lit(node: ast.AST | None, depth: int = 0) -> str | None:
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                return node.value
            if isinstance(node, ast.JoinedStr):
                return "".join(v.value if isinstance(v, ast.Constant) else "<*>" for v in node.values)
            if isinstance(node, ast.Name) and depth < 3:
                return lit(assigns.get(node.id), depth + 1)
            return None

        def need(node: ast.AST | None, what: str, at: int) -> str | None:
            name = lit(node)
            # evals/: run_experiment(run_name=…) names a dataset run, not a span
            if name is None and scope == "app" and node is not None:
                expr = ast.unparse(node)
                if not allowed((rel(path), expr), "TRACE_DYNAMIC_NAMES"):
                    err(
                        path,
                        at,
                        f"{what} is `{expr}`, not a string literal or f-string — the trace-name check cannot see "
                        "it: use one (names are an API: stable, `<domain>.<action>`), or allowlist "
                        f"({rel(path)!r}, {expr!r}) in {ALLOWLIST} TRACE_DYNAMIC_NAMES and document the names",
                    )
            return name

        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = ast.unparse(node.func)
                kw = {k.arg: k.value for k in node.keywords if k.arg}
                first = node.args[0] if node.args else None
                if func in ("tracing.step", "tracing.event", "tracing.score_trace"):
                    if (name := need(first or kw.get("name"), f"name of {func}()", node.lineno)) is None:
                        continue
                    if func == "tracing.score_trace":
                        emit(name, "score", None, scope, path, node.lineno)
                    else:
                        typ = "event" if func == "tracing.event" else (lit(kw.get("as_type")) or "span")
                        emit(name, "obs", typ, scope, path, node.lineno)
                elif func.endswith(".add_node") and isinstance(first, ast.Constant):
                    emit(first.value, "obs", "chain", scope, path, node.lineno)  # LangGraph node = chain
                elif func.split(".")[-1] == "get_llm" and isinstance(first, ast.Constant):
                    emit(f"llm.{first.value}", "obs", "chain", scope, path, node.lineno)
                    emit(f"llm.{first.value}.generate", "obs", "generation", scope, path, node.lineno)
                elif func == "RunnableLambda" and "name" in kw:
                    if name := need(kw["name"], "name= of RunnableLambda()", node.lineno):
                        emit(name, "obs", run_type(name), scope, path, node.lineno)
                elif func.endswith("start_as_current_observation") and (name := lit(kw.get("name"))):
                    emit(name, "obs", lit(kw.get("as_type")) or "span", scope, path, node.lineno)
                elif func.endswith("create_score") and (name := lit(kw.get("name"))):
                    emit(name, "score", None, scope, path, node.lineno)
                if "run_name" in kw and (name := need(kw["run_name"], "run_name=", node.lineno)):
                    emit(name, "obs", run_type(name), scope, path, node.lineno)
                if "trace_name" in kw and (name := need(kw["trace_name"], "trace_name=", node.lineno)):
                    emit(name, "trace", None, scope, path, node.lineno)
            elif isinstance(node, ast.Dict):
                for k, v in zip(node.keys, node.values, strict=True):
                    if isinstance(k, ast.Constant) and k.value == "run_name":
                        if name := need(v, 'config "run_name"', node.lineno):
                            emit(name, "obs", run_type(name), scope, path, node.lineno)
            elif isinstance(node, ast.Assign):
                t = node.targets[0]
                if isinstance(t, ast.Subscript) and isinstance(t.slice, ast.Constant) and t.slice.value == "run_name":
                    if name := need(node.value, 'config["run_name"]', node.lineno):
                        emit(name, "obs", run_type(name), scope, path, node.lineno)

    for path, scope in files:
        scan(path, scope)
    return out


@dataclass
class DocNode:
    name: str
    typ: str | None
    n: int
    score: bool = False


def tree_nodes(lines: list[tuple[int, str]]) -> tuple[list[DocNode], set[str]]:
    """Structured names of a drawn trace tree (root, `├─ x`, `x [type]`, `→ x`, `scores:` line) + every name-like
    token of the block (for "is this emitted name mentioned anywhere in the tree")."""
    nodes: list[DocNode] = []
    tokens: set[str] = set()
    for i, (n, line) in enumerate(lines):
        tokens |= {shape(t) for t in NAME_IN_TEXT_RE.findall(line)}
        if line.strip().startswith("scores:"):
            for part in line.strip()[len("scores:") :].split("·"):
                if m := NAME_IN_TEXT_RE.search(part):
                    nodes.append(DocNode(m.group(0), None, n, score=True))
            continue
        found: dict[str, str | None] = {}
        heads = [m.group(1) for m in re.finditer(r"[├└]─+\s*(\S+)", line)]
        if i == 0:
            heads.append(line.split()[0])
        heads += re.findall(rf"→\s*({NAME})", line)
        for h in heads:
            if NAME_FULL_RE.match(h):
                found.setdefault(h, None)
        for m in re.finditer(rf"(?<![\w.<>/-])({NAME})\s+\[(\w+)\s*[],]", line):
            if m.group(2) in OBS_TYPES:
                found[m.group(1)] = m.group(2)
        for name, typ in found.items():
            if not re.fullmatch(r"<[^<>]+>", name):  # `<tool_name> [tool]` = the tools themselves
                nodes.append(DocNode(name, typ, n))
    return nodes, tokens


def skill_tree() -> list[tuple[int, str]]:
    lines = read(TRACING_SKILL).splitlines()
    head = next((i for i, t in enumerate(lines) if t.startswith("## ") and "trace tree" in t.lower()), None)
    if head is None:
        raise SystemExit(f"{rel(TRACING_SKILL)}: no '## … trace tree' heading — update check_drift.py")
    start = next(i for i in range(head, len(lines)) if FENCE_RE.match(lines[i]))
    end = next(i for i in range(start + 1, len(lines)) if FENCE_RE.match(lines[i]))
    return [(i + 1, lines[i]) for i in range(start + 1, end)]


def docstring_tree() -> list[tuple[int, str]]:
    lines = read(TRACING_PY).splitlines()
    start = next((i for i, t in enumerate(lines) if t.strip().startswith("agent.invoke")), None)
    if start is None:
        raise SystemExit(f"{rel(TRACING_PY)}: docstring tree (line starting with agent.invoke) not found")
    end = next((i for i in range(start, len(lines)) if not lines[i].strip()), len(lines))
    return [(i + 1, lines[i]) for i in range(start, end)]


def check_trace_names(errors: list[str]) -> None:
    err = reporter(errors, "trace-names")
    n0 = len(errors)
    emitted = emitted_names(errors)
    ctypes = chain_types()
    patterns = [s for s in emitted if "<*>" in s]

    def is_emitted(name: str, kind: str | None = None) -> bool:
        s = shape(name)
        if s in emitted and (kind is None or emitted[s].kind == kind):
            return True
        return "<*>" not in s and any(
            shape_matches(p, s) and (kind is None or emitted[p].kind == kind) for p in patterns
        )

    tree = skill_tree()
    nodes, tokens = tree_nodes(tree)
    tree_where = f"the Step 2 tree of {rel(TRACING_SKILL)} (lines {tree[0][0]}–{tree[-1][0]})"
    schema_tokens = {
        shape(t) for span in CODE_SPAN_RE.findall(read(TRACE_SCHEMA)) for t in NAME_IN_TEXT_RE.findall(span)
    }
    # 1) emitted ⇒ documented
    for s, e in sorted(emitted.items()):
        path, n = e.sites[0]
        if e.kind == "trace":
            if s not in schema_tokens:
                err(
                    path,
                    n,
                    f"trace name `{s}` is not in {rel(TRACE_SCHEMA)} (Trace-level attributes) — document it there",
                )
            continue
        if e.scope != "app" or s in tokens or allowed(s, "TRACE_NOT_IN_TREE"):
            continue
        what = "score" if e.kind == "score" else f"[{'/'.join(sorted(e.types))}]"
        err(
            path,
            n,
            f"`{s}` {what} is emitted here but missing from {tree_where} — add "
            f"`{s} [{next(iter(e.types), 'event')}]` under its parent node"
            + (" (or the `scores:` line)" if e.kind == "score" else "")
            + f", or allowlist it in {ALLOWLIST} (TRACE_NOT_IN_TREE) with a reason",
        )
    # 2) documented ⇒ emitted, with the same observation type
    tree_types: dict[str, str | None] = {}
    for node in nodes:
        s = shape(node.name)
        tree_types.setdefault(s, node.typ)
        if node.score:
            if not is_emitted(node.name, "score"):
                err(
                    TRACING_SKILL,
                    node.n,
                    f"score `{node.name}` is documented but no template calls "
                    "score_trace / create_score with it — remove it or fix the name",
                )
            continue
        if not is_emitted(node.name, "obs"):
            err(
                TRACING_SKILL,
                node.n,
                f"`{node.name}` is documented in the trace tree but no template "
                "emits it (tracing.step/event, run_name, graph node, get_llm) — stale doc: remove or rename it",
            )
        elif node.typ and s in emitted and emitted[s].types != {node.typ}:
            sites = ", ".join(f"{rel(p)}:{n}" for p, n in emitted[s].sites)
            err(
                TRACING_SKILL,
                node.n,
                f"`{node.name}` is documented as [{node.typ}] but the code emits "
                f"it as [{'/'.join(sorted(emitted[s].types))}] ({sites}) — one name, one observation type: align "
                "the doc or the as_type",
            )
    # 3) tracing.py's docstring tree ⊆ the SKILL.md tree (same types)
    for node in tree_nodes(docstring_tree())[0]:
        s = shape(node.name)
        if s not in tree_types:
            err(
                TRACING_PY,
                node.n,
                f"docstring tree names `{node.name}`, which {tree_where} lacks — "
                "the SKILL.md tree is the full one: add it there (or fix this name)",
            )
        elif node.typ and tree_types[s] and node.typ != tree_types[s]:
            err(
                TRACING_PY,
                node.n,
                f"docstring tree types `{node.name}` as [{node.typ}], the SKILL.md "
                f"tree as [{tree_types[s]}] — make them agree",
            )
    # 4) references/trace-schema.md: cited dotted names are emitted; every _CHAIN_TYPES entry is in its type row
    domains = {s.split(".")[0] for s in emitted if "." in s} | {p.split(".")[0] for p, _ in ctypes}
    documented_ctypes = [p for p, _ in ctypes]
    for ml in md_lines(TRACE_SCHEMA):
        for span in ml.code():
            for tok in NAME_IN_TEXT_RE.findall(span):
                if "." not in tok or FILE_EXT_RE.search(tok):
                    continue
                if tok.split(".")[0] not in domains and not tok.startswith("<"):
                    continue
                parts = tok.split(".")
                prefixes = [".".join(parts[:i]) for i in range(len(parts), 1, -1)]  # tools.collect.output → …
                if any(is_emitted(p) for p in prefixes) or any(tok.startswith(p) for p in documented_ctypes):
                    continue
                err(
                    TRACE_SCHEMA,
                    ml.n,
                    f"`{tok}` is cited but no template emits it — fix the name or drop it",
                )
    rows = {m.group(1): m.group(2) for m in re.finditer(r"^\|\s*`(\w+)`\s*\|([^|]*)\|", read(TRACE_SCHEMA), re.M)}
    for prefix, typ in ctypes:
        if not any(t.startswith(prefix) for t in CODE_SPAN_RE.findall(rows.get(typ, ""))):
            err(
                TRACING_PY,
                1,
                f"_CHAIN_TYPES maps `{prefix}` to [{typ}] but the `{typ}` row of "
                f"{rel(TRACE_SCHEMA)}'s observation-type table does not cite it — add it there",
            )
    # 5) trace metadata: the keys of service._trace_meta() ↔ the `metadata` row of trace-schema.md
    service = SCAFFOLD / "assets/backend/app/service.py"
    fn = next(
        (f for f in ast.walk(parse_py(service)) if isinstance(f, ast.FunctionDef) and f.name == "_trace_meta"), None
    )
    ret = next((r.value for r in ast.walk(fn) if isinstance(r, ast.Return)), None) if fn else None
    if not isinstance(ret, ast.Dict):
        err(service, 1, "_trace_meta() returning a dict literal not found — update check_drift.py")
    else:
        keys: set[str] = set()
        for k, v in zip(ret.keys, ret.values, strict=True):
            if k is None and isinstance(v, ast.DictComp):  # **{f"llm_model_{t}": … for t in TIERS}
                k = v.key
            if isinstance(k, ast.Constant):
                keys.add(k.value)
            elif isinstance(k, ast.JoinedStr):
                keys.add("".join(x.value if isinstance(x, ast.Constant) else "<*>" for x in k.values))
        text = read(TRACE_SCHEMA)
        row = re.search(r"^\|\s*`metadata`\s*\|([^|]*)\|", text, re.M)
        doc = {shape(t.strip(" `")) for t in row.group(1).split(",")} if row else set()
        n = line_of(text, row.start()) if row else 1
        for k in sorted(keys - doc):
            err(
                TRACE_SCHEMA,
                n,
                f"trace metadata key `{k}` (set by _trace_meta() in {rel(service)}) is "
                "missing from the `metadata` row — add it",
            )
        for k in sorted(doc - keys):
            err(
                TRACE_SCHEMA,
                n,
                f"`metadata` row lists `{k}`, which _trace_meta() in {rel(service)} "
                "does not set — remove or rename it (placeholders as `<tier>`)",
            )
    if len(errors) == n0:
        n_app = sum(1 for e in emitted.values() if e.scope == "app")
        print(
            f"ok  trace-names: {n_app} names emitted per turn (+{len(emitted) - n_app} by evals/) ↔ "
            f"{len(nodes)} tree entries, docstring tree, trace-schema.md"
        )


# ------------------------------------------------------------------------------------------------ 3. make-targets
# `make docker-build TAG=x`, `make TAG=x docker-build`, `make setup/dev/test` (a list of targets)
MAKE_RE = re.compile(r"(?<![\w-])make\s+(?:[A-Z][A-Z0-9_]*=\S+\s+)*([a-z][a-z0-9_-]*(?:/[a-z][a-z0-9_-]*)*)(?![\w-])")


def makefile_targets(path: Path) -> set[str]:
    return set(re.findall(r"^([A-Za-z0-9][A-Za-z0-9_.-]*)\s*:(?!=)", read(path), re.M))


def check_make_targets(errors: list[str]) -> None:
    err = reporter(errors, "make-targets")
    n0 = len(errors)
    makefiles = {
        "project root": (ROOT_MAKEFILE, makefile_targets(ROOT_MAKEFILE)),
        "MCP server": (MCP_SERVER / "Makefile", makefile_targets(MCP_SERVER / "Makefile")),
    }
    every = set().union(*(t for _, t in makefiles.values()))
    count = 0

    def verify(path: Path, n: int, target: str) -> None:
        nonlocal count
        count += 1
        ctx = "MCP server" if path.is_relative_to(MCP_SKILL) else "project root"
        mk, targets = makefiles[ctx]
        if target in targets or allowed((rel(path), target), "MAKE_TARGET_EXCEPTIONS"):
            return
        elsewhere = [k for k, (_, t) in makefiles.items() if target in t]
        hint = (
            (
                f" (it exists in the {elsewhere[0]} Makefile — say which project the command runs in, or "
                f"allowlist ({rel(path)!r}, {target!r}) in {ALLOWLIST} MAKE_TARGET_EXCEPTIONS)"
            )
            if elsewhere
            else ""
        )
        err(
            path,
            n,
            f"`make {target}`: no target `{target}` in {rel(mk)} ({ctx} Makefile) — add the "
            f"target or fix the command{hint}",
        )

    for doc in doc_files():
        for ml in md_lines(doc):
            for code in ml.code():
                for m in MAKE_RE.finditer(code):
                    for target in m.group(1).split("/"):
                        verify(doc, ml.n, target)
    # Templates and scripts: prose ("make it", "make the") is skipped — only real targets or `make x-y` are checked
    for path in text_files(SKILLS):
        if path.suffix == ".md" or path.name.endswith(".jsonl"):
            continue
        for n, line in enumerate(read(path).splitlines(), start=1):
            for m in MAKE_RE.finditer(line):
                for t in m.group(1).split("/"):
                    if t in every or "-" in t or f"`make {m.group(1)}" in line:
                        verify(path, n, t)
    if len(errors) == n0:
        print(f"ok  make-targets: {count} `make` mentions resolve in {len(makefiles)} Makefiles")


# ------------------------------------------------------------------------------------------------------- 4. paths
PATH_EXTS = ("py", "ts", "tsx", "json", "jsonl", "md", "sh", "yml", "yaml", "tpl", "toml", "example", "txt")
PATH_DIRS = ("app", "tests", "scripts", "deploy", "evals", "src", ".github", "assets", "skills")
EXT_RE = re.compile(rf"\.(?:{'|'.join(PATH_EXTS)})$")
# A file path in prose (outside code): starts with a template dir and ends with a known extension
BARE_PATH_RE = re.compile(rf"(?<![\w./<>{{}}*-])(?:{'|'.join(map(re.escape, PATH_DIRS))})/[\w./-]*\w")


def resolve_template_path(p: str) -> bool:
    """True if `p` (as written in a doc, relative to a generated project, a skill or this repo) exists."""
    skills = [s for s in sorted(SKILLS.iterdir()) if s.is_dir()]
    if allowed(p.rstrip("/"), "NON_TEMPLATE_PATHS", "NON_TEMPLATE_PATH_PATTERNS"):
        return True
    if p.startswith("src/"):  # generated project: src/<sub> ← assets/<sub> (backend, frontend, web)
        _, sub, *rest = p.split("/", 2) + [""]
        if sub == "":
            return True  # src/ itself
        if sub not in ("backend", "frontend", "web"):
            return False
        p = rest[0]
        if not p:
            return True
        if allowed(p.rstrip("/"), "NON_TEMPLATE_PATHS", "NON_TEMPLATE_PATH_PATTERNS"):
            return True
        bases = [s / "assets" / sub for s in skills]
    else:
        if p in ROOT_MAP:
            return (ROOT_TPL / ROOT_MAP[p]).exists()
        if any(k.startswith(p.rstrip("/") + "/") for k in ROOT_MAP):  # deploy/, .github/workflows/
            return True
        if any(ch in p for ch in "*?[") and any(fnmatch.fnmatchcase(k, p) for k in ROOT_MAP):  # deploy/*
            return True
        bases = [
            ROOT,
            SKILLS,
            *skills,
            *(s / "assets" / sub for s in skills for sub in ("backend", "mcp_server", "frontend", "web")),
        ]
    p = p.rstrip("/")
    candidates = [p]
    if not EXT_RE.search(p):  # module notation: `app/reflection` ⇒ app/reflection.py, `app/tools.mcp` ⇒ …/mcp.py
        candidates += [f"{p}.py", p.replace(".", "/") + ".py"]
    for b in bases:
        for cand in candidates:
            if ("*" in cand and any(b.glob(cand))) or (b / cand).exists():
                return True
    return False


def cited_paths(code: str, strict: bool) -> Iterator[str]:
    """Template paths in a piece of code. strict (fenced blocks: commands and comments) ⇒ only file-like tokens
    (known extension) or explicit directories (trailing /)."""
    skill_names = {p.name for p in SKILLS.iterdir()}
    for raw in re.split(r"[\s,;()\[\]]+", code):
        tok = re.sub(r"::\w.*$|:\d+$", "", raw.strip("`'\".:"))  # app/x.py::func, file:line
        tok = re.sub(r"^<[\w-]+>/", "", tok).removeprefix("./")  # <skill-dir>/scripts/lf_trace.py
        if "/" not in tok or tok.startswith(("/", "~", "$", "http", "*", "references/")):
            continue
        if re.search(r"[<>{}$=|…@]", tok):  # placeholders, URLs with credentials, expressions
            continue
        head = tok.split("/")[0]
        if head not in PATH_DIRS and head not in skill_names:
            continue  # not rooted in a template dir: provider/model ids, other repos, prose like "deploy/redeploy"
        if strict and not (EXT_RE.search(tok) or tok.endswith("/")):
            continue
        yield tok


def check_paths(errors: list[str]) -> None:
    err = reporter(errors, "paths")
    n0 = len(errors)
    count = 0
    for doc in doc_files():
        for ml in md_lines(doc):
            toks = []
            if ml.in_fence:
                toks += cited_paths(ml.text, strict=True)
            else:
                for span in CODE_SPAN_RE.findall(ml.text):
                    toks += cited_paths(span, strict=False)
                toks += [t for t in BARE_PATH_RE.findall(CODE_SPAN_RE.sub(" ", ml.text)) if EXT_RE.search(t)]
            for tok in dict.fromkeys(toks):
                count += 1
                if not resolve_template_path(tok):
                    err(
                        doc,
                        ml.n,
                        f"`{tok}` exists in no skill (assets/backend, assets/mcp_server, "
                        "assets/frontend, assets/web, assets/root via scaffold.sh's mapping, the skill dirs) — fix the path, or "
                        f"allowlist it in {ALLOWLIST} (NON_TEMPLATE_PATHS) with who creates it",
                    )
    if len(errors) == n0:
        print(f"ok  paths: {count} cited template paths exist")


# --------------------------------------------------------------------------------------------------- 5. env-names
# `/` before: the tail of a shorthand such as GREENNODE_CLIENT_ID/SECRET/AGENT_IDENTITY. A trailing `_`/`_*`/`*` is a
# prefix mention (EXPO_PUBLIC_, LLM_*) checked as a glob.
ENV_TOKEN_RE = re.compile(rf"(?<![A-Za-z0-9_\-./]){UPPER_RE}(?:_\*|\*|_)?(?![A-Za-z0-9\-])")


def check_env_names(errors: list[str]) -> None:
    err = reporter(errors, "env-names")
    n0 = len(errors)
    w = env_world()
    names = set(w.agent) | set(w.mcp) | set(w.ci) | set(w.ci_store) | set(w.code_reads) | set(w.json_refs)
    for keys in w.env_files.values():
        names |= set(keys)
    # Only tokens that share a prefix with a config name are checked (LLM_, AUTH_, MCP_, EVAL_, GRN_…); shell/CI
    # internals (GITHUB_*, RUNNER_*) and Python constants are not config, whatever their prefix
    config = set(w.agent) | set(w.mcp) | set(w.ci_store) | {k for keys in w.env_files.values() for k in keys}
    prefixes = {k.split("_")[0] for k in config} | {"GREENNODE", "LANGFUSE", "EXPO"}
    count = 0

    def verify(path: Path, n: int, text: str) -> None:
        nonlocal count
        for tok in ENV_TOKEN_RE.findall(text):
            if tok.split("_")[0] not in prefixes:
                continue
            if tok.endswith(("*", "_")):
                glob = tok.rstrip("*") + "*"
                if any(fnmatch.fnmatchcase(k, glob) for k in names) or allowed(glob, "EXTERNAL_ENV_PATTERNS"):
                    continue
                err(path, n, f"`{tok}` matches no known variable — fix the pattern")
                continue
            count += 1
            if tok in w.constants or w.known(tok) or allowed(tok, "NON_ENV_TOKENS"):
                continue
            err(
                path,
                n,
                f"{tok} looks like a config variable but is not a Settings field, an env-file "
                "key, read by the code, nor a CI/Makefile/Dockerfile name — fix the name (renamed setting?), or "
                f"allowlist it in {ALLOWLIST} (EXTERNAL_ENV) with what reads it",
            )

    for doc in doc_files():
        for ml in md_lines(doc):
            verify(doc, ml.n, ml.text)
    for path in text_files(
        SKILLS / "agentbase-build-scaffold/assets", *SKILLS.glob("*/assets/backend"), MCP_SERVER, *JS_TEMPLATES
    ):
        if path.suffix in (".md", ".jsonl") or path.name == "package-lock.json":
            continue
        for n, line in enumerate(read(path).splitlines(), start=1):
            verify(path, n, line)
    if len(errors) == n0:
        print(f"ok  env-names: {count} config-variable mentions resolve")


# ------------------------------------------------------------------------------------------------- 6. build-sync
DIGEST_RE = re.compile(r"@sha256:[0-9a-f]{64}$")
FROZEN_RE = re.compile(r"\buv\s+(?:sync|run|export|pip\s+sync)\b[^`#\n]*--frozen")
CI_STEP_RE = re.compile(r"^\s*(?:-\s+)?(?:uses|runs-on):\s|^\s*-\s+run:|^\s*run:\s*\|")


def dockerfile_facts(path: Path) -> dict[str, tuple[object, int]]:
    """Shared build settings of a Dockerfile → (value, line where it is set)."""
    text = read(path)

    def at(rx: str) -> int:
        m = re.search(rx, text, re.M)
        return line_of(text, m.start()) if m else 1

    instr = re.sub(r"\\\n\s*", " ", text)  # join continuation lines
    runs = re.findall(r"^RUN\s+(.*)$", instr, re.M)
    return {
        "python images": (sorted(set(re.findall(r"^FROM\s+(python:\S+)", instr, re.M))), at(r"^FROM\s+python")),
        "stages": (len(re.findall(r"^FROM\s", instr, re.M)), at(r"^FROM\s")),
        "uv image": (sorted(set(re.findall(r"^FROM\s+(\S*astral-sh/uv\S*)", instr, re.M))), at(r"astral-sh/uv")),
        "uv env": (sorted(set(re.findall(r"\b(UV_[A-Z_]+=\S+)", instr))), at(r"UV_[A-Z_]+=")),
        "uv sync flags": (
            [
                " ".join(sorted(re.findall(r"--[\w-]+", m)))
                for r in runs
                for m in re.findall(r"uv sync((?:\s+--[\w-]+)*)", r)
            ],
            at(r"^RUN.*uv sync"),
        ),
        "chown": ([r for r in runs if "chown" in r], at(r"^RUN.*chown")),
    }


def check_build_sync(errors: list[str]) -> None:
    err = reporter(errors, "build-sync")
    n0 = len(errors)
    facts = {p: dockerfile_facts(p) for p in DOCKERFILES}
    (a, fa), (b, fb) = facts.items()
    for p, f in facts.items():
        images, n = f["python images"]
        if len(images) != 1:  # type: ignore[arg-type]
            err(p, n, f"stages use different python images {images} — use one tag")
        for key in ("python images", "uv image"):
            for img in f[key][0]:  # type: ignore[union-attr]
                if not DIGEST_RE.search(img):
                    err(
                        p,
                        f[key][1],
                        f"base image {img} is not pinned by digest — a tag moves under a build; pin it with "
                        f"`{img.split('@')[0]}@sha256:<digest>` (`docker buildx imagetools inspect {img}`; "
                        "Dependabot's docker ecosystem keeps the digest current)",
                    )
        chown, n = f["chown"]
        if chown:
            err(
                p,
                n,
                f"`chown` in a RUN layer ({chown[0][:60]}…) — keep /app root-owned (read-only "
                "for the non-root user) like the other Dockerfile; no `chown -R` layer",
            )
    for key in ("python images", "stages", "uv image", "uv env", "uv sync flags"):
        if fa[key][0] != fb[key][0]:
            err(
                b,
                fb[key][1],
                f"{key}: {fb[key][0]} here vs {fa[key][0]} in {rel(a)}:{fa[key][1]} — "
                "the two Dockerfiles share their build settings: change them together",
            )
    image = (fa["python images"][0] or ["?"])[0].split("@")[0]  # type: ignore[index]  # tag, without the digest
    pyver = re.match(r"python:(\d+\.\d+)", str(image))
    for d in (SCAFFOLD / "assets/backend", MCP_SERVER):
        pv = (d / ".python-version").read_text().strip()
        if pyver and pv != pyver.group(1):
            err(d / ".python-version", 1, f"{pv} but the Dockerfiles use {image} — keep them equal")
    reqs = {
        d: re.search(r'requires-python\s*=\s*"([^"]+)"', read(d / "pyproject.toml"))
        for d in (SCAFFOLD / "assets/backend", MCP_SERVER)
    }
    vals = {d: m.group(1) if m else None for d, m in reqs.items()}
    if len(set(vals.values())) != 1:
        d = next(iter(vals))
        err(
            d / "pyproject.toml",
            1,
            "requires-python differs between templates: " + ", ".join(f"{rel(k)}={v}" for k, v in vals.items()),
        )
    for mk in (ROOT_MAKEFILE, MCP_SERVER / "Makefile"):
        text = read(mk)
        for m in re.finditer(r"^\t.*docker build\b.*$", text, re.M):
            if "--platform linux/amd64" not in m.group(0):
                err(
                    mk,
                    line_of(text, m.start()),
                    "docker build without --platform linux/amd64 (AgentBase Runtime is amd64)",
                )
    # `uv … --frozen` anywhere (the lockfile must be checked: --locked); docs cite the Dockerfiles' python image
    workflows = sorted((ROOT / ".github/workflows").glob("*.yml"))
    scanned = [*doc_files(), *(p for p in text_files(SKILLS) if p.suffix != ".md"), *workflows]
    for path in scanned:
        for n, line in enumerate(read(path).splitlines(), start=1):
            if FROZEN_RE.search(line):
                err(
                    path,
                    n,
                    "`uv … --frozen` installs whatever uv.lock says even when it no longer "
                    "matches pyproject.toml — use `--locked` (fails on a stale lock), as the Dockerfiles and ci.yml do",
                )
            for img in re.findall(r"\bpython:\d+\.\d+[\w.-]*", line):
                if path.suffix == ".md" and img != image:
                    err(path, n, f"cites {img} but the Dockerfiles use {image} — update the doc")
    for doc in doc_files():
        for start, block in fenced_blocks(doc):
            if any(CI_STEP_RE.search(t) for _, t in block) and not allowed((rel(doc), start), "CI_SNIPPET_EXCEPTIONS"):
                err(
                    doc,
                    start,
                    "code block copies GitHub Actions steps — such copies drift from "
                    f"{rel(CI_YML)} (the eval SKILL's had `uv sync --frozen` and no config export): describe the "
                    "job in prose and point to `.github/workflows/ci.yml`, or allowlist (file, line) in "
                    f"{ALLOWLIST} CI_SNIPPET_EXCEPTIONS",
                )
    if len(errors) == n0:
        uv_image = fa["uv image"][0][0].split("@")[0]  # type: ignore[index]
        print(f"ok  build-sync: both Dockerfiles on {image} + {uv_image}, same digests, --locked; no CI copies in docs")


# ------------------------------------------------------------------------------------------------- 7. locks
_REQ_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[([^\]]*)\])?\s*(.*?)\s*$")


def _requirement(spec: str) -> tuple[str, tuple[str, ...], str]:
    """`pyjwt[crypto]>=2.14,<3` → ("pyjwt", ("crypto",), ">=2.14,<3") — the shape uv.lock records it in."""
    m = _REQ_RE.match(spec.split(";")[0])
    if not m:
        return spec, (), ""
    extras = tuple(sorted(e.strip().lower() for e in (m.group(2) or "").split(",") if e.strip()))
    return re.sub(r"[-_.]+", "-", m.group(1)).lower(), extras, re.sub(r"\s+", "", m.group(3))


def _locked(entries: list[dict]) -> set[tuple[str, tuple[str, ...], str]]:
    return {
        (
            re.sub(r"[-_.]+", "-", e["name"]).lower(),
            tuple(sorted(e.get("extras", []))),
            re.sub(r"\s+", "", e.get("specifier", "")),
        )
        for e in entries
    }


def check_locks(errors: list[str]) -> None:
    err = reporter(errors, "locks")
    n0 = len(errors)
    for d, project in ((SCAFFOLD / "assets/backend", "__PROJECT_NAME__-backend"), (MCP_SERVER, "__SERVER_NAME__")):
        lock_path, toml_path = d / "uv.lock", d / "pyproject.toml"
        if not lock_path.exists():
            err(lock_path, 1, "missing — run agentbase-build-scaffold/scripts/relock.sh (the Dockerfile needs a lock)")
            continue
        lock = tomllib.loads(read(lock_path))
        proj = tomllib.loads(read(toml_path))
        entry = next((p for p in lock.get("package", []) if (p.get("source") or {}).get("virtual") == "."), None)
        if entry is None or entry["name"] != project:
            got = entry["name"] if entry else None
            err(lock_path, 1, f"project entry is {got!r}, expected the placeholder {project!r} (relock.sh writes it)")
            continue
        meta = entry.get("metadata", {})
        pairs = (
            ("dependencies", {_requirement(x) for x in proj["project"].get("dependencies", [])}, _locked(meta.get("requires-dist", []))),
            (
                "dev dependencies",
                {_requirement(x) for x in proj.get("dependency-groups", {}).get("dev", []) if isinstance(x, str)},
                _locked((meta.get("requires-dev") or {}).get("dev", [])),
            ),
        )
        for what, want, have in pairs:
            if want != have:
                err(
                    lock_path,
                    1,
                    f"{what} differ from {rel(toml_path)}: only in pyproject {sorted(want - have)}, only in the lock "
                    f"{sorted(have - want)} — run agentbase-build-scaffold/scripts/relock.sh and commit the lock",
                )
    if len(errors) == n0:
        print("ok  locks: uv.lock of the backend and MCP server templates match their pyproject.toml")


CHECKS = (
    check_settings_env,
    check_trace_names,
    check_make_targets,
    check_paths,
    check_env_names,
    check_build_sync,
    check_locks,
)


def run(errors: list[str]) -> None:
    if sys.version_info < (3, 12):  # the templates use PEP 695 generics (def f[T](…)), which older ast can't parse
        errors.append(
            f"[drift] needs Python >= 3.12 to parse the templates (this is {sys.version.split()[0]}) — "
            "e.g. `uv run --python 3.12 --no-project python3 .github/scripts/check_skills.py`"
        )
        return
    for check in CHECKS:
        check(errors)
    check_allowlist(errors)


if __name__ == "__main__":
    errs: list[str] = []
    run(errs)
    for e in errs:
        print(f"ERROR {e}")
    sys.exit(1 if errs else 0)
