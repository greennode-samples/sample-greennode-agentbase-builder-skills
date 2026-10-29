"""Multi-tenant and roles: tenant from the token only, per-tenant user ids, fail-closed tool gating."""

from __future__ import annotations

import pytest
from greennode_agentbase.exceptions import GreenNodeRequestError
from langchain_core.tools import tool

from app.auth import inbound
from app.config import get_settings
from tests.test_auth import _ctx, jwt_env  # noqa: F401  (fixture)


@pytest.fixture
def tenant_env(jwt_env, monkeypatch):  # noqa: F811
    monkeypatch.setenv("AUTH_TENANT_CLAIM", "org_id")
    monkeypatch.setenv("AUTH_ROLES_CLAIM", "realm_access.roles")
    get_settings.cache_clear()
    yield jwt_env
    get_settings.cache_clear()


def _auth(token, user=None):
    return inbound.authenticate(_ctx(token, user), get_settings())


def test_same_person_in_two_tenants_is_two_users(tenant_env):
    a = _auth(tenant_env("alice", org_id="acme"))
    b = _auth(tenant_env("alice", org_id="globex"))
    assert a.tenant_id == "acme" and b.tenant_id == "globex"
    assert a.user_id != b.user_id and a.user_id.startswith("u-")
    assert a.user_id == inbound.user_id_from_subject("alice", "https://issuer.test", "acme")


def test_tenant_and_subject_cannot_be_reshuffled(tenant_env):
    # "a" + "b|c" vs "a|b" + "c": a plain concatenation would give both the same id
    x = inbound.user_id_from_subject("b|c", "iss", "a")
    y = inbound.user_id_from_subject("c", "iss", "a|b")
    assert x != y


def test_token_without_tenant_is_refused(tenant_env):
    with pytest.raises(GreenNodeRequestError) as e:
        _auth(tenant_env("alice"))
    assert e.value.status_code == 403


@pytest.mark.parametrize("bad", ["../acme", "", "a" * 200, "acme/x"])
def test_unsafe_tenant_id_is_refused(tenant_env, bad):
    with pytest.raises(GreenNodeRequestError) as e:
        _auth(tenant_env("alice", org_id=bad))
    assert e.value.status_code == 403


def test_tenant_allowlist(tenant_env, monkeypatch):
    monkeypatch.setenv("AUTH_ALLOWED_TENANTS", '["acme"]')
    get_settings.cache_clear()
    assert _auth(tenant_env("alice", org_id="acme")).tenant_id == "acme"
    with pytest.raises(GreenNodeRequestError) as e:
        _auth(tenant_env("alice", org_id="globex"))
    assert e.value.status_code == 403


def test_raw_subject_header_is_not_accepted_with_tenants(tenant_env):
    token = tenant_env("alice", org_id="acme")
    with pytest.raises(GreenNodeRequestError) as e:
        _auth(token, user="alice")  # without tenants "alice" would match the raw sub
    assert e.value.status_code == 403
    scoped = inbound.user_id_from_subject("alice", "https://issuer.test", "acme")
    assert _auth(token, user=scoped).user_id == scoped


def test_allowed_tenants_require_the_claim(monkeypatch):
    monkeypatch.setenv("AUTH_ALLOWED_TENANTS", '["acme"]')
    monkeypatch.delenv("AUTH_TENANT_CLAIM", raising=False)
    get_settings.cache_clear()
    with pytest.raises(ValueError, match="AUTH_TENANT_CLAIM"):
        get_settings()
    get_settings.cache_clear()


def test_roles_from_nested_list_or_space_separated(tenant_env, monkeypatch):
    p = _auth(tenant_env("bob", org_id="acme", realm_access={"roles": ["admin", "user"]}))
    assert p.roles == {"admin", "user"}
    monkeypatch.setenv("AUTH_ROLES_CLAIM", "scope")
    get_settings.cache_clear()
    assert _auth(tenant_env("bob", org_id="acme", scope="read write")).roles == {"read", "write"}


def test_tool_roles_fail_closed(monkeypatch):
    monkeypatch.setenv("TOOL_ROLES", '{"delete_*": ["admin"], "*_invoice": ["finance", "admin"]}')
    get_settings.cache_clear()
    s = get_settings()
    admin = inbound.Principal(user_id="u1", roles=frozenset({"admin"}))
    finance = inbound.Principal(user_id="u2", roles=frozenset({"finance"}))
    nobody = inbound.Principal(user_id="u3")
    assert inbound.tool_allowed("delete_invoice", admin, s)
    assert not inbound.tool_allowed(
        "delete_invoice", finance, s
    )  # every matching pattern must allow it
    assert inbound.tool_allowed("create_invoice", finance, s)
    assert not inbound.tool_allowed("create_invoice", nobody, s)
    assert inbound.tool_allowed("get_current_time", nobody, s)
    get_settings.cache_clear()


async def test_role_gated_tools_are_hidden_from_the_model(monkeypatch):
    from app import tools as tools_pkg

    @tool
    def delete_record(record_id: str) -> str:
        """Delete a record."""
        return "deleted"

    monkeypatch.setattr(tools_pkg, "get_local_tools", lambda: [delete_record])
    monkeypatch.setenv("TOOL_ROLES", '{"delete_*": ["admin"]}')
    get_settings.cache_clear()
    s = get_settings()
    user = inbound.Principal(user_id="u1", roles=frozenset({"user"}))
    admin = inbound.Principal(user_id="u2", roles=frozenset({"admin"}))
    assert "delete_record" not in [t.name for t in await tools_pkg.collect_tools(s, user)]
    assert "delete_record" in [t.name for t in await tools_pkg.collect_tools(s, admin)]
    get_settings.cache_clear()
