"""Security quick scan of this agent project — the invariants of /agentbase-build-security-audit, as PASS/FAIL.

    make security-scan                    (= cd src/backend && uv run python -m scripts.security_scan)
    make security-scan ENV=prod           only check the prod deploy env file
    make security-scan JSON=1             machine-readable result (CI)

Offline and read-only: reads the source, the committed deploy env files (.env.<env>.example, or the git-ignored
.env.<env> copy when it exists), mcp_servers.json, a2a_agents.json, the CI workflow and .agentbase-state.json. Never
calls the platform, never prints a value read from an env file (only key names), so it is safe to run in a chat.

Statuses: PASS · FAIL · WARN (should fix) · MANUAL (a fact outside the repo: the line names the skill that reads it)
· N/A · WAIVED (accepted in .agentbase-state.json → security_audit.waivers {"S3": "reason"}). Exit code 1 when a
blocking check FAILs — `ci.yml` runs it before every deploy.
"""

from __future__ import annotations

import argparse
import ast
import fnmatch
import json
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values

from app.config import Settings
from app.tools.mcp import mcp_config_error

DEPLOY_ENVS = ("dev", "staging", "prod")
STATE = ".agentbase-state.json"
SKILL = "/agentbase-build-security-audit"

# A tool parameter with one of these names lets the model choose whose data or which credential is used
IDENTITY_PARAMS = {
    "user_id",
    "userid",
    "user",
    "actor_id",
    "actor",
    "namespace",
    "session_id",
    "tenant_id",
    "owner",
}
CREDENTIAL_PARAMS = {
    "api_key",
    "apikey",
    "token",
    "access_token",
    "password",
    "secret",
    "client_secret",
}
# Words in a tool name that mean a side effect (write / send / pay / delete …) ⇒ the tool needs HITL approval
SIDE_EFFECT_WORDS = {
    "create", "add", "update", "edit", "set", "delete", "remove", "cancel", "send", "post", "publish", "submit",
    "pay", "transfer", "refund", "book", "order", "purchase", "approve", "reject", "write", "upload", "insert",
    "modify", "merge", "deploy", "execute", "run", "invite", "notify", "email", "sms", "reply", "comment", "close",
}  # fmt: skip
# Tools that only send data out are an exfiltration path for injected instructions
EGRESS_WORDS = {
    "send",
    "email",
    "sms",
    "post",
    "webhook",
    "http",
    "fetch",
    "upload",
    "notify",
    "reply",
}
SECRET_KEY_RE = re.compile(
    r"(SECRET|PASSWORD|PRIVATE_KEY|SIGNING_KEY|ACCESS_KEY|API_KEY$|_TOKEN$|CREDENTIAL)", re.I
)
# Env prefixes bundled into the client (mobile app, web page): never a secret behind them
PUBLIC_PREFIXES = ("EXPO_PUBLIC_", "NEXT_PUBLIC_")
PRIVATE_KEY_RE = re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")
_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
PINNED_RE = re.compile(r"^\s*-?\s*uses:\s*(\S+)@([0-9a-f]{40})\b")
USES_RE = re.compile(r"^\s*-?\s*uses:\s*(\S+)")
DATA_RULE_RE = re.compile(r"\bDATA\b.*(do not|never) follow instructions", re.I)
# Bounds that cap cost and blast radius: raising them above the template default is a WARN
BOUNDS = (
    "MAX_TOOL_ROUNDS",
    "REQUEST_TIMEOUT_S",
    "CONTEXT_HARD_LIMIT_TOKENS",
    "LLM_MAX_TOKENS",
    "MAX_REQUEST_BYTES",
    "SESSION_MAX_TURNS",
    "MAX_CONCURRENT_REQUESTS",
    "RATE_LIMIT_USER_PER_MIN",
)
# 0 turns these limits off: one runaway answer / one caller / one long session then has no ceiling
OFF_WHEN_ZERO = (
    "LLM_MAX_TOKENS",
    "SESSION_MAX_TURNS",
    "MAX_CONCURRENT_REQUESTS",
    "RATE_LIMIT_USER_PER_MIN",
)


