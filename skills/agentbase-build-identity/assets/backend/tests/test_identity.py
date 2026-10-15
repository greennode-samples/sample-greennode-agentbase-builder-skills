"""Outbound credentials (AgentBase Identity) with a fake Identity API — no platform needed.

The fake keeps consent PER agent_user_id, like the platform: a user who has not consented gets an
authorization URL; after consent the next call returns that user's credential.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest
from greennode_agentbase import GreenNodeAgentBaseContext
from langchain_core.tools import tool

from app import identity
from app.config import get_settings
from app.identity import (
    AuthorizationRequired,
    agent_access_token,
    agent_api_key,
    user_access_token,
    user_api_key,
)


class FakeIdentityAPI:
    def __init__(self):
        self.consented: set[tuple[str, str]] = set()  # (provider, agent_user_id)
        self.calls: list[tuple[str, str, str | None]] = []  # (method, provider, agent_user_id)
        self.clients = 0  # IdentityClient instances built (each one = its own IAM token request)

    def client(self, *a, **k):
        api = self
        api.clients += 1

        class _Client:
            async def get_workload_access_token_async(self, workload_identity_name, user_id=None):
                # The SDK's api-key decorator asks for it when the request context has none (= on the Runtime)
                api.calls.append(("workload", workload_identity_name, None))
                return "iam-token"

            async def get_api_key_for_agent_identity_async(
                self, provider_name, agent_identity_name
            ):
                api.calls.append(("static", provider_name, None))
                return SimpleNamespace(apikey=f"static-{provider_name}")

            async def get_m2m_token_async(self, provider_name, agent_identity_name, request):
                api.calls.append(("m2m", provider_name, None))
                return SimpleNamespace(access_token=f"m2m-{provider_name}")

            async def get_3lo_token_async(self, provider_name, agent_identity_name, request):
                user = request.agent_user_id
                api.calls.append(("3lo", provider_name, user))
                if (provider_name, user) in api.consented:
                    return SimpleNamespace(
                        access_token=f"tok-{user}", authorization_url=None, session_id=None
                    )
                return SimpleNamespace(
                    access_token=None,
                    authorization_url=f"https://idp.test/auth?u={user}",
                    session_id="11111111-1111-4111-8111-111111111111",
                )

            async def get_delegated_api_key_for_agent_identity_async(
                self, provider_name, agent_identity_name, request
            ):
                user = request.agent_user_id
                api.calls.append(("delegated", provider_name, user))
                if (provider_name, user) in api.consented:
                    return SimpleNamespace(
                        apikey=f"key-{user}",
                        authorization_url=None,
                        session_id=None,
                        status="COMPLETED",
                    )
                return SimpleNamespace(
                    apikey=None,
                    authorization_url=f"https://agentbase.test/delegate?u={user}",
                    session_id="22222222-2222-4222-8222-222222222222",
                    status="IN_PROGRESS",
                )

        return _Client()


@pytest.fixture
def api(monkeypatch, tmp_path):
    import json

    import greennode_agentbase.core.config as sdk_config
    import greennode_agentbase.identity.auth as sdk_auth

    fake = FakeIdentityAPI()
    monkeypatch.setattr(sdk_auth, "IdentityClient", fake.client)
    monkeypatch.setattr(identity, "_shared_client", None)  # rebuilt from the fake on first use
    # Local dev as the skill prescribes: IAM pair + agent identity live in .greennode.json (not env vars)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".greennode.json").write_text(
        json.dumps({"client_id": "c", "client_secret": "s", "agent_identity": "test-agent"})
    )
    monkeypatch.delenv("GREENNODE_AGENT_IDENTITY", raising=False)
    monkeypatch.setattr(
        sdk_config, "_config_cache", None
    )  # the SDK caches .greennode.json per process
    monkeypatch.setenv("IDENTITY_CALLBACK_URL", "https://app.test/oauth/done")
    # conftest runs AUTH_MODE=none: the user id is asserted, so per-user credentials need the explicit opt-in
    monkeypatch.setenv("IDENTITY_TRUST_ASSERTED_USER", "true")
    get_settings.cache_clear()
    # The Runtime does NOT set a workload token in the request context (only with the X-GreenNode-AgentBase-
    # Access-Token header) ⇒ test that production path, not a preset token
    GreenNodeAgentBaseContext.set_workload_access_token(None)
    return fake


def as_user(user_id: str) -> None:
    GreenNodeAgentBaseContext.set_user_id(user_id)


# ----------------------------------------------------------------------------- per-user (3LO / delegated)
@user_access_token("google", scopes=["calendar.readonly"])
async def _events(day: str, *, access_token: str) -> str:
    return f"{day}:{access_token}"


async def test_3lo_returns_link_without_blocking_then_works_after_consent(api):
    as_user("alice")
    started = time.monotonic()
    with pytest.raises(AuthorizationRequired) as e:
        # wait_for: if the SDK's 600s poller were used, FAIL after 2s instead of hanging the suite
        await asyncio.wait_for(_events("mon"), timeout=2)
    assert time.monotonic() - started < 2
    assert e.value.url == "https://idp.test/auth?u=alice"
    assert "https://idp.test/auth?u=alice" in e.value.tool_message
    # str(e) carries the link too: a tool WITHOUT try/except still shows it (ToolNode handle_tool_errors)
    assert "https://idp.test/auth?u=alice" in str(e.value)

    api.consented.add(("google", "alice"))  # user opened the link and consented
    assert await _events("mon") == "mon:tok-alice"


async def test_3lo_credentials_are_per_user(api):
    api.consented.add(("google", "alice"))
    as_user("alice")
    assert await _events("tue") == "tue:tok-alice"
    as_user("bob")  # bob never consented ⇒ must not get alice's token
    with pytest.raises(AuthorizationRequired) as e:
        await _events("tue")
    assert "u=bob" in e.value.url
    assert {u for m, _, u in api.calls if m == "3lo"} == {"alice", "bob"}


@user_api_key("user-openai")
async def _ask_with_user_key(q: str, *, api_key: str) -> str:
    return api_key


async def test_delegated_api_key_flow(api):
    as_user("carol")
    with pytest.raises(AuthorizationRequired) as e:
        await _ask_with_user_key("hi")
    assert "delegate?u=carol" in e.value.url
    api.consented.add(("user-openai", "carol"))
    assert await _ask_with_user_key("hi") == "key-carol"


async def test_per_user_refused_without_user_or_callback(api, monkeypatch):
    GreenNodeAgentBaseContext._user_id.set(None)  # e.g. a background job with no authenticated user
    with pytest.raises(PermissionError):
        await _events("wed")
    as_user("alice")
    monkeypatch.setenv("IDENTITY_CALLBACK_URL", "")
    get_settings.cache_clear()
    with pytest.raises(ValueError, match="IDENTITY_CALLBACK_URL"):
        await _events("wed")
    assert not any(m == "3lo" for m, _, _ in api.calls)  # refused before calling the platform


# ----------------------------------------------------------------------------- agent-wide (M2M)
@agent_api_key("weather-key")
async def _weather(city: str, *, api_key: str) -> str:
    return f"{city}:{api_key}"


@agent_access_token("crm", scopes=["crm.read"])
async def _crm(*, access_token: str) -> str:
    return access_token


async def test_agent_wide_credentials(api):
    assert await _weather("hanoi") == "hanoi:static-weather-key"
    assert await _crm() == "m2m-crm"


async def test_missing_agent_identity_fails_instead_of_creating_one(api, tmp_path):
    import greennode_agentbase.core.config as sdk_config

    (tmp_path / ".greennode.json").write_text('{"client_id": "c", "client_secret": "s"}')
    sdk_config._config_cache = None  # re-read the file without agent_identity
    with pytest.raises(ValueError, match="GREENNODE_AGENT_IDENTITY"):
        await _weather("hue")
    assert api.calls == []


# ----------------------------------------------------------------------------- tool pattern
@tool
async def list_events(day: str) -> str:
    """List the user's calendar events for a day."""
    try:
        return await _events(day)
    except AuthorizationRequired as e:
        return e.tool_message


