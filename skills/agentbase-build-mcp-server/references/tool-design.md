# Designing MCP tools

The LLM chooses and calls tools only from what the server exposes: name, docstring, input schema, annotations. Treat those as the API contract. Samples to copy are in `assets/mcp_server/server.py`: `get_product` (shared data from an internal system) and `add_note`, `list_notes`, `delete_note` (per-user data in a DB).

## 1. Contract

| Element | Rule | Sample |
|---|---|---|
| Name | `verb_noun`, snake_case, unique across the agent's servers. The agent sees `<server>_<tool>` | `get_product`, `list_notes` |
| Docstring | First line says what it does. Then when to use it and the required scope. The LLM reads all of it | `get_product` |
| Inputs | Typed with `Annotated[T, Field(description=…, constraints)]`: `pattern`, `ge`, `le`, `min_length`, `max_length`. Pydantic rejects bad input before your code runs | `sku` pattern, `limit` `le=50` |
| Output | Return a Pydantic model. FastMCP then publishes `outputSchema` and returns `structuredContent` plus JSON text. Use `str` only for short confirmations | `Product`, `NotePage` |
| Annotations | `ToolAnnotations(readOnlyHint, destructiveHint, idempotentHint, openWorldHint)`. These are hints for the client, not enforcement | `delete_note` uses `destructiveHint=True` |

- **Never** take `user_id`, `owner` or `tenant` as an argument. Identity comes from `current_user()`.
- Keep the arguments to the minimum the LLM can actually know. Use IDs from a previous tool's output (`note_id` from `list_notes`) rather than free text.
- Make every tool that has `destructiveHint=True` or causes an external side effect a candidate for the agent's `HITL_TOOLS` (`<server>_<tool>`, see `/agentbase-build-hitl`). Give it its own scope (see `oauth.md` §5).

## 2. Data per user (`store.py`)

- Every store method takes `owner` as its first parameter, and every query has `WHERE owner = ?`, including UPDATE and DELETE. A "not found" for another user's row must look exactly like a truly missing row, so the response doesn't leak that it exists.
- Writes: put `UNIQUE(owner, idempotency_key)` on the table and accept an optional `idempotency_key`. LLM agents retry, and a retry must not create a duplicate.
  - Make it **atomic**: `INSERT … ON CONFLICT (owner, idempotency_key) DO NOTHING`, then `SELECT` the row by `(owner, idempotency_key)` in the **same transaction** (Postgres: same statements, default READ COMMITTED). SELECT-then-INSERT races: concurrent retries hit the UNIQUE index and the raw `UNIQUE constraint failed` SQL error reaches the LLM.
  - A replay returns the **stored** row (original text, id, `created_at`), not the new arguments — `store.add()` returns the row.
- Wrap blocking I/O in `asyncio.to_thread`, or use an async driver. Never block the event loop: one server handles many concurrent calls.
- Local uses SQLite (a file, `MCP_DB_PATH`). With **more than 1 Runtime replica**, keep the same interface and switch to Postgres (asyncpg / SQLAlchemy async) with a connection pool. Schema changes then go through migrations (Alembic), not `CREATE TABLE IF NOT EXISTS`.

## 3. Calling an internal system (`backend.py`)

- Use one shared `httpx.AsyncClient` (connection pool). Always set an explicit timeout (`MCP_BACKEND_TIMEOUT_S`) below the agent's MCP tool timeout.
- Map every failure to a short `ToolError` the LLM can act on:

  | Upstream | Message to the LLM | Log |
  |---|---|---|
  | timeout / connect error | "timed out / unreachable. Try again later." | WARNING |
  | 404 | "Not found." | — |
  | 401/403 | "not allowed to access that resource" | ERROR (our credential is wrong) |
  | 5xx / other 4xx | "Internal system error (status)." | ERROR |

  Never put upstream bodies, stack traces, internal hostnames or credentials in the message. The sample has a test for this (`hunter2`, `backend.test`).
- Credentials for the internal system:
  - **Service credential** (`MCP_BACKEND_TOKEN`): use it when the data is shared, or when the internal API accepts an `owner` filter from a trusted caller. Set it in the runtime env or secret store.
  - **User's token**: only when the internal system shares the IdP with the MCP server (3LO / inbound forward). Pass `get_access_token().token` through. Never log it.
- Validate the upstream response with a Pydantic model (`Product.model_validate`). If the upstream contract drifts, the tool then fails loudly instead of handing the LLM garbage.

## 4. Output size

LLM context is limited and expensive.
- **Paginate** list tools: take `limit` (with `le=`) and `offset` or a cursor, and return `total` plus `next_offset`.
- Return only the fields the LLM needs, not the raw upstream record.
- For large text (documents, logs), truncate to a fixed budget and say so in the output (`"truncated": true`), or return an excerpt plus an ID for a follow-up tool.

## 5. Tests per tool (`tests/test_tools.py`)

Cover the minimum with the fixtures in `tests/conftest.py`:

| Case | How |
|---|---|
| happy path + structured output | `call(...)`, then assert on `res.structuredContent` |
| invalid input | wrong type, empty, out of range ⇒ `res.isError` |
| missing scope | `make_token("u", scope="…without it…")` |
| per-user isolation | two tokens (`alice`, `bob`) on `jwt_server` |
| idempotent write | same `idempotency_key` twice ⇒ same stored row; 8 concurrent retries (`session(...)` + `asyncio.gather`) ⇒ one row, no error |
| upstream failures | `start_server(JWT_ENV, backend=handler)` with a fake handler returning 404/500/timeout |
| annotations | `list_tools(...)`, then check `annotations` and `outputSchema` |
