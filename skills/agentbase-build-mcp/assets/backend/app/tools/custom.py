"""YOUR local tools — the one file to add them to. A seed: template upgrades never merge into it.

Add a tool in four steps (the full recipe, with HITL, idempotency, tests and eval items, is
`/agentbase-build` → `references/add-a-tool.md`):

    @tool
    async def create_ticket(title: str, config: RunnableConfig) -> str:
        \"\"\"Open a support ticket for the current user. Use when they ask to report a problem.\"\"\"
        user_id = config["configurable"]["actor_id"]  # NEVER a parameter: the model would be writing the identity
        ...

    def custom_tools() -> list[BaseTool]:
        return [create_ticket]

A tool that changes something outside the agent (write / send / pay / delete) must also be listed in HITL_TOOLS of
every env file — `make security-scan` (S3) tells you where it is missing. Something shared with other agents, or
reaching an internal system: an MCP server instead (`/agentbase-build-mcp-server`), same tool name.
"""

from __future__ import annotations

from langchain_core.tools import BaseTool


def custom_tools() -> list[BaseTool]:
    """Registered next to the built-in tools (get_current_time, search_knowledge) by get_local_tools()."""
    return []