async def test_tool_schema_never_exposes_the_credential(api):
    assert set(list_events.args) == {"day"}  # access_token is injected into the INNER function only
    as_user("dave")
    out = await list_events.ainvoke({"day": "fri"})
    assert out.startswith(identity.AUTH_REQUIRED_PREFIX) and "u=dave" in out


async def test_service_sets_verified_user_in_sdk_context(monkeypatch, fake_llm):
    """Per-user credentials are keyed by the SDK context user id: every run path (here run_chat, used by eval,
    jobs and A2A) must set it from the verified principal — never leave a stale/None value."""
    from langchain_core.messages import AIMessage

    import app.service as service

    seen = {}
    original = service.collect_tools

    async def spy(settings, principal):
        seen["ctx_user"] = GreenNodeAgentBaseContext.get_user_id()
        return await original(settings, principal)

    monkeypatch.setattr(service, "collect_tools", spy)
    GreenNodeAgentBaseContext._user_id.set("someone-else")  # stale value from another request
    fake_llm(AIMessage("ok"))
    await service.run_chat("hi", user_id="erin", session_id="s-ctx")
    assert seen["ctx_user"] == "erin"


# ----------------------------------------------------------------------------- who the user is
async def test_config_user_fallback_is_not_a_user(api, tmp_path):
    """GreenNodeAgentBaseContext.get_user_id() falls back to `agent_user_id` from .greennode.json — a fixed
    user for every request. Per-user credentials must use the request's own (verified) user only."""
    import greennode_agentbase.core.config as sdk_config

    (tmp_path / ".greennode.json").write_text(
        '{"client_id": "c", "client_secret": "s", "agent_identity": "test-agent", '
        '"agent_user_id": "victim"}'
    )
    sdk_config._config_cache = None
    GreenNodeAgentBaseContext._user_id.set(None)
    assert GreenNodeAgentBaseContext.get_user_id() == "victim"  # the SDK's fallback
    with pytest.raises(PermissionError):
        await _events("thu")
    with pytest.raises(PermissionError):
        await _ask_with_user_key("hi")
    assert not any(u == "victim" for _, _, u in api.calls)


