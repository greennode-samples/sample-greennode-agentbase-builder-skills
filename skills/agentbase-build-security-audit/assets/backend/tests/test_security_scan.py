"""make security-scan: the template passes, each planted mistake fails its check, and no env value is ever printed."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from scripts import security_scan

BACKEND = Path(__file__).resolve().parents[1]
PROJECT = BACKEND.parent.parent
SECRET = "sk-live-DO-NOT-PRINT"
CACHES = {".venv", "__pycache__", ".pytest_cache", ".ruff_cache", ".greennode.json"}
# Every tool / server / file a test plants is named "probe…": this file runs inside real projects, whose own
# tools, connectors and HITL_TOOLS patterns (crm_*, send_*…) must never collide with a planted mistake
PROBE = "probe"


def _skip(_dir: str, names: list[str]) -> set[str]:
    """Never copy the developer's secrets (.env, .env.<env>) or caches into the test project."""
    return {
        n for n in names if n in CACHES or (n.startswith(".env") and not n.endswith(".example"))
    }


@pytest.fixture
def project(tmp_path):
    """A copy of this project (backend + the root files the scan reads), committed in a throwaway git repo."""
    backend = tmp_path / "src/backend"
    shutil.copytree(BACKEND, backend, ignore=_skip)
    for name in (".gitignore", ".agentbase-state.json", ".github/workflows/ci.yml"):
        if (PROJECT / name).exists():
            (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(PROJECT / name, tmp_path / name)
    if not (tmp_path / ".gitignore").exists():
        (tmp_path / ".gitignore").write_text(".env\n.env.*\n!.env*.example\n.greennode.json\n")
    (backend / "uv.lock").touch()
    _git(tmp_path)
    return tmp_path


def _git(root: Path) -> None:
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "-A", "-f", "."], check=True)


def _scan(root: Path) -> dict[str, security_scan.Result]:
    return {r.id: r for r in security_scan.scan(root / "src/backend")}


def _set(root: Path, key: str, value: str, env: str = "prod") -> None:
    path = root / f"src/backend/.env.{env}.example"
    lines = [ln for ln in path.read_text().splitlines() if not ln.startswith(f"{key}=")]
    path.write_text("\n".join([*lines, f"{key}={value}"]) + "\n")


def _hitl(root: Path, env: str) -> list[str]:
    for line in (root / f"src/backend/.env.{env}.example").read_text().splitlines():
        if line.startswith("HITL_TOOLS="):
            return json.loads(line.partition("=")[2] or "[]")
    return []


def _tool(root: Path, source: str) -> None:
    (root / "src/backend/app/tools/probe_tools.py").write_text(
        "from langchain_core.tools import tool\n\n\n" + source
    )


def test_template_has_no_blocking_failure(project, capsys, monkeypatch):
    monkeypatch.chdir(project / "src/backend")
    assert security_scan.main([]) == 0, capsys.readouterr().out
    results = _scan(project)
    assert {r.status for r in results.values() if r.blocking} <= {"PASS", "WARN", "MANUAL", "N/A"}


def test_no_auth_outside_local_fails(project):
    _set(project, "AUTH_MODE", "none")
    assert _scan(project)["S1"].status == "FAIL"


def test_trusted_asserted_user_fails(project):
    _set(project, "IDENTITY_TRUST_ASSERTED_USER", "true", env="staging")
    assert _scan(project)["S1"].status == "FAIL"


def test_identity_or_credential_tool_parameter_fails(project):
    _tool(project, "@tool\ndef read_orders(user_id: str) -> str:\n    return ''\n")
    r = _scan(project)["S2"]
    assert r.status == "FAIL" and any("read_orders(user_id)" in d for _, d in r.lines)
    _tool(
        project, '@tool("lookup")\nasync def lookup(q: str, api_key: str) -> str:\n    return ""\n'
    )
    assert _scan(project)["S2"].status == "FAIL"


def test_side_effect_tool_without_approval_fails(project):
    _tool(project, "@tool\ndef probe_send_invoice(order: str) -> str:\n    return ''\n")
    assert _scan(project)["S3"].status == "FAIL"
    # add to the project's own patterns (it may have write tools already)
    for env in security_scan.DEPLOY_ENVS:
        _set(project, "HITL_TOOLS", json.dumps([*_hitl(project, env), "probe_send_*"]), env=env)
    # deploy envs fixed; .env.example (the local template) still misses it ⇒ WARN only
    r = _scan(project)["S3"]
    assert r.status == "WARN" and not any(s == "FAIL" for s, _ in r.lines)


def test_local_env_without_approval_warns(project):
    _tool(project, "@tool\ndef probe_send_invoice(order: str) -> str:\n    return ''\n")
    for env in security_scan.DEPLOY_ENVS:
        _set(project, "HITL_TOOLS", json.dumps([*_hitl(project, env), "probe_send_*"]), env=env)
    # make dev reads .env: created from .env.example before the tool existed, never updated
    (project / "src/backend/.env").write_text(f"HITL_TOOLS=[]\nLLM_API_KEY={SECRET}\n")
    r = _scan(project)["S3"]
    # the project's own write tools may be listed on the same line
    env_lines = [d for _, d in r.lines if d.startswith("src/backend/.env:")]
    assert r.status == "WARN" and any("probe_send_invoice" in d for d in env_lines)
    assert SECRET not in str(r.lines)


