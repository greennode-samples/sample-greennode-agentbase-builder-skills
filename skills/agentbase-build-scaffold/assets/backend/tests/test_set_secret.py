"""make set-secret: the user types secrets into a hidden prompt — written right, everything else kept, never shown."""

from __future__ import annotations

import ast
import importlib.util
import io
import json
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import set_secret

# templates: .env.example, .greennode.json.example, .gitignore (+ .env.prod.example in a generated project)
BACKEND = Path(__file__).resolve().parent.parent
CID, CSECRET = "cid-0123456789abcdef", "csecret-DO-NOT-PRINT-0123456789"
LLM_KEY = "llm-key-DO-NOT-PRINT-abcdef"


class Terminal:
    """A scripted user: answers to hidden prompts (getpass) and visible ones (input), in order."""

    def __init__(self, monkeypatch, hidden=(), typed=()):
        self.hidden, self.typed, self.prompts = list(hidden), list(typed), []
        monkeypatch.setattr(set_secret.getpass, "getpass", self._getpass)
        monkeypatch.setattr("builtins.input", self._input)

    def _getpass(self, prompt="", stream=None):
        self.prompts.append(prompt)
        answer = self.hidden.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def _input(self, prompt=""):
        self.prompts.append(prompt)
        return self.typed.pop(0)


@pytest.fixture
def base(tmp_path, monkeypatch):
    """A project (cwd = its root) with the backend templates and no .env / .greennode.json yet."""
    backend = tmp_path / "src" / "backend"
    backend.mkdir(parents=True)
    for name in (".env.example", ".greennode.json.example", ".gitignore"):
        shutil.copy(BACKEND / name, backend / name)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(set_secret, "stdin_is_tty", lambda: True)
    # not a repo: the .gitignore files decide
    monkeypatch.setattr(set_secret, "_git", lambda cwd, *args: 128)
    return backend


def run(base: Path, *args: str) -> int:
    return set_secret.main(["--base", str(base), *args])


def assert_not_shown(capsys, term: Terminal | None, *values: str) -> str:
    out = capsys.readouterr()
    shown = out.out + out.err + "".join(term.prompts if term else [])
    for v in values:
        assert v not in shown
    return shown


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_first_run_sets_the_required_keys(base, monkeypatch, capsys):
    # Enter in the menu = the required keys not set yet
    term = Terminal(monkeypatch, hidden=[CID, CSECRET, LLM_KEY], typed=[""])
    assert run(base) == 0
    creds = json.loads((base / ".greennode.json").read_text())
    # the .example's <placeholders> are not copied
    assert creds == {"client_id": CID, "client_secret": CSECRET, "agent_identity": ""}
    example = (base / ".env.example").read_text().splitlines()
    env = (base / ".env").read_text().splitlines()
    assert [i for i, (a, b) in enumerate(zip(example, env, strict=True)) if a != b] == [
        example.index("LLM_API_KEY=")
    ]
    assert f"LLM_API_KEY={LLM_KEY}" in env
    assert mode(base / ".env") == mode(base / ".greennode.json") == 0o600
    shown = assert_not_shown(capsys, term, CID, CSECRET, LLM_KEY)
    assert f"client_secret set ({len(CSECRET)} chars)" in shown
    assert "created from .env.example" in shown and "Next: make" in shown
    assert not list(base.glob(".env.set-secret-*"))  # atomic write left no temp file


def test_other_lines_and_keys_are_kept(base, monkeypatch, capsys):
    original = (base / ".env.example").read_text().replace("LOG_LEVEL=INFO", "LOG_LEVEL=DEBUG")
    original += "# my own note\nMY_TOOL_KEY=keep-me-123456789\n"
    (base / ".env").write_text(original)
    (base / ".greennode.json").write_text(
        json.dumps(
            {"client_id": CID, "client_secret": "old-secret-123456", "agent_identity": "a-dev"}
        )
    )
    term = Terminal(monkeypatch, hidden=[CSECRET, LLM_KEY], typed=["y"])  # replace client_secret
    assert run(base, "--name", "client_secret,LLM_API_KEY") == 0
    assert (base / ".env").read_text() == original.replace(
        "LLM_API_KEY=\n", f"LLM_API_KEY={LLM_KEY}\n"
    )
    creds = json.loads((base / ".greennode.json").read_text())
    assert creds == {"client_id": CID, "client_secret": CSECRET, "agent_identity": "a-dev"}
    assert list(creds) == ["client_id", "client_secret", "agent_identity"]
    assert_not_shown(capsys, term, CSECRET, LLM_KEY, "old-secret-123456")


