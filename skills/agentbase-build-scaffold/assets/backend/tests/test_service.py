"""Request lifecycle: what the caller sees when a backing service fails, test isolation from the developer's
.env, and where the local server listens."""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest
from greennode_agentbase import RequestContext
from greennode_agentbase.exceptions import GreenNodeRequestError

from app import service
from app.config import get_settings

LEAK = "HTTP 403 error: principal iam:sa-1234 not allowed memory:GetEvents on mem-secret"


def ctx(session: str = "svc-1") -> RequestContext:
    return RequestContext(session_id=session, user_id="alice")


def _sdk_fails(monkeypatch, status: int) -> None:
    """Memory / Identity SDK error raised inside the run (here: while collecting tools)."""

    async def failing(settings, principal):
        raise GreenNodeRequestError(LEAK, status_code=status)

    monkeypatch.setattr(service, "collect_tools", failing)


@pytest.mark.parametrize(
    ("sdk_status", "expected"), [(403, 502), (404, 502), (429, 503), (500, 503)]
)
async def test_sdk_errors_never_reach_the_caller_verbatim(
    monkeypatch, caplog, sdk_status, expected
):
    """A 403 from Memory used to be forwarded as is: internals leaked, and frontends logged the user out."""
    _sdk_fails(monkeypatch, sdk_status)
    with pytest.raises(GreenNodeRequestError) as e:
        await service.handle({"message": "hi"}, ctx())
    assert e.value.status_code == expected and "iam:sa" not in e.value.message
    assert type(e.value).__name__ == "GreenNodeRequestError"  # `error_type` in the SDK's JSON body
    assert LEAK in caplog.text  # the original stays in the server log

    events = [ev async for ev in await service.handle({"message": "hi", "stream": True}, ctx())]
    assert events == [{"event": "error", "message": events[0]["message"], "status": expected}]
    assert "iam:sa" not in events[0]["message"]


async def test_client_errors_still_pass_through(fake_llm):
    with pytest.raises(GreenNodeRequestError) as e:
        await service.handle({"message": "x" * (service.MAX_MESSAGE_CHARS + 1)}, ctx())
    assert e.value.status_code == 400 and "exceeds" in e.value.message


def test_hostile_dotenv_does_not_change_settings_in_tests(tmp_path, monkeypatch):
    """A realistic developer .env (other tiers, streaming off, custom header…) used to fail 21 tests locally
    while CI was green: conftest makes Settings ignore .env and load_dotenv() a no-op."""
    import dotenv

    (tmp_path / ".env").write_text(
        'LLM_MODEL=from-dotenv\nLLM_MODEL_LARGE=big-model\nLLM_TIER_FALLBACKS={"large":["fb"]}\n'
        "AUTH_TOKEN_HEADER=X-GreenNode-AgentBase-Custom-User-Token\nLTM_AUTO_RECALL=false\n"
        "LLM_STREAMING=false\nAUTH_MODE=jwt\n"
    )
    monkeypatch.chdir(tmp_path)
    assert not dotenv.load_dotenv()  # what main.py / evals call
    assert "LLM_MODEL_LARGE" not in os.environ
    get_settings.cache_clear()
    s = get_settings()
    assert (s.llm_model, s.llm_model_large, s.llm_tier_fallbacks) == ("fake-model", "", {})
    assert s.auth_mode == "none" and s.auth_token_header == "Authorization"
    assert s.ltm_auto_recall and s.llm_streaming


@pytest.mark.parametrize(
    ("app_env", "auth_mode", "in_docker", "host"),
    [
        ("local", "none", False, "127.0.0.1"),  # no auth: LAN callers could act as any user
        ("local", "none", True, "0.0.0.0"),  # container: make docker-run publishes on 127.0.0.1
        ("local", "jwt", False, "0.0.0.0"),  # authenticated: a phone on the LAN may call it
        ("prod", "jwt", False, "0.0.0.0"),  # Runtime (no /.dockerenv, never local)
    ],
)
def test_bind_host(tmp_path, app_env, auth_mode, in_docker, host):
    import main

    dockerenv = tmp_path / ".dockerenv"
    if in_docker:
        dockerenv.touch()
    s = SimpleNamespace(is_local=app_env == "local", auth_mode=auth_mode)
    assert main.bind_host(s, dockerenv) == host