def test_mcp_write_tool_from_allow_tools_needs_approval(project):
    cfg = project / "src/backend/mcp_servers.json"
    data = json.loads(cfg.read_text())
    data["servers"][PROBE] = {
        "transport": "streamable_http",
        "url": f"${{MCP_GATEWAY_URL}}/{PROBE}",
        "auth": "iam",
        "allow_tools": ["get_contact", "delete_contact"],
    }
    cfg.write_text(json.dumps(data))
    r = _scan(project)["S3"]
    assert r.status == "FAIL" and any(f"{PROBE}_delete_contact" in d for _, d in r.lines)
    # deploy envs fixed; the local .env still runs the MCP write tool unapproved ⇒ WARN only
    for env in security_scan.DEPLOY_ENVS:
        _set(
            project, "HITL_TOOLS", json.dumps([*_hitl(project, env), f"{PROBE}_delete_*"]), env=env
        )
    (project / "src/backend/.env").write_text("HITL_TOOLS=[]\n")
    r = _scan(project)["S3"]
    env_lines = [d for s, d in r.lines if s == "WARN" and d.startswith("src/backend/.env:")]
    assert not any(s == "FAIL" for s, _ in r.lines)
    assert any(f"{PROBE}_delete_contact" in d for d in env_lines)


def test_unsafe_mcp_entries_fail(project):
    cfg = project / "src/backend/mcp_servers.json"
    data = json.loads(cfg.read_text())
    data["servers"][f"{PROBE}_root"] = {
        "transport": "streamable_http",
        "url": "${MCP_GATEWAY_URL}",
        "auth": "iam",
    }
    data["servers"][f"{PROBE}_noauth"] = {
        "transport": "streamable_http",
        "url": "${MCP_GATEWAY_URL}/x",
    }
    cfg.write_text(json.dumps(data))
    details = " ".join(d for _, d in _scan(project)["S4"].lines)
    assert "gateway root" in details and '"auth" is required' in details


def test_committed_secret_fails_and_is_never_printed(project, capsys, monkeypatch):
    (project / "src/backend/.env.prod").write_text(
        f"APP_ENV=prod\nAUTH_MODE=none\nLLM_API_KEY={SECRET}\n"
    )
    _set(project, "LANGFUSE_SECRET_KEY", SECRET)
    _git(project)
    results = _scan(project)
    assert results["S5"].status == "FAIL"
    details = " ".join(d for _, d in results["S5"].lines)
    assert (
        "src/backend/.env.prod is committed" in details
        and "LANGFUSE_SECRET_KEY has a value" in details
    )
    monkeypatch.chdir(project / "src/backend")
    assert security_scan.main([]) == 1
    assert security_scan.main(["--json"]) == 1
    assert SECRET not in capsys.readouterr().out


@pytest.mark.parametrize("app", ["frontend", "web"])
def test_secret_in_public_client_env_fails(project, app):
    prefix = "EXPO_PUBLIC_" if app == "frontend" else "NEXT_PUBLIC_"
    env = project / f"src/{app}/.env.example"
    env.parent.mkdir(parents=True, exist_ok=True)
    env.write_text(f"{prefix}API_URL=https://agent.example\n{prefix}CLIENT_SECRET=<set-me>\n")
    _git(project)
    details = " ".join(d for _, d in _scan(project)["S5"].lines)
    assert f"{prefix}CLIENT_SECRET" in details and f"{prefix}API_URL" not in details


def test_third_party_key_in_env_file_fails(project):
    _set(project, "STRIPE_API_KEY", "<set-in-deploy>")
    assert _scan(project)["S6"].status == "FAIL"


def test_prompt_without_data_rule_fails(project):
    prompt = project / "src/backend/app/prompts/system.md"
    prompt.write_text("\n".join(ln for ln in prompt.read_text().splitlines() if "DATA" not in ln))
    assert _scan(project)["S7"].status == "FAIL"


def test_injection_eval_item_passes(project):
    ds = project / "src/backend/evals/datasets/probe_injection.jsonl"
    item = {"id": "doc-inj", "input": {"message": "x"}, "metadata": {"tags": ["injection"]}}
    ds.write_text(json.dumps(item) + "\n")
    assert _scan(project)["S8"].status == "PASS"


def test_masking_off_and_cors_wildcard_fail(project):
    _set(project, "TRACE_MASK_PII", "false")
    _set(project, "CORS_ALLOW_ORIGINS", '["*"]')
    results = _scan(project)
    assert results["S9"].status == "FAIL" and results["S11"].status == "FAIL"


def test_raised_bound_warns(project):
    _set(project, "MAX_TOOL_ROUNDS", "50")
    assert _scan(project)["S10"].status == "WARN"


@pytest.mark.parametrize("key", ["LLM_MAX_TOKENS", "RATE_LIMIT_USER_PER_MIN", "SESSION_MAX_TURNS"])
def test_limit_turned_off_warns(project, key):
    _set(project, key, "0")
    details = " ".join(d for _, d in _scan(project)["S10"].lines)
    assert f"{key}=0 turns this limit off" in details


def test_unpinned_action_fails(project):
    wf = project / ".github/workflows/probe.yml"
    wf.parent.mkdir(parents=True, exist_ok=True)
    wf.write_text("jobs:\n  a:\n    steps:\n      - uses: actions/checkout@v4\n")
    assert _scan(project)["S12"].status == "FAIL"


def test_open_audit_findings_fail_and_waiver_accepts(project, monkeypatch):
    state = project / ".agentbase-state.json"
    data = json.loads(state.read_text()) if state.exists() else {}
    data["security_audit"] = {
        "date": "2026-10-28",
        "source_ref": "abc123",
        "confirmed_open": 2,
        "waivers": {},
    }
    state.write_text(json.dumps(data))
    assert _scan(project)["S13"].status == "FAIL"
    data["security_audit"]["waivers"] = {"S13": "accepted by the product owner until 2026-11-30"}
    state.write_text(json.dumps(data))
    assert _scan(project)["S13"].status == "WAIVED"
    monkeypatch.chdir(project / "src/backend")
    assert security_scan.main([]) == 0