def test_replacing_a_value_needs_confirmation(base, monkeypatch, capsys):
    (base / ".env").write_text("LLM_API_KEY=old-key-123456789\n")
    term = Terminal(monkeypatch, typed=["n"])
    assert run(base, "--name", "LLM_API_KEY") == 0
    assert (base / ".env").read_text() == "LLM_API_KEY=old-key-123456789\n"
    assert term.hidden == [] and len(term.prompts) == 1  # no hidden prompt after "n"
    shown = assert_not_shown(capsys, term, "old-key-123456789")
    assert "already set (17 chars)" in shown

    term = Terminal(monkeypatch, hidden=[LLM_KEY], typed=["y"])
    assert run(base, "--name", "llm") == 0  # group name
    assert (base / ".env").read_text() == f"LLM_API_KEY={LLM_KEY}\n"


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        ("Bearer sk-abcdefgh12345", "auth scheme"),
        ('"sk-abcdefgh12345"', "quotes"),
        ("sk-abc defgh12345", "whitespace"),
        ("sk-abc${HOME}12345", "${…}"),
        ("LLM_API_KEY=sk-abcdefgh12345", "NAME=value"),
        ("sk-123", "only 6 characters"),
        ("https://maas.example/v1", "is a URL"),
        ("<set-in-deploy>", "placeholder"),
        ("\x1b[200~sk-abcdefgh12345", "control characters"),
        ('{"key":"sk-abcdefgh12345"}', "JSON"),
    ],
)
def test_invalid_values_are_refused(base, monkeypatch, capsys, value, reason):
    term = Terminal(monkeypatch, hidden=[value] * set_secret.ATTEMPTS)
    assert run(base, "--name", "LLM_API_KEY") == 0
    assert not (base / ".env").exists()  # nothing written
    shown = assert_not_shown(capsys, term, value)
    assert reason in shown and f"skipped after {set_secret.ATTEMPTS} attempts" in shown


def test_surrounding_whitespace_is_stripped(base, monkeypatch, capsys):
    term = Terminal(monkeypatch, hidden=[f"  {LLM_KEY} \t"])
    assert run(base, "--name", "LLM_API_KEY") == 0
    assert f"LLM_API_KEY={LLM_KEY}\n" in (base / ".env").read_text()
    assert "surrounding whitespace removed" in assert_not_shown(capsys, term, LLM_KEY)


def test_wrong_pastes_between_keys_are_refused(base, monkeypatch, capsys):
    term = Terminal(monkeypatch, hidden=[CID] + [CID] * set_secret.ATTEMPTS)  # the id pasted twice
    assert run(base, "--name", "iam") == 0
    assert json.loads((base / ".greennode.json").read_text())["client_secret"] == ""
    assert "same value as client_id" in assert_not_shown(capsys, term, CID)

    term = Terminal(monkeypatch, hidden=["pk-lf-0123456789"] * set_secret.ATTEMPTS)
    assert run(base, "--name", "LANGFUSE_SECRET_KEY") == 0
    assert "Langfuse PUBLIC key" in assert_not_shown(capsys, term, "pk-lf-0123456789")


def test_langfuse_group_and_plain_base_url(base, monkeypatch, capsys):
    pk, sk = "pk-lf-0123456789", "sk-lf-DO-NOT-PRINT-0123"
    term = Terminal(monkeypatch, hidden=[pk, sk], typed=["langfuse", ""])  # menu, then keep the URL
    assert run(base) == 0
    env = (base / ".env").read_text()
    assert f"LANGFUSE_PUBLIC_KEY={pk}\n" in env and f"LANGFUSE_SECRET_KEY={sk}\n" in env
    assert "LANGFUSE_BASE_URL=https://cloud.langfuse.com\n" in env
    assert_not_shown(capsys, term, pk, sk)


def test_feedback_key_is_generated_on_enter(base, monkeypatch, capsys):
    term = Terminal(monkeypatch, hidden=[""])
    assert run(base, "--name", "FEEDBACK_SIGNING_KEY") == 0
    key = set_secret.parse_env((base / ".env").read_text().splitlines())["FEEDBACK_SIGNING_KEY"]
    assert len(key) >= 32
    assert_not_shown(capsys, term, key)


