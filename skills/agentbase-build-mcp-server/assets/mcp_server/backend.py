"""Client for the internal system the tools expose (example: a product catalog API).

Pattern: one shared httpx.AsyncClient (connection pool), explicit timeout, every failure mapped to a short
ToolError the LLM can act on — never leak stack traces, internal URLs or raw upstream bodies.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import BaseModel, ValidationError

log = logging.getLogger(__name__)

UNEXPECTED = "Internal system returned an unexpected response."


class Backend:
    def __init__(
        self,
        base_url: str,
        timeout_s: float,
        token: str = "",
        transport: httpx.AsyncBaseTransport | None = None,  # tests inject httpx.MockTransport
    ):
        headers = {"Authorization": f"Bearer {token}"} if token else None
        self._client = (
            httpx.AsyncClient(
                base_url=base_url, timeout=timeout_s, headers=headers, transport=transport
            )
            if base_url
            else None
        )

    async def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        if self._client is None:
            raise ToolError("Internal system is not configured (MCP_BACKEND_URL is empty).")
        try:
            r = await self._client.get(path, params=params)
        except httpx.TimeoutException:
            log.warning("backend timeout path=%s", path)
            raise ToolError("Internal system timed out. Try again later.") from None
        except httpx.HTTPError as e:
            log.warning("backend unreachable path=%s err=%s", path, type(e).__name__)
            raise ToolError("Internal system is unreachable. Try again later.") from None
        if r.status_code == 404:
            raise ToolError("Not found.")
        if r.status_code in (401, 403):
            log.error("backend rejected our credential status=%s path=%s", r.status_code, path)
            raise ToolError("This MCP server is not allowed to access that resource.")
        if r.status_code >= 400:
            log.error("backend error status=%s path=%s", r.status_code, path)
            raise ToolError(f"Internal system error ({r.status_code}). Try again later.")
        try:
            return r.json()
        except ValueError:  # an HTML login/error page, truncated body… — never echo it to the LLM
            log.error(
                "backend returned non-JSON status=%s path=%s content-type=%s",
                r.status_code,
                path,
                r.headers.get("content-type"),
            )
            raise ToolError(UNEXPECTED) from None

    async def get_model[M: BaseModel](
        self, model: type[M], path: str, params: dict[str, Any] | None = None
    ) -> M:
        """GET + validate against the contract `model`. Upstream contract drift (renamed / missing fields)
        becomes a clean ToolError; the pydantic error (it embeds the raw upstream record, which may hold
        internal fields or credentials) is logged without input values and never reaches the LLM."""
        data = await self.get_json(path, params)
        try:
            return model.model_validate(data)
        except ValidationError as e:
            fields = [
                (".".join(map(str, err["loc"])), err["type"])
                for err in e.errors(include_url=False, include_input=False, include_context=False)
            ]
            log.error("backend contract drift %s path=%s errors=%s", model.__name__, path, fields)
            raise ToolError(UNEXPECTED) from None