async def test_asserted_user_needs_the_explicit_opt_in(api, monkeypatch):
    """api_key / none: the caller only ASSERTS the user id ⇒ any caller could use any user's connected
    accounts. Refused unless IDENTITY_TRUST_ASSERTED_USER=true; a JWT-proven user needs no opt-in."""
    monkeypatch.setenv("IDENTITY_TRUST_ASSERTED_USER", "false")
    get_settings.cache_clear()
    as_user("alice")
    with pytest.raises(PermissionError, match="IDENTITY_TRUST_ASSERTED_USER"):
        await _events("fri")
    with pytest.raises(PermissionError, match="IDENTITY_TRUST_ASSERTED_USER"):
        await _ask_with_user_key("hi")
    assert api.calls == []
    monkeypatch.setenv("AUTH_MODE", "jwt")
    monkeypatch.setenv("AUTH_JWKS_URL", "http://127.0.0.1:1/jwks.json")
    get_settings.cache_clear()
    api.consented.add(("google", "alice"))
    assert await _events("fri") == "fri:tok-alice"


async def test_one_identity_client_for_every_call(api):
    """requires_access_token built 2 new IdentityClients (2 IAM token requests) per call; the helpers share one."""
    api.consented.add(("google", "alice"))
    as_user("alice")
    for _ in range(3):
        assert await _crm() == "m2m-crm"
        assert await _events("sat") == "sat:tok-alice"
        assert await _weather("hue") == "hue:static-weather-key"
    assert api.clients == 1
    # OAuth2 helpers call the Identity API directly — no workload-token round trip per call
    assert [m for m, _, _ in api.calls if m != "workload"] == ["m2m", "3lo", "static"] * 3
