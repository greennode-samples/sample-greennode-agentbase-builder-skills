"""Tool tests — copy a block per new tool. Minimum per tool:
happy path · input validation · missing scope · per-user isolation (if it touches user data) ·
upstream failures (if it calls an internal system) · annotations.
"""

from __future__ import annotations

import httpx
from conftest import JWT_ENV, call, list_tools, make_token, text


# ----------------------------------------------------------------------------- notes (per-user, DB)
async def test_notes_per_user_isolation(jwt_server):
    alice, bob = make_token("alice"), make_token("bob")
    await call(jwt_server, alice, "add_note", {"text": "alice's secret"})
    a = await call(jwt_server, alice, "list_notes")
    b = await call(jwt_server, bob, "list_notes")
    assert a.structuredContent["total"] == 1
    assert b.structuredContent == {"items": [], "total": 0, "next_offset": None}


async def test_add_note_is_idempotent_with_key(local_server):
    args = {"text": "pay rent", "idempotency_key": "k-1"}
    first = await call(local_server, None, "add_note", args)
    retry = await call(local_server, None, "add_note", args)
    assert first.structuredContent["id"] == retry.structuredContent["id"]
    assert (await call(local_server, None, "list_notes")).structuredContent["total"] == 1


async def test_add_note_validates_input(local_server):
    res = await call(local_server, None, "add_note", {"text": ""})
    assert res.isError


async def test_list_notes_paginates(local_server):
    for i in range(5):
        await call(local_server, None, "add_note", {"text": f"n{i}"})
    page1 = (await call(local_server, None, "list_notes", {"limit": 2})).structuredContent
    assert [n["text"] for n in page1["items"]] == ["n0", "n1"] and page1["next_offset"] == 2
    last = await call(local_server, None, "list_notes", {"limit": 2, "offset": 4})
    assert last.structuredContent["next_offset"] is None
    assert (await call(local_server, None, "list_notes", {"limit": 500})).isError  # le=50


async def test_cannot_delete_other_users_note(jwt_server):
    alice, bob = make_token("alice"), make_token("bob")
    note_id = (await call(jwt_server, alice, "add_note", {"text": "x"})).structuredContent["id"]
    res = await call(jwt_server, bob, "delete_note", {"note_id": note_id})
    assert res.isError and "not found" in text(res).lower()  # no hint that the note exists
    assert not (await call(jwt_server, alice, "delete_note", {"note_id": note_id})).isError


# ----------------------------------------------------------------------------- get_product (internal system)
def _catalog(request: httpx.Request) -> httpx.Response:
    """Fake internal system. SKU decides the outcome."""
    sku = request.url.path.rsplit("/", 1)[-1]
    if sku == "SKU-1001":
        return httpx.Response(
            200,
            json={"sku": sku, "name": "Lamp", "price": 19.5, "currency": "USD", "in_stock": True},
        )
    if sku == "SKU-SLOW":
        raise httpx.ReadTimeout("slow", request=request)
    if sku == "SKU-BOOM":
        return httpx.Response(500, text="Traceback ... db password=hunter2")
    return httpx.Response(404)


async def test_get_product_ok(start_server):
    base = start_server(JWT_ENV, backend=_catalog)
    res = await call(base, make_token("alice"), "get_product", {"sku": "SKU-1001"})
    assert not res.isError and res.structuredContent["name"] == "Lamp"


async def test_get_product_upstream_failures_are_clean(start_server):
    base = start_server(JWT_ENV, backend=_catalog)
    tok = make_token("alice")
    cases = {"SKU-404": "not found", "SKU-SLOW": "timed out", "SKU-BOOM": "error (500)"}
    for sku, expected in cases.items():
        res = await call(base, tok, "get_product", {"sku": sku})
        assert res.isError and expected in text(res).lower()
        assert "hunter2" not in text(res) and "backend.test" not in text(res)  # no leaks


async def test_get_product_validation_and_scope(start_server):
    base = start_server(JWT_ENV, backend=_catalog)
    bad = await call(base, make_token("alice"), "get_product", {"sku": "../etc"})
    assert bad.isError
    no_scope = await call(
        base, make_token("alice", scope="notes.read"), "get_product", {"sku": "SKU-1001"}
    )
    assert no_scope.isError and "catalog.read" in text(no_scope)


async def test_get_product_not_configured_locally(local_server):
    res = await call(local_server, None, "get_product", {"sku": "SKU-1001"})
    assert res.isError and "MCP_BACKEND_URL" in text(res)


# ----------------------------------------------------------------------------- metadata
async def test_tool_annotations(local_server):
    tools = {t.name: t for t in await list_tools(local_server)}
    assert tools["delete_note"].annotations.destructiveHint is True
    assert tools["list_notes"].annotations.readOnlyHint is True
    assert tools["get_product"].outputSchema is not None  # structured output
