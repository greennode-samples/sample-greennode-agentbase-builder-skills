"""Tool registry — the ONLY place that collects tools for the graph."""

from __future__ import annotations

from langchain_core.tools import BaseTool

from app.a2a.client import build_a2a_tools
from app.auth.inbound import Principal
from app.config import Settings
from app.memory.long_term import build_memory_tools, get_ltm
from app.observability import tracing
from app.tools.local_tools import get_local_tools
from app.tools.mcp import get_mcp_tools


async def collect_tools(settings: Settings, principal: Principal) -> list[BaseTool]:
    with tracing.step("tools.collect") as st:
        local = get_local_tools()
        ltm = get_ltm(settings)
        memory = build_memory_tools(ltm, settings) if ltm else []
        mcp_errors: dict[str, str] = {}
        mcp = await get_mcp_tools(settings, principal, errors=mcp_errors)
        a2a = build_a2a_tools(settings, principal)
        tools: list[BaseTool] = [*local, *memory, *mcp, *a2a]
        names = [t.name for t in tools]
        dupes = {n for n in names if names.count(n) > 1}
        if dupes:
            raise ValueError(f"Duplicate tool names: {sorted(dupes)}")
        st.set(
            output={
                "local": [t.name for t in local],
                "memory": [t.name for t in memory],
                "mcp": [t.name for t in mcp],
                "a2a": [t.name for t in a2a],
                "mcp_errors": mcp_errors,
            },
            metadata={"tool_count": len(tools)},
            level="WARNING" if mcp_errors else None,
        )
        return tools
