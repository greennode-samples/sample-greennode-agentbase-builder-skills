"""Standard MCP server to deploy on AgentBase Runtime and attach to the MCP Gateway as a Custom Connector.

Runtime contract: 0.0.0.0:8080, GET /health 200 (no auth). MCP endpoint: POST /mcp (streamable HTTP,
stateless ⇒ scale to many replicas without sticky sessions). Connector URL = <runtime endpoint>/mcp.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Annotated

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field
from starlette.requests import Request
from starlette.responses import JSONResponse

from auth import build_verifier
from backend import Backend
from settings import get_settings
from store import NotesStore

settings = get_settings()
logging.basicConfig(level="INFO")
log = logging.getLogger(settings.server_name)

verifier = build_verifier(settings)
store = NotesStore(settings.db_path)
backend = Backend(settings.backend_url, settings.backend_timeout_s, settings.backend_token)
mcp = FastMCP(
    settings.server_name,
    instructions="Short description of what this MCP server does — the LLM reads this.",
    host="0.0.0.0",
    port=settings.port,
    streamable_http_path="/mcp",
    stateless_http=True,
    json_response=True,
    token_verifier=verifier,
    auth=AuthSettings(
        issuer_url=settings.issuer or settings.resource_url,
        resource_server_url=settings.resource_url,
        required_scopes=settings.required_scopes or None,
        validate_token_resource=False,  # JwtVerifier checks `aud` itself (MCP_AUDIENCE)
    )
    if verifier
    else None,
)


@mcp.custom_route("/health", methods=["GET"])
async def health(_: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


# ----------------------------------------------------------------------------- authz helpers
class Forbidden(ToolError):
    pass


def require_scope(scope: str) -> None:
    tok = get_access_token()
    if tok is not None and settings.auth_mode != "none" and scope not in tok.scopes:
        raise Forbidden(f"Missing scope '{scope}'")


def current_user() -> str:
    """End-user identity from the verified token (OAuth 3LO / inbound forward). NEVER from parameters."""
    if settings.auth_mode == "none":  # local dev only (Settings blocks it elsewhere)
        return settings.dev_user
    tok = get_access_token()
    if tok is None or not tok.subject:
        raise Forbidden("This tool requires user identity (OAuth 3LO or Inbound forward JWT)")
    return tok.subject


# ----------------------------------------------------------------------------- sample tools
# Patterns (references/tool-design.md): typed + described inputs (Field) · Pydantic return ⇒ structured
# output · annotations (read-only / destructive / idempotent) · pagination · ToolError messages for the LLM.


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
async def server_time() -> str:
    """Current server time (UTC, ISO 8601). Needs no user identity."""
    return datetime.now(UTC).isoformat(timespec="seconds")


# --- shared data from an internal system (no user identity needed; works with API Key 2LO)
class Product(BaseModel):
    sku: str
    name: str
    price: float
    currency: str
    in_stock: bool


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
async def get_product(
    sku: Annotated[
        str, Field(pattern=r"^[A-Z0-9-]{3,32}$", description="Product SKU, e.g. SKU-1001")
    ],
) -> Product:
    """Look up one product in the catalog by SKU: name, price, stock. Use when the user asks about a
    specific product. Requires scope `catalog.read`."""
    require_scope("catalog.read")
    return Product.model_validate(await backend.get_json(f"/products/{sku}"))


# --- per-user data (identity from the token, every query filtered by owner)
class Note(BaseModel):
    id: int
    text: str
    created_at: str


class NotePage(BaseModel):
    items: list[Note]
    total: int
    next_offset: int | None = Field(
        description="Pass as `offset` to get the next page; null = last"
    )


@mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True)
)
async def add_note(
    text: Annotated[str, Field(min_length=1, max_length=2000, description="Note content")],
    idempotency_key: Annotated[
        str | None,
        Field(max_length=64, description="Optional. Same key ⇒ same note, no duplicate on retry"),
    ] = None,
) -> Note:
    """Save a note for the CURRENT USER. Requires scope `notes.write`."""
    require_scope("notes.write")
    user = current_user()
    note_id = await store.add(user, text, idempotency_key)
    log.info("note added user=%s id=%d", user, note_id)
    return Note(id=note_id, text=text, created_at=datetime.now(UTC).isoformat(timespec="seconds"))


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def list_notes(
    limit: Annotated[int, Field(ge=1, le=50, description="Page size")] = 20,
    offset: Annotated[int, Field(ge=0, description="From `next_offset` of the previous page")] = 0,
) -> NotePage:
    """List the CURRENT USER's notes, oldest first, paginated (never sees other users' notes).
    Requires scope `notes.read`."""
    require_scope("notes.read")
    rows, total = await store.list(current_user(), limit, offset)
    nxt = offset + len(rows)
    return NotePage(
        items=[Note(id=r.id, text=r.text, created_at=r.created_at) for r in rows],
        total=total,
        next_offset=nxt if nxt < total else None,
    )


@mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True)
)
async def delete_note(
    note_id: Annotated[int, Field(ge=1, description="`id` from list_notes")],
) -> str:
    """Permanently delete one of the CURRENT USER's notes. Destructive ⇒ the agent should confirm first
    (HITL). Requires scope `notes.write`."""
    require_scope("notes.write")
    user = current_user()
    if not await store.delete(user, note_id):
        # Same message for other users' notes: don't leak that the id exists
        raise ToolError("Note not found.")
    log.info("note deleted user=%s id=%d", user, note_id)
    return f"Deleted note {note_id}."


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