@dataclass
class Result:
    id: str
    title: str
    blocking: bool
    lines: list[tuple[str, str]] = field(default_factory=list)  # (status, detail)

    def add(self, status: str, detail: str) -> None:
        self.lines.append((status, detail))

    @property
    def status(self) -> str:
        found = {s for s, _ in self.lines}  # MANUAL only when nothing could be checked here
        for s in ("FAIL", "WAIVED", "WARN", "PASS", "MANUAL"):
            if s in found:
                return s
        return "N/A"


@dataclass
class Project:
    root: Path
    backend: Path
    envs: dict[str, dict[str, str]]  # deploy env name → {KEY: value} (values never printed)
    env_files: dict[str, str]  # deploy env name → file shown in messages
    state: dict

    def setting(self, env: str, key: str) -> str:
        """The value the runtime gets: the env file's, else the Settings default (empty = default)."""
        value = self.envs.get(env, {}).get(key) or ""
        if value:
            return value
        f = Settings.model_fields.get(key.lower())
        default = None if f is None else f.get_default(call_default_factory=True)
        if default is None:
            return ""
        return default if isinstance(default, str) else json.dumps(default)

    def json_list(self, env: str, key: str) -> list[str]:
        try:
            v = json.loads(self.setting(env, key) or "[]")
        except json.JSONDecodeError:
            return []
        return [str(x) for x in v] if isinstance(v, list) else []


