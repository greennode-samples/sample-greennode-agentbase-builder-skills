"""make check-creds: correct verdicts, and it NEVER prints a secret (users run it in chats with coding agents)."""

from __future__ import annotations

import json

import httpx
import pytest

from app.tools.mcp import IAMBearerAuth
from scripts import check_creds

SECRETS = {
    "client_secret": "iam-secret-DO-NOT-PRINT",
    "LLM_API_KEY": "llm-key-DO-NOT-PRINT",
    "LANGFUSE_SECRET_KEY": "lf-secret-DO-NOT-PRINT",
}


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    """Empty project dir: no developer .greennode.json / .env leaks in, no GREENNODE_* from the shell."""
    monkeypatch.chdir(tmp_path)
    for k in (
        "GREENNODE_CLIENT_ID",
        "GREENNODE_CLIENT_SECRET",
        "GREENNODE_AGENT_IDENTITY",
        "LANGFUSE_PUBLIC_KEY",
        "LANGFUSE_SECRET_KEY",
    ):
        monkeypatch.setenv(k, "x")  # registers the original value so teardown restores it…
        monkeypatch.delenv(k)  # …even though normalize_iam_env() writes GREENNODE_* during the test
    import greennode_agentbase.core.config as sdk_config

    monkeypatch.setattr(sdk_config, "_config_cache", None)  # SDK caches .greennode.json per process
    return tmp_path


def _creds_file(path, secret=SECRETS["client_secret"]):
    (path / ".greennode.json").write_text(
        json.dumps({"client_id": "cid-1", "client_secret": secret})
    )


def _fake_http(monkeypatch, llm=200, langfuse=200, iam=200, llm_body=b""):
    def refresh(self):
        if iam != 200:
            req = httpx.Request("POST", "https://iam.test")
            raise httpx.HTTPStatusError("x", request=req, response=httpx.Response(iam, request=req))
        self.principal = "iam:sa-123"

    monkeypatch.setattr(IAMBearerAuth, "refresh", refresh)
    monkeypatch.setattr(
        check_creds.httpx, "post", lambda *a, **k: httpx.Response(llm, content=llm_body)
    )
    monkeypatch.setattr(check_creds.httpx, "get", lambda *a, **k: httpx.Response(langfuse))


def test_all_ok_shows_principal_and_no_secret(workdir, monkeypatch, capsys):
    _creds_file(workdir)
    monkeypatch.setenv("LLM_API_KEY", SECRETS["LLM_API_KEY"])
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", SECRETS["LANGFUSE_SECRET_KEY"])
    _fake_http(monkeypatch)
    assert check_creds.main() == 0
    out = capsys.readouterr().out
    assert "iam:sa-123" in out
    assert not any(secret in out for secret in SECRETS.values())


def test_missing_iam_file_fails(workdir, monkeypatch, capsys):
    _fake_http(monkeypatch)
    assert check_creds.main() == 1
    assert ".greennode.json.example" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("kw", "expected"),
    [
        ({"iam": 401}, "rejected by IAM"),
        ({"llm": 401}, "LLM_API_KEY rejected"),
        (  # MaaS answer when the key is valid but the model is not enabled for it
            {"llm": 403, "llm_body": b'{"error":{"message":"model not allowed for this key"}}'},
            "not allowed for this LLM_API_KEY",
        ),
        ({"llm": 404}, "not usable"),
    ],
)
def test_rejected_credentials_fail(workdir, monkeypatch, capsys, kw, expected):
    _creds_file(workdir)
    _fake_http(monkeypatch, **kw)
    assert check_creds.main() == 1
    assert expected in capsys.readouterr().out


def test_langfuse_optional(workdir, monkeypatch, capsys):
    _creds_file(workdir)
    _fake_http(monkeypatch)
    assert check_creds.main() == 0
    assert "SKIP  Langfuse" in capsys.readouterr().out


def test_agent_identity_placeholder_fails(workdir, monkeypatch, capsys):
    import greennode_agentbase.core.config as sdk_config

    (workdir / ".greennode.json").write_text(
        json.dumps(
            {"client_id": "c", "client_secret": "s", "agent_identity": "<agent identity name>"}
        )
    )
    monkeypatch.setattr(sdk_config, "_config_cache", None)
    monkeypatch.delenv("GREENNODE_AGENT_IDENTITY", raising=False)
    _fake_http(monkeypatch)
    assert check_creds.main() == 1
    assert "placeholder" in capsys.readouterr().out


TIER_ENV = {
    "LLM_MODEL_LARGE": "big-model",  # replaces LLM_MODEL (fake-model) as the large primary
    "LLM_MODEL_SMALL": "small-model",
    "LLM_TIER_FALLBACKS": '{"large": ["fb-model", "small-model"]}',
}


def _per_model_http(monkeypatch, refused: set[str]) -> list[str]:
    """MaaS answers 403 "model not allowed for this key" for the `refused` models; returns the pinged models."""
    _fake_http(monkeypatch)
    pinged: list[str] = []

    def post(url, **kwargs):
        model = kwargs["json"]["model"]
        pinged.append(model)
        if model in refused:
            return httpx.Response(403, content=b'{"error":"model not allowed for this key"}')
        return httpx.Response(200)

    monkeypatch.setattr(check_creds.httpx, "post", post)
    return pinged


@pytest.mark.parametrize("settings_valid", [True, False])
def test_every_tier_model_is_pinged_once(workdir, monkeypatch, capsys, settings_valid):
    """A key may be valid for LLM_MODEL but not for another tier's model (seen: LLM_MODEL_LARGE ⇒ 403)."""
    _creds_file(workdir)
    for k, v in TIER_ENV.items():
        monkeypatch.setenv(k, v)
    if not settings_valid:  # resolved from the raw env instead of app.llm
        monkeypatch.setenv("MEMORY_BACKEND", "agentbase")
        monkeypatch.delenv("MEMORY_ID", raising=False)
    pinged = _per_model_http(monkeypatch, refused={"big-model"})
    assert check_creds.main() == 1
    assert sorted(pinged) == ["big-model", "fb-model", "small-model"]  # deduplicated
    out = capsys.readouterr().out
    assert "FAIL  LLM (MaaS)" in out and "[reasoning primary, large primary]" in out
    assert "not allowed for this LLM_API_KEY" in out


def test_broken_fallback_is_only_a_warning(workdir, monkeypatch, capsys):
    _creds_file(workdir)
    for k, v in TIER_ENV.items():
        monkeypatch.setenv(k, v)
    _per_model_http(monkeypatch, refused={"fb-model"})
    assert check_creds.main() == 0
    out = capsys.readouterr().out
    assert "WARN  LLM (MaaS)             [large fallback]" in out
    assert "[large fallback, small primary] key accepted" in out  # one ping, both roles


def test_greennode_json_example_has_an_empty_agent_identity(workdir, monkeypatch, capsys):
    """The template ships `"agent_identity": ""`: copied as is it is 'not set' (SKIP), never the placeholder FAIL."""
    from pathlib import Path

    example = Path(__file__).resolve().parent.parent / ".greennode.json.example"
    data = json.loads(example.read_text())
    assert data["agent_identity"] == ""
    (workdir / ".greennode.json").write_text(json.dumps(data))
    assert check_creds.check_agent_identity()[0] == "SKIP"