def test_without_a_tty_nothing_is_read(base, monkeypatch, capsys):
    monkeypatch.setattr(set_secret, "stdin_is_tty", lambda: False)
    term = Terminal(monkeypatch)
    assert run(base, "--name", "LLM_API_KEY") == 2
    assert term.prompts == [] and not (base / ".env").exists()
    err = capsys.readouterr().err
    assert "YOUR interactive terminal" in err and "make set-secret NAME=LLM_API_KEY" in err


def test_a_terminal_that_cannot_hide_input_is_refused(base, monkeypatch, capsys):
    def echoing_getpass(prompt="", stream=None):
        import warnings

        warnings.warn("Can not control echo on the terminal.", set_secret.getpass.GetPassWarning, 2)
        return LLM_KEY  # what the fallback would read — with echo

    monkeypatch.setattr(set_secret.getpass, "getpass", echoing_getpass)
    assert run(base, "--name", "LLM_API_KEY") == 1
    assert "cannot hide input" in capsys.readouterr().err
    assert not (base / ".env").exists()


def test_stdin_mode_for_secret_managers(base, monkeypatch, capsys):
    monkeypatch.setattr(set_secret, "stdin_is_tty", lambda: False)
    monkeypatch.setattr(sys, "stdin", io.StringIO(LLM_KEY + "\n"))
    assert run(base, "--name", "LLM_API_KEY", "--stdin") == 0
    assert f"LLM_API_KEY={LLM_KEY}\n" in (base / ".env").read_text()

    monkeypatch.setattr(sys, "stdin", io.StringIO("another-key-123456789\n"))
    assert run(base, "--name", "LLM_API_KEY", "--stdin") == 1  # already set: needs --yes
    monkeypatch.setattr(sys, "stdin", io.StringIO("a-key-123456789\nsecond-line\n"))
    assert run(base, "--name", "LLM_API_KEY", "--stdin", "--yes") == 1
    assert run(base, "--name", "iam", "--stdin") == 1  # one key per pipe
    assert f"LLM_API_KEY={LLM_KEY}\n" in (base / ".env").read_text()
    shown = assert_not_shown(capsys, None, LLM_KEY, "another-key-123456789", "a-key-123456789")
    assert "already set" in shown and "more than one line" in shown and "exactly one key" in shown

    # in a terminal the hidden prompt is the way
    monkeypatch.setattr(set_secret, "stdin_is_tty", lambda: True)
    assert run(base, "--name", "LLM_API_KEY", "--stdin") == 1


def test_list_shows_names_and_status_only(base, monkeypatch, capsys):
    (base / ".env").write_text(f"LLM_API_KEY={LLM_KEY}\nLANGFUSE_SECRET_KEY=\n")
    # works from a coding agent's shell (no TTY)
    monkeypatch.setattr(set_secret, "stdin_is_tty", lambda: False)
    assert run(base, "--list") == 0
    shown = assert_not_shown(capsys, None, LLM_KEY)
    lines = {line.split()[0]: line for line in shown.splitlines() if line.startswith("  ")}
    assert " set " in lines["LLM_API_KEY"] and "not set" in lines["client_secret"]
    assert "required" in lines["client_id"] and "not secret" in lines["agent_identity"]


def test_files_git_would_commit_are_refused(base, monkeypatch, capsys):
    (base / ".gitignore").write_text(".venv/\n")
    Terminal(monkeypatch, hidden=[LLM_KEY])
    assert run(base, "--name", "LLM_API_KEY") == 1
    assert not (base / ".env").exists()
    assert "not git-ignored" in capsys.readouterr().err


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_git_decides_in_a_repository(base, monkeypatch, capsys):
    monkeypatch.undo()  # the real _git
    monkeypatch.chdir(base.parent.parent)
    monkeypatch.setattr(set_secret, "stdin_is_tty", lambda: True)
    subprocess.run(["git", "init", "-q"], check=True)
    Terminal(monkeypatch, hidden=[LLM_KEY])
    assert run(base, "--name", "LLM_API_KEY") == 0  # ignored by src/backend/.gitignore
    subprocess.run(["git", "add", "-f", "src/backend/.env"], check=True)
    Terminal(monkeypatch, typed=["y"], hidden=["new-key-123456789"])
    assert run(base, "--name", "LLM_API_KEY") == 1
    assert "tracked by git" in capsys.readouterr().err


def test_templates_are_never_written(base, capsys):
    assert run(base, "--env-file", str(base / ".env.example"), "--name", "LLM_API_KEY") == 1
    assert "committed template" in capsys.readouterr().err


