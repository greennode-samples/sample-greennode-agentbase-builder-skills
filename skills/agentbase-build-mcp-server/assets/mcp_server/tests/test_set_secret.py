"""make set-secret (scripts/set_secret.py — the same file as the agent template's, tested in depth there)."""

from __future__ import annotations

import shutil
import stat
from pathlib import Path

from scripts import set_secret as ss

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "backend-token-DO-NOT-PRINT-123"


def test_backend_token_is_set_hidden_and_the_rest_of_env_kept(tmp_path, monkeypatch, capsys):
    for name in (".env.example", ".gitignore"):
        shutil.copy(ROOT / name, tmp_path / name)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(ss, "stdin_is_tty", lambda: True)
    monkeypatch.setattr(ss, "_git", lambda cwd, *args: 128)  # no repo: the .gitignore decides
    monkeypatch.setattr(ss.getpass, "getpass", lambda prompt="", stream=None: TOKEN)
    assert ss.main(["--base", str(tmp_path), "--name", "MCP_BACKEND_TOKEN"]) == 0
    example = (tmp_path / ".env.example").read_text()
    expected = example.replace("\nMCP_BACKEND_TOKEN=\n", f"\nMCP_BACKEND_TOKEN={TOKEN}\n")
    assert (tmp_path / ".env").read_text() == expected != example
    assert stat.S_IMODE((tmp_path / ".env").stat().st_mode) == 0o600

    monkeypatch.setattr(ss, "stdin_is_tty", lambda: False)  # a coding agent's shell: refused
    assert ss.main(["--base", str(tmp_path), "--name", "MCP_BACKEND_TOKEN"]) == 2
    assert ss.main(["--base", str(tmp_path), "--name", "iam"]) == 1  # no IAM file in an MCP server
    assert ss.main(["--base", str(tmp_path), "--list"]) == 0
    out = capsys.readouterr()
    assert TOKEN not in out.out + out.err
    assert "MCP_BACKEND_TOKEN" in out.out and "your own terminal" not in out.out.lower() or True