def load_project(backend: Path, only_env: str | None) -> Project:
    root = backend.parent.parent
    envs: dict[str, dict[str, str]] = {}
    files: dict[str, str] = {}
    for env in DEPLOY_ENVS:
        if only_env and env != only_env:
            continue
        real, example = backend / f".env.{env}", backend / f".env.{env}.example"
        path = real if real.exists() else example
        if path.exists():
            envs[env] = {k: (v or "").strip() for k, v in dotenv_values(path).items()}
            files[env] = str(path.relative_to(root))
    try:
        state = json.loads((root / STATE).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        state = {}
    return Project(root, backend, envs, files, state if isinstance(state, dict) else {})


# --- source helpers ---------------------------------------------------------------------------------------------


def _tool_name(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> str | None:
    """Name of a LangChain tool defined with @tool / @tool("name"), else None."""
    for d in fn.decorator_list:
        target = d.func if isinstance(d, ast.Call) else d
        name = target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", None)
        if name != "tool":
            continue
        if (
            isinstance(d, ast.Call)
            and d.args
            and isinstance(d.args[0], ast.Constant)
            and isinstance(d.args[0].value, str)
        ):
            return d.args[0].value
        return fn.name
    return None


def local_tools(backend: Path) -> list[tuple[str, list[str], str]]:
    """(tool name, parameter names, file:line) of every @tool under app/."""
    out = []
    for py in sorted((backend / "app").rglob("*.py")):
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and (
                name := _tool_name(node)
            ):
                a = node.args
                params = [p.arg for p in (*a.posonlyargs, *a.args, *a.kwonlyargs)]
                out.append((name, params, f"{py.relative_to(backend)}:{node.lineno}"))
    return out


def _words(name: str) -> set[str]:
    return set(re.split(r"[_\-.]+", name.lower()))


def is_side_effect(name: str) -> bool:
    return bool(_words(name) & SIDE_EFFECT_WORDS)


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def mcp_servers(p: Project, env: str, config_file: str | None = None) -> dict[str, dict]:
    path = config_file or p.setting(env, "MCP_CONFIG_FILE")
    servers = _read_json(p.backend / path).get("servers") or {}
    return {
        n: e
        for n, e in servers.items()
        if isinstance(e, dict)
        and e.get("enabled", True) is not False
        and (not e.get("envs") or env in e["envs"])
    }


def expand(cfg: dict, values: dict[str, str]) -> dict:
    """`${VAR}` in string leaves → the env file's value (unset ⇒ a dummy https URL: only the shape is checked)."""
    text = json.dumps(cfg)
    return json.loads(
        _ENV_REF.sub(
            lambda m: json.dumps(values.get(m.group(1)) or "https://unset.invalid")[1:-1], text
        )
    )


def a2a_tools(p: Project) -> list[str]:
    agents = _read_json(p.backend / "a2a_agents.json").get("agents") or {}
    return [
        "ask_" + re.sub(r"[^a-z0-9_]+", "_", n.lower())
        for n, e in agents.items()
        if isinstance(e, dict) and e.get("enabled", True) is not False
    ]


def git_files(root: Path) -> list[str] | None:
    try:
        r = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z"], capture_output=True, timeout=30, check=True
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return [f for f in r.stdout.decode().split("\0") if f]


# --- checks -------------------------------------------------------------------------------------------------------


def s1_inbound_auth(p: Project, r: Result) -> None:
    for env, f in p.env_files.items():
        before = len(r.lines)
        mode = p.setting(env, "AUTH_MODE")
        app_env = p.envs[env].get("APP_ENV", "")
        if app_env in ("", "local"):
            r.add("FAIL", f"{f}: APP_ENV must be set to {env} (the Runtime refuses local)")
        if mode == "none":
            r.add(
                "FAIL",
                f"{f}: AUTH_MODE=none — anyone reaching the endpoint is a user (/agentbase-build-auth)",
            )
        elif mode == "api_key":
            r.add(
                "WARN",
                f"{f}: AUTH_MODE=api_key — only for trusted server callers; write the reason in the spec",
            )
        elif mode == "jwt" and not p.setting(env, "AUTH_JWKS_URL"):
            r.add(
                "WARN",
                f"{f}: AUTH_MODE=jwt without AUTH_JWKS_URL — set it before deploy (/agentbase-build-auth)",
            )
        for flag in ("AUTH_ALLOW_NO_AUDIENCE", "IDENTITY_TRUST_ASSERTED_USER"):
            if p.setting(env, flag).lower() == "true":
                r.add(
                    "FAIL",
                    f"{f}: {flag}=true — accepted only behind a trusted BFF (waive with the reason)",
                )
        if len(r.lines) == before:
            r.add("PASS", f"{f}: AUTH_MODE={mode}")
    r.add(
        "MANUAL",
        "Runtime Inbound Auth / IP Access Control of each deployed version — /agentbase-deploy",
    )


def s2_tool_parameters(p: Project, r: Result) -> None:
    bad = False
    tools = local_tools(p.backend)
    for name, params, where in tools:
        for param in params:
            if param.lower() in IDENTITY_PARAMS:
                bad = True
                r.add(
                    "FAIL",
                    f"{where}: tool {name}({param}) — the model picks whose data; read config['configurable']['actor_id']",
                )
            elif param.lower() in CREDENTIAL_PARAMS:
                bad = True
                r.add(
                    "FAIL",
                    f"{where}: tool {name}({param}) — credential as a tool parameter; use app/identity.py",
                )
    if not bad:
        r.add("PASS", f"{len(tools)} local tools, no identity or credential parameter")


def mcp_tool_names(servers: dict[str, dict]) -> tuple[list[str], list[str]]:
    """(tool names the agent sees from allow_tools, servers without allow_tools — their tools are unknown)."""
    known, unknown = [], []
    for server, cfg in servers.items():
        if allow := cfg.get("allow_tools"):
            known += [f"{server}_{t}" if cfg.get("prefix", True) else t for t in allow]
        else:
            unknown.append(server)
    return known, unknown


def s3_hitl(p: Project, r: Result) -> None:
    tools = [n for n, _, _ in local_tools(p.backend)]
    for env, f in p.env_files.items():
        patterns = p.json_list(env, "HITL_TOOLS")
        mcp_known, unknown_servers = mcp_tool_names(mcp_servers(p, env))
        known = tools + mcp_known
        missing = [
            t
            for t in known
            if is_side_effect(t) and not any(fnmatch.fnmatch(t, x) for x in patterns)
        ]
        for t in missing:
            r.add(
                "FAIL",
                f"{f}: side-effect tool {t} is not matched by HITL_TOOLS (/agentbase-build-hitl)",
            )
        unmatched = [x for x in patterns if not any(fnmatch.fnmatch(t, x) for t in known)]
        if unmatched and not unknown_servers:
            r.add(
                "WARN",
                f"{f}: HITL_TOOLS patterns match no known tool: {', '.join(unmatched)} — approval not enforced",
            )
        if unknown_servers:
            r.add(
                "MANUAL",
                f"{f}: MCP servers without allow_tools ({', '.join(unknown_servers)}): check "
                "tools.collect.output.hitl_unmatched on a real trace and list their write tools in HITL_TOOLS",
            )
        if not missing:
            r.add("PASS", f"{f}: {len(known)} known tools, every side-effect tool needs approval")
    # Local runs (make dev) read .env, created from .env.example once: a write tool added later runs unapproved
    for name in (".env.example", ".env"):
        path = p.backend / name
        if not path.exists():
            continue
        values = dotenv_values(path)
        servers = mcp_servers(p, "local", values.get("MCP_CONFIG_FILE") or None)
        writes = [t for t in tools + mcp_tool_names(servers)[0] if is_side_effect(t)]
        try:
            patterns = json.loads(values.get("HITL_TOOLS") or "[]")
        except json.JSONDecodeError:
            patterns = []
        if missing := [t for t in writes if not any(fnmatch.fnmatch(t, str(x)) for x in patterns)]:
            r.add(
                "WARN",
                f"src/backend/{name}: make dev runs {', '.join(missing)} without approval — add them to "
                f"HITL_TOOLS there (.env: the user edits it)",
            )
    if peers := a2a_tools(p):
        r.add(
            "MANUAL",
            f"A2A tools {', '.join(peers)}: in HITL_TOOLS if the peer can take actions (/agentbase-build-a2a)",
        )


def s4_mcp(p: Project, r: Result) -> None:
    any_server = False
    for env, f in p.env_files.items():
        for name, cfg in mcp_servers(p, env).items():
            any_server = True
            if err := mcp_config_error(expand(cfg, p.envs[env])):
                r.add("FAIL", f"{f}: mcp_servers.json → {name}: {err}")
            elif cfg.get("transport") == "stdio":
                r.add("FAIL", f"{f}: mcp_servers.json → {name}: stdio server enabled outside local")
            elif cfg.get("auth") == "none":
                r.add(
                    "WARN",
                    f"{f}: mcp_servers.json → {name}: auth none — only for a self-built server with its own key",
                )
            elif re.fullmatch(r"\$\{MCP_GATEWAY_URL\}/?", str(cfg.get("url", ""))):
                r.add(
                    "FAIL",
                    f"{f}: mcp_servers.json → {name}: url is the gateway root, use the connector's connectUrl",
                )
            else:
                r.add("PASS", f"{f}: mcp_servers.json → {name}: auth {cfg.get('auth')}")
    if any_server:
        r.add(
            "MANUAL",
            "Policy Group allows only the spec's <connector>__<tool> actions per principal — /agentbase-policy",
        )


def s5_secrets_committed(p: Project, r: Result) -> None:
    files = git_files(p.root)
    if files is None:
        r.add("WARN", "not a git repository — cannot check what is committed")
        files = []
    never = re.compile(
        r"(^|/)(\.env(\.[a-z]+)?|\.greennode\.json|[^/]*\.credentials\.json|deploy/(agent|runtime)\.yaml)$"
    )
    for f in files:
        if never.search(f) and not f.endswith(".example"):
            r.add(
                "FAIL",
                f"{f} is committed — remove it from git and rotate its secrets (references/security.md)",
            )
    for f in files:
        path = p.root / f
        if (
            path.suffix in (".png", ".jpg", ".ico", ".lock")
            or not path.is_file()
            or path.stat().st_size > 1_000_000
        ):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if PRIVATE_KEY_RE.search(text):
            r.add("FAIL", f"{f}: contains a private key")
        if Path(f).name.startswith(".env") and f.endswith(".example"):
            for k, v in dotenv_values(path).items():
                if SECRET_KEY_RE.search(k) and v and not v.startswith("<"):
                    r.add(
                        "FAIL",
                        f"{f}: {k} has a value — committed env files hold <set-in-deploy> or empty only",
                    )
                public = next((x for x in PUBLIC_PREFIXES if k.startswith(x)), None)
                if public and SECRET_KEY_RE.search(k.removeprefix(public)):
                    r.add(
                        "FAIL", f"{f}: {k} — {public}* ships to the browser / app, never a secret"
                    )
    for ignore_file in (p.root / ".gitignore", p.backend / ".dockerignore"):
        text = ignore_file.read_text(encoding="utf-8") if ignore_file.exists() else ""
        for entry in (".env", ".greennode.json"):
            if not re.search(rf"^(\*\*/|/)?{re.escape(entry)}\*?$", text, re.M):
                r.add("FAIL", f"{ignore_file.relative_to(p.root)} does not exclude {entry}")
    if not any(s == "FAIL" for s, _ in r.lines):
        r.add("PASS", f"{len(files)} tracked files: no secret files, private keys or secret values")


def s6_outbound_credentials(p: Project, r: Result) -> None:
    own = {k.upper() for k in Settings.model_fields} | {
        "LANGFUSE_PUBLIC_KEY",
        "LANGFUSE_SECRET_KEY",
    }
    found = False
    for env, f in p.env_files.items():
        for k in p.envs[env]:
            if (
                SECRET_KEY_RE.search(k)
                and k not in own
                and not k.startswith(("A2A_", "MCP_", "GREENNODE_"))
            ):
                found = True
                r.add(
                    "FAIL",
                    f"{f}: {k} is not the agent's own setting — external-service keys go to AgentBase "
                    "Identity (/agentbase-build-identity)",
                )
    if not found:
        r.add("PASS", "deploy env files hold only the agent's own settings")


def s7_injection_defenses(p: Project, r: Result) -> None:
    prompt = p.backend / "app/prompts/system.md"
    text = prompt.read_text(encoding="utf-8") if prompt.exists() else ""
    if DATA_RULE_RE.search(text):
        r.add(
            "PASS", "app/prompts/system.md keeps the rule: tool / document / agent content is DATA"
        )
    else:
        r.add(
            "FAIL",
            "app/prompts/system.md lost the rule that tool / document / agent content is DATA, not instructions",
        )
    for env, f in p.env_files.items():
        if p.setting(env, "LANGFUSE_PROMPT_NAME"):
            r.add(
                "MANUAL",
                f"{f}: LANGFUSE_PROMPT_NAME set — check the Langfuse prompt keeps the DATA rule",
            )
    egress = sorted({n for n, _, _ in local_tools(p.backend) if _words(n) & EGRESS_WORDS})
    if egress:
        r.add(
            "MANUAL",
            f"tools that send data out ({', '.join(egress)}): confirm a destination allowlist",
        )


def s8_injection_eval(p: Project, r: Result) -> None:
    count = 0
    for ds in sorted((p.backend / "evals/datasets").glob("*.jsonl")):
        for line in ds.read_text(encoding="utf-8").splitlines():
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            tags = (item.get("metadata") or {}).get("tags") or []
            count += any("injection" in str(t).lower() for t in tags)
    if count:
        r.add("PASS", f"{count} eval items tagged injection")
    else:
        r.add(
            "WARN",
            "no eval item tagged injection — add one per content source the agent reads (/agentbase-build-eval)",
        )


def s9_masking(p: Project, r: Result) -> None:
    for env, f in p.env_files.items():
        if p.setting(env, "TRACE_MASK_PII").lower() in ("false", "0", "no"):
            r.add("FAIL", f"{f}: TRACE_MASK_PII=false — PII would reach Langfuse")
        else:
            r.add("PASS", f"{f}: TRACE_MASK_PII on")
    r.add(
        "MANUAL",
        "one real trace has no token, key or raw PII — lf_trace.py --audit (/agentbase-build-tracing)",
    )


def s10_bounds(p: Project, r: Result) -> None:
    for env, f in p.env_files.items():
        for key in BOUNDS:
            if (field := Settings.model_fields.get(key.lower())) is None:
                continue  # a project from an older template
            default = field.get_default(call_default_factory=True)
            try:
                value = float(p.setting(env, key))
            except ValueError:
                continue
            if value == 0 and key in OFF_WHEN_ZERO:
                r.add("WARN", f"{f}: {key}=0 turns this limit off — write the reason in the spec")
            elif value > float(default):
                r.add(
                    "WARN",
                    f"{f}: {key} raised above the template default ({default}) — write the reason in the spec",
                )
        if not any(s == "WARN" and x.startswith(f) for s, x in r.lines):
            r.add("PASS", f"{f}: cost bounds at or below the defaults")
    r.add("MANUAL", "the agent's LLM key has a Rate Limit — /agentbase-llm")


def s11_exposure(p: Project, r: Result) -> None:
    for env, f in p.env_files.items():
        if "*" in p.json_list(env, "CORS_ALLOW_ORIGINS"):
            r.add("FAIL", f"{f}: CORS_ALLOW_ORIGINS contains * — list the web origins")
        else:
            r.add("PASS", f"{f}: CORS origins listed explicitly")
    r.add(
        "MANUAL",
        "network mode / IP Access Control match .agentbase-state.json → environments — /agentbase-build-deploy",
    )


def s12_supply_chain(p: Project, r: Result) -> None:
    files = git_files(p.root) or []
    if (p.backend / "requirements.txt").exists():
        r.add(
            "FAIL", "src/backend/requirements.txt — dependencies only in pyproject.toml + uv.lock"
        )
    if not (p.backend / "uv.lock").exists():
        r.add("FAIL", "src/backend/uv.lock missing — run make lock and commit it")
    elif files and "src/backend/uv.lock" not in files:
        r.add("WARN", "src/backend/uv.lock is not committed")
    for wf in sorted((p.root / ".github/workflows").glob("*.y*ml")):
        for n, line in enumerate(wf.read_text(encoding="utf-8").splitlines(), start=1):
            m = USES_RE.match(line)
            if m and not m.group(1).startswith("./") and not PINNED_RE.match(line):
                r.add(
                    "FAIL", f"{wf.relative_to(p.root)}:{n}: {m.group(1)} not pinned to a commit SHA"
                )
    if not any(s in ("FAIL", "WARN") for s, _ in r.lines):
        r.add("PASS", "uv.lock present, CI actions pinned to commit SHAs")


def s13_audit_record(p: Project, r: Result) -> None:
    audit = p.state.get("security_audit") or {}
    if not audit.get("date"):
        r.add("WARN", f"no full audit recorded — run one before the first prod release ({SKILL})")
        return
    open_findings = audit.get("confirmed_open") or 0
    if open_findings:
        r.add(
            "FAIL",
            f"{open_findings} confirmed High/Critical findings open since the audit of {audit['date']}",
        )
    else:
        r.add(
            "PASS",
            f"last full audit {audit['date']} at {audit.get('source_ref') or '?'}: nothing open",
        )


CHECKS: list[tuple[str, str, bool, Callable[[Project, Result], None]]] = [
    ("S1", "Inbound auth outside local", True, s1_inbound_auth),
    ("S2", "No user, actor or credential as a tool parameter", True, s2_tool_parameters),
    ("S3", "Side-effect tools need human approval", True, s3_hitl),
    ("S4", "MCP servers declared safely", True, s4_mcp),
    ("S5", "No secrets committed", True, s5_secrets_committed),
    ("S6", "External-service credentials only via Identity", True, s6_outbound_credentials),
    ("S7", "Prompt-injection defenses", True, s7_injection_defenses),
    ("S8", "Prompt injection covered by evals", False, s8_injection_eval),
    ("S9", "No PII or secrets in traces", True, s9_masking),
    ("S10", "Cost and blast-radius bounds", False, s10_bounds),
    ("S11", "Network exposure", True, s11_exposure),
    ("S12", "Supply chain", False, s12_supply_chain),
    ("S13", "Full audit findings closed", True, s13_audit_record),
]


def scan(backend: Path, only_env: str | None = None) -> list[Result]:
    p = load_project(backend, only_env)
    waivers = (p.state.get("security_audit") or {}).get("waivers") or {}
    results = []
    for cid, title, blocking, check in CHECKS:
        r = Result(cid, title, blocking)
        if not p.env_files and cid in ("S1", "S3", "S4", "S6", "S9", "S10", "S11"):
            r.add(
                "FAIL" if blocking else "WARN",
                "no deploy env file (src/backend/.env.<env>.example) to check",
            )
        else:
            check(p, r)
        if (reason := waivers.get(cid)) and r.status == "FAIL":
            r.lines = [("WAIVED" if s == "FAIL" else s, d) for s, d in r.lines] + [
                ("WAIVED", f"reason: {reason}")
            ]
        results.append(r)
    return results


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--env", choices=DEPLOY_ENVS, help="check one deploy environment only")
    ap.add_argument("--json", action="store_true", help="print JSON")
    args = ap.parse_args(argv)
    results = scan(Path.cwd(), args.env)
    failed = [r for r in results if r.blocking and r.status == "FAIL"]
    if args.json:
        print(
            json.dumps(
                [{**r.__dict__, "status": r.status} for r in results], ensure_ascii=False, indent=2
            )
        )
    else:
        for r in results:
            print(f"{r.status:<7} {r.id:<4} {r.title}{'  [B]' if r.blocking else ''}")
            for status, detail in r.lines:
                print(f"          {status:<7} {detail}")
        print(
            f"\n{len(failed)} blocking check(s) failed" if failed else "\nNo blocking check failed"
        )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