def _deploy_template(base: Path) -> None:
    src = BACKEND / ".env.prod.example"
    if src.is_file():
        shutil.copy(src, base / ".env.prod.example")
    else:
        (base / ".env.prod.example").write_text(
            "APP_ENV=prod\nLLM_API_KEY=<set-in-deploy>\nLANGFUSE_SECRET_KEY=\n"
        )


def test_deploy_env_file_is_filled_from_its_template(base, monkeypatch, capsys):
    _deploy_template(base)
    target = base / ".env.prod"
    term = Terminal(monkeypatch, hidden=[LLM_KEY], typed=[""])  # Enter = the <set-in-deploy> keys
    assert run(base, "--env-file", str(target)) == 0
    text = target.read_text()
    assert f"LLM_API_KEY={LLM_KEY}\n" in text and "=<set-in-deploy>" not in text
    assert mode(target) == 0o600 and not (base / ".greennode.json").exists()
    shown = assert_not_shown(capsys, term, LLM_KEY)
    assert "client_id" not in shown  # IAM is never in a deploy env file
    # deploy/env_file.py: the strict parser the platform agrees with
    lint = BACKEND.parent.parent / "deploy" / "env_file.py"
    if lint.is_file():
        spec = importlib.util.spec_from_file_location("env_file", lint)
        env_file = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(env_file)
        values, errors = env_file.parse(target)
        assert errors == [] and values["LLM_API_KEY"] == LLM_KEY

    assert run(base, "--env-file", str(target), "--name", "iam") == 1
    assert "never go in an env file" in capsys.readouterr().err


def test_referenced_secret_vars_are_offered(base, monkeypatch, capsys):
    agents = {
        "agents": {
            "stock": {"url": "${A2A_STOCK_AGENT_URL}", "api_key": "${A2A_STOCK_AGENT_API_KEY}"},
            "off": {"url": "x", "api_key": "${A2A_OFF_API_KEY}", "enabled": False},
        }
    }
    (base / "a2a_agents.json").write_text(json.dumps(agents))
    (base / ".env").write_text("LLM_API_KEY=\n")
    assert run(base, "--list") == 0
    listed = capsys.readouterr().out
    assert "A2A_STOCK_AGENT_API_KEY" in listed and "refs" in listed
    assert "A2A_STOCK_AGENT_URL" not in listed and "A2A_OFF_API_KEY" not in listed

    # declared by a2a_agents.json: no "add it?" question
    term = Terminal(monkeypatch, hidden=["peer-key-0123456789"])
    assert run(base, "--name", "A2A_STOCK_AGENT_API_KEY") == 0
    env = (base / ".env").read_text()
    assert env == "LLM_API_KEY=\nA2A_STOCK_AGENT_API_KEY=peer-key-0123456789\n"
    assert_not_shown(capsys, term, "peer-key-0123456789")

    term = Terminal(monkeypatch, typed=["n"])  # any other new key: confirmed first (a typo?)
    assert run(base, "--name", "OTHER_API_KEY") == 0
    assert "OTHER_API_KEY" not in (base / ".env").read_text() and term.hidden == []


def test_a_declared_non_secret_key_gets_a_plain_prompt(base, monkeypatch, capsys):
    term = Terminal(monkeypatch, typed=["qwen-test-model"])  # visible input, no getpass
    assert run(base, "--name", "LLM_MODEL") == 0
    assert "LLM_MODEL=qwen-test-model\n" in (base / ".env").read_text() and term.hidden == []


def test_unknown_names_are_refused(base, capsys):
    assert run(base, "--name", "llm_key") == 1  # lower case: a typo, never a new key
    assert "unknown name" in capsys.readouterr().err


def test_interrupt_keeps_what_was_saved(base, monkeypatch, capsys):
    term = Terminal(monkeypatch, hidden=[CID, KeyboardInterrupt()])
    assert run(base, "--name", "iam") == 130
    assert json.loads((base / ".greennode.json").read_text())["client_id"] == CID
    assert "Stopped" in assert_not_shown(capsys, term, CID)


def test_runs_on_the_system_python3():
    """`make set-secret` uses python3 before `uv sync`: macOS ships 3.9. Syntax check + stdlib-only imports."""
    source = Path(set_secret.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source, feature_version=(3, 9))
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import | ast.ImportFrom)
        for alias in (
            node.names if isinstance(node, ast.Import) else [ast.alias(node.module or "")]
        )
    }
    assert imported <= set(sys.stdlib_module_names) | {"__future__"}
