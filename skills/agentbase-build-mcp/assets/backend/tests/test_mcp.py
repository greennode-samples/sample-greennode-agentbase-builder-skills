from __future__ import annotations

import base64
import contextlib
import json
import socket
import threading
import time

import httpx
import uvicorn
from langchain_core.tools import StructuredTool, ToolException
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from app.tools.mcp import _guard_policy, iam_principal


def _tool(behavior, name: str = "tavily_search"):
    async def coro(query: str):
        return behavior(query)

    return StructuredTool.from_function(
        coroutine=coro,
        name=name,
        description="search",
        response_format="content_and_artifact",
        handle_tool_error=True,  # like langchain-mcp-adapters tools,
    )


async def test_policy_denied_exception_becomes_clear_message():
    def deny(_):
        raise ToolException("Request denied by policy.")

    tool = _guard_policy(_tool(deny), "tavily")
    out = await tool.ainvoke({"query": "x"})
    assert out.startswith("POLICY_DENIED") and "Do NOT call" in out


async def test_normal_result_passthrough_and_other_errors():
    ok = _guard_policy(_tool(lambda q: (f"result {q}", None)), "tavily")
    assert await ok.ainvoke({"query": "a"}) == "result a"

    def boom(_):
        raise ToolException("upstream timeout")

    other = _guard_policy(_tool(boom), "tavily")
    assert "upstream timeout" in await other.ainvoke({"query": "a"})  # other errors: unchanged


def test_iam_principal_from_token_sub():
    payload = base64.urlsafe_b64encode(json.dumps({"sub": "abc-123"}).encode()).decode().rstrip("=")
    assert iam_principal(f"h.{payload}.s") == "iam:abc-123"
    assert iam_principal("garbage") is None


async def test_denials_are_per_tool_then_per_connector():
    """One denied tool must not block the connector's ALLOWED tools (policies list actions per tool); the
    same tool is short-circuited, and 2 distinct denied tools ⇒ the whole connector for the turn."""
    from app.tools.mcp import reset_policy_denials

    calls = []

    def deny(q):
        calls.append(q)
        raise ToolException("Request denied by policy.")

    reset_policy_denials()
    quote = _guard_policy(_tool(deny, "stock_quote"), "stock")
    gainers = _guard_policy(_tool(deny, "stock_gainers"), "stock")
    losers = _guard_policy(_tool(deny, "stock_losers"), "stock")
    first = await quote.ainvoke({"query": "a"})
    assert first.startswith("POLICY_DENIED") and "Do NOT call this tool again" in first
    assert (await quote.ainvoke({"query": "a2"})).startswith("POLICY_DENIED")
    assert calls == ["a"]  # same tool: short-circuited
    second = await gainers.ainvoke({"query": "b"})  # another tool still reaches the gateway
    assert calls == ["a", "b"] and "any tool of this connector" in second
    assert (await losers.ainvoke({"query": "c"})).startswith("POLICY_DENIED")
    assert calls == ["a", "b"]  # 2 distinct denies ⇒ connector short-circuited
    reset_policy_denials()  # new request ⇒ retry normally
    await losers.ainvoke({"query": "d"})
    assert calls == ["a", "b", "d"]


async def test_policy_all_or_nothing_short_circuits_at_first_deny():
    from app.tools.mcp import reset_policy_denials

    calls = []

    def deny(q):
        calls.append(q)
        raise ToolException("Request denied by policy.")

    reset_policy_denials()
    t1 = _guard_policy(_tool(deny, "a"), "stock", all_or_nothing=True)
    t2 = _guard_policy(_tool(deny, "b"), "stock", all_or_nothing=True)
    assert "any tool of this connector" in await t1.ainvoke({"query": "1"})
    assert (await t2.ainvoke({"query": "2"})).startswith("POLICY_DENIED")
    assert calls == ["1"]


async def test_failing_server_negative_cached(monkeypatch, tmp_path):
    import json

    import app.tools.mcp as mcp_mod
    from app.auth.inbound import Principal
    from app.config import get_settings

    calls = []

    class _Boom:
        def __init__(self, *a, **k):
            pass

        async def get_tools(self, server_name):
            calls.append(server_name)
            raise ConnectionError("upstream down")

    cfg = tmp_path / "mcp.json"
    cfg.write_text(json.dumps({"servers": {"down": {"url": "https://x/mcp", "auth": "none"}}}))
    monkeypatch.setenv("MCP_CONFIG_FILE", str(cfg))
    get_settings.cache_clear()
    monkeypatch.setattr(mcp_mod, "MultiServerMCPClient", _Boom)
    mcp_mod._failed.clear()
    errors: dict = {}
    assert await mcp_mod.get_mcp_tools(get_settings(), Principal(user_id="u"), errors) == []
    assert await mcp_mod.get_mcp_tools(get_settings(), Principal(user_id="u"), errors) == []
    assert calls == ["down"]  # 2nd time skipped thanks to negative cache
    assert "skipped" in errors["down"]


async def test_per_user_server_failure_not_cached_for_others(monkeypatch, tmp_path):
    import json

    import app.tools.mcp as mcp_mod
    from app.auth.inbound import Principal
    from app.config import get_settings

    calls = []

    class _Boom:
        def __init__(self, *a, **k):
            pass

        async def get_tools(self, server_name):
            calls.append(server_name)
            # NOT an auth error: only `per_user` (not `_is_auth_error`) may keep it out of the cache
            raise ConnectionError("upstream down")

    cfg = tmp_path / "mcp.json"
    cfg.write_text(json.dumps({"servers": {"gh": {"url": "https://x/mcp", "auth": "user_jwt"}}}))
    monkeypatch.setenv("MCP_CONFIG_FILE", str(cfg))
    get_settings.cache_clear()
    monkeypatch.setattr(mcp_mod, "MultiServerMCPClient", _Boom)
    mcp_mod._failed.clear()
    await mcp_mod.get_mcp_tools(get_settings(), Principal(user_id="a", token="t-a"), {})
    assert "gh" not in mcp_mod._failed  # user A's error does not block user B
    errors: dict = {}
    await mcp_mod.get_mcp_tools(get_settings(), Principal(user_id="b", token="t-b"), errors)
    assert calls == ["gh", "gh"] and "skipped" not in errors["gh"]  # user B really retried


async def test_policy_text_in_successful_result_is_not_a_deny():
    out = await _guard_policy(
        _tool(lambda q: ("Article: what is Request denied by policy?", None)), "web"
    ).ainvoke({"query": "x"})
    assert out.startswith("Article")


# --- mcp_servers.json `auth` fails closed: the agent's IAM token is never sent by accident
def _load(monkeypatch, tmp_path, servers: dict) -> dict:
    import json

    import app.tools.mcp as mcp_mod
    from app.config import get_settings

    cfg = tmp_path / "mcp.json"
    cfg.write_text(json.dumps({"servers": servers}))
    monkeypatch.setenv("MCP_CONFIG_FILE", str(cfg))
    get_settings.cache_clear()
    return mcp_mod.load_mcp_config(get_settings())


def test_server_without_auth_is_skipped_not_given_iam(monkeypatch, tmp_path, caplog):
    loaded = _load(monkeypatch, tmp_path, {"thirdparty": {"url": "https://mcp.example.com/mcp"}})
    assert loaded == {}
    assert '"auth" is required' in caplog.text


def test_authorization_header_conflicts_with_iam(monkeypatch, tmp_path, caplog):
    entry = {"url": "https://x/mcp", "auth": "iam", "headers": {"Authorization": "Bearer k"}}
    assert _load(monkeypatch, tmp_path, {"notes": entry}) == {}
    assert "conflicts with auth" in caplog.text


def test_static_key_with_auth_none_is_sent_and_no_iam(monkeypatch, tmp_path):
    import app.tools.mcp as mcp_mod
    from app.auth.inbound import Principal
    from app.config import get_settings

    monkeypatch.setenv("NOTES_MCP_KEY", "k-123")
    entry = {"url": "http://localhost:8080/mcp", "auth": "none"}
    entry["headers"] = {"Authorization": "Bearer ${NOTES_MCP_KEY}"}
    cfg = _load(monkeypatch, tmp_path, {"notes": entry})["notes"]
    conn = mcp_mod._connection(cfg, Principal(user_id="u"), get_settings())
    assert conn["headers"]["Authorization"] == "Bearer k-123"
    assert "auth" not in conn  # no IAMBearerAuth attached


def test_stdio_needs_no_auth_and_iam_to_non_gateway_warns(monkeypatch, tmp_path, caplog):
    servers = {
        "fetch": {"transport": "stdio", "command": "uvx", "args": ["mcp-server-fetch"]},
        "odd": {"url": "https://mcp.example.com/mcp", "auth": "iam"},
        "gw": {"url": "https://gw-a-b.agentbase-gateway.aiplatform.vngcloud.vn/x", "auth": "iam"},
    }
    assert set(_load(monkeypatch, tmp_path, servers)) == {"fetch", "odd", "gw"}
    assert "'odd' uses auth=iam" in caplog.text and "'gw' uses auth=iam" not in caplog.text


def test_user_jwt_to_non_gateway_warns(monkeypatch, tmp_path, caplog):
    servers = {
        "self": {"url": "https://mcp.example.com/mcp", "auth": "user_jwt"},
        "gw": {
            "url": "https://gw-a-b.agentbase-gateway.aiplatform.vngcloud.vn/x",
            "auth": "user_jwt",
        },
    }
    assert set(_load(monkeypatch, tmp_path, servers)) == {"self", "gw"}  # warn, not refuse
    assert "'self' uses auth=user_jwt" in caplog.text and "end user's JWT" in caplog.text
    assert "'gw' uses auth=user_jwt" not in caplog.text


# --- ${VAR} is expanded inside parsed string values, never over the raw JSON text
def test_env_value_with_quote_or_backslash_does_not_break_the_file(monkeypatch, tmp_path):
    monkeypatch.setenv("ODD_KEY", 'k"1\\2')
    servers = {
        "a": {"url": "http://localhost:1/mcp", "auth": "none", "headers": {"X-Key": "${ODD_KEY}"}},
        "b": {"url": "http://localhost:2/mcp", "auth": "none"},
    }
    loaded = _load(monkeypatch, tmp_path, servers)
    assert set(loaded) == {"a", "b"}
    assert loaded["a"]["headers"]["X-Key"] == 'k"1\\2'


def test_env_value_cannot_inject_keys(monkeypatch, tmp_path):
    evil = 'https://evil.example/mcp", "auth": "iam", "x": "'
    monkeypatch.setenv("NOTES_URL", evil)
    loaded = _load(monkeypatch, tmp_path, {"notes": {"auth": "none", "url": "${NOTES_URL}"}})
    assert loaded["notes"]["auth"] == "none" and loaded["notes"]["url"] == evil


def test_unset_env_var_skips_only_that_server(monkeypatch, tmp_path, caplog):
    monkeypatch.delenv("NOPE_GATEWAY_URL", raising=False)
    monkeypatch.setenv("EMPTY_KEY", "")
    servers = {
        "gw": {"url": "${NOPE_GATEWAY_URL}/tavily", "auth": "iam"},
        "empty": {
            "url": "http://localhost:1/mcp",
            "auth": "none",
            "headers": {"K": "${EMPTY_KEY}"},
        },
        "off": {"url": "${ALSO_UNSET}/x", "auth": "iam", "enabled": False},
        "ok": {"url": "http://localhost:2/mcp", "auth": "none"},
    }
    assert set(_load(monkeypatch, tmp_path, servers)) == {"ok"}
    assert "'gw' skipped" in caplog.text and "NOPE_GATEWAY_URL" in caplog.text
    assert "'empty' skipped" in caplog.text and "EMPTY_KEY" in caplog.text
    assert "ALSO_UNSET" not in caplog.text  # disabled entries are not expanded


# --- A fake MCP Gateway over real streamable HTTP (uvicorn thread): JSON-RPC answers, configurable
# HTTP status for tools/list and tools/call — the official gateway answers a policy DENY with 403.
class _Seen(list):
    """JSON-RPC methods received, in order; `.auth` = (method, Authorization header) per request."""

    def __init__(self) -> None:
        super().__init__()
        self.auth: list[tuple[str, str | None]] = []


@contextlib.contextmanager
def _fake_gateway(
    *,
    list_status: int = 200,
    call_status: int = 200,
    call_headers: dict[str, str] | None = None,
    tools: tuple[str, ...] = ("tavily_search", "tavily_extract"),
    call_result=None,  # (tool name) -> MCP CallToolResult dict, e.g. {"content": [...], "isError": True}
):
    methods = _Seen()

    async def endpoint(request: Request) -> Response:
        if request.method != "POST":
            return Response(status_code=405)
        msg = await request.json()
        method = msg.get("method", "")
        methods.append(method)
        methods.auth.append((method, request.headers.get("authorization")))
        if "id" not in msg:  # notification
            return Response(status_code=202)
        if method == "initialize":
            result = {
                "protocolVersion": msg["params"]["protocolVersion"],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake-gateway", "version": "0"},
            }
        elif method == "tools/list":
            if list_status != 200:
                return JSONResponse({"message": "upstream failed"}, status_code=list_status)
            schema = {"type": "object", "properties": {"query": {"type": "string"}}}
            result = {
                "tools": [{"name": n, "description": n, "inputSchema": schema} for n in tools]
            }
        elif method == "tools/call":
            if call_status != 200:
                body = {"message": "No policy allows this request"}
                return JSONResponse(body, status_code=call_status, headers=call_headers)
            result = (call_result or (lambda _: None))(msg["params"]["name"]) or {
                "content": [{"type": "text", "text": "ok"}],
                "isError": False,
            }
        else:
            result = {}
        return JSONResponse({"jsonrpc": "2.0", "id": msg["id"], "result": result})

    app = Starlette(routes=[Route("/{connector}", endpoint, methods=["GET", "POST", "DELETE"])])
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(app, lifespan="off", log_level="warning"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.01)
    try:
        yield f"http://127.0.0.1:{sock.getsockname()[1]}", methods
    finally:
        server.should_exit = True
        thread.join(5)


def _use_config(monkeypatch, tmp_path, servers: dict):
    import app.tools.mcp as mcp_mod
    from app.config import get_settings

    cfg = tmp_path / "mcp.json"
    cfg.write_text(json.dumps({"servers": servers}))
    monkeypatch.setenv("MCP_CONFIG_FILE", str(cfg))
    get_settings.cache_clear()
    for name in servers:
        mcp_mod._static_tools.pop(name, None)
        mcp_mod._failed.pop(name, None)
    return get_settings()


async def test_gateway_http_403_is_policy_deny_and_short_circuits(monkeypatch, tmp_path):
    import app.tools.mcp as mcp_mod
    from app.auth.inbound import Principal

    events = []
    monkeypatch.setattr(mcp_mod.tracing, "event", lambda name, **kw: events.append((name, kw)))
    with _fake_gateway(call_status=403) as (base, methods):
        settings = _use_config(
            monkeypatch, tmp_path, {"tavily": {"url": f"{base}/tavily", "auth": "none"}}
        )
        mcp_mod.reset_policy_denials()
        tools = {t.name: t for t in await mcp_mod.get_mcp_tools(settings, Principal(user_id="u"))}
        first = await tools["tavily_tavily_search"].ainvoke({"query": "a"})
        second = await tools["tavily_tavily_extract"].ainvoke({"query": "b"})
        third = await tools["tavily_tavily_search"].ainvoke({"query": "c"})
    assert all(x.startswith("POLICY_DENIED") for x in (first, second, third))
    assert methods.count("tools/call") == 2  # 2 distinct tools denied ⇒ the 3rd call never left
    denied = [kw["metadata"] for n, kw in events if n == "mcp.policy_denied"]
    assert [(d["scope"], d["short_circuit"]) for d in denied] == [
        ("tool", False),
        ("connector", False),
        ("connector", True),
    ]


async def test_real_adapter_iserror_deny_blocks_only_that_tool(monkeypatch, tmp_path):
    """MCP isError "Request denied by policy." (adapter _MCPToolExecutionError) ⇒ POLICY_DENIED for that
    tool; the connector's allowed tool still works; another isError text reaches the LLM unchanged."""
    import app.tools.mcp as mcp_mod
    from app.auth.inbound import Principal

    def result(name: str) -> dict:
        texts = {"create_issue": "Request denied by policy.", "close_issue": "Issue #7 is locked"}
        if name in texts:
            return {"content": [{"type": "text", "text": texts[name]}], "isError": True}
        return {"content": [{"type": "text", "text": "3 issues"}], "isError": False}

    gateway = _fake_gateway(
        tools=("search_issues", "create_issue", "close_issue"), call_result=result
    )
    with gateway as (base, methods):
        settings = _use_config(
            monkeypatch, tmp_path, {"github": {"url": f"{base}/github", "auth": "none"}}
        )
        mcp_mod.reset_policy_denials()
        tools = {t.name: t for t in await mcp_mod.get_mcp_tools(settings, Principal(user_id="u"))}
        denied = await tools["github_create_issue"].ainvoke({"query": "x"})
        allowed = await tools["github_search_issues"].ainvoke({"query": "x"})
        server_error = await tools["github_close_issue"].ainvoke({"query": "x"})
    assert denied.startswith("POLICY_DENIED") and "github_create_issue" in denied
    assert "3 issues" in str(allowed)  # used to be short-circuited for the whole turn
    assert "Issue #7 is locked" in str(server_error)  # the server's own isError text, untouched
    assert "POLICY_DENIED" not in str(server_error) and "MCP_ERROR" not in str(server_error)
    assert methods.count("tools/call") == 3


async def test_upstream_failure_is_clean_text_for_the_llm(monkeypatch, tmp_path):
    """ToolNode used to put `HTTPStatusError(... url '…?tavilyApiKey=SECRET')` + "Please fix your mistakes"
    into the LLM context, checkpoint and traces."""
    from langchain_core.messages import AIMessage
    from langgraph.graph import START, MessagesState, StateGraph
    from langgraph.prebuilt import ToolNode

    import app.tools.mcp as mcp_mod
    from app.auth.inbound import Principal

    events = []
    monkeypatch.setattr(mcp_mod.tracing, "event", lambda name, **kw: events.append((name, kw)))
    with _fake_gateway(call_status=502) as (base, _):
        url = f"{base}/tavily?tavilyApiKey=SECRET-123"
        settings = _use_config(monkeypatch, tmp_path, {"tavily": {"url": url, "auth": "none"}})
        mcp_mod.reset_policy_denials()
        tools = await mcp_mod.get_mcp_tools(settings, Principal(user_id="u"))
        g = StateGraph(MessagesState)
        g.add_node("tools", ToolNode(tools, handle_tool_errors=True))
        g.add_edge(START, "tools")
        call = {"name": "tavily_tavily_search", "args": {"query": "q"}, "id": "c1"}
        out = await g.compile().ainvoke({"messages": [AIMessage("", tool_calls=[call])]})
    content = str(out["messages"][-1].content)
    assert content.startswith("MCP_ERROR (tavily)") and "HTTP 502" in content, content
    assert "retry at most once" in content
    for leak in ("SECRET-123", "127.0.0.1", "tavilyApiKey", "Please fix"):
        assert leak not in content, content
    assert [kw["metadata"]["error"] for n, kw in events if n == "mcp.tool_error"] == ["HTTP 502"]


def test_llm_error_summary_strips_urls():
    from app.tools.mcp import _llm_error_summary

    err = ConnectionError(
        "cannot reach https://gw-x.example/tavily?tavilyApiKey=SECRET-1 (refused)"
    )
    summary = _llm_error_summary(ExceptionGroup("tg", [err]))
    assert summary == "ConnectionError: cannot reach <url> (refused)"
    assert _llm_error_summary(httpx.ReadTimeout("slow")) == "timeout"


async def test_server_insufficient_scope_is_not_a_policy_deny(monkeypatch, tmp_path):
    """403 + WWW-Authenticate error="insufficient_scope" comes from the MCP SERVER (RFC 6750): a scope
    misconfiguration, not a Policy Group deny ⇒ distinct message/event, no deny recorded."""
    import app.tools.mcp as mcp_mod
    from app.auth.inbound import Principal

    events = []
    monkeypatch.setattr(mcp_mod.tracing, "event", lambda name, **kw: events.append((name, kw)))
    www = 'Bearer error="insufficient_scope", error_description="Required scope: notes.write"'
    with _fake_gateway(call_status=403, call_headers={"WWW-Authenticate": www}) as (base, methods):
        settings = _use_config(
            monkeypatch, tmp_path, {"notes": {"url": f"{base}/notes", "auth": "none"}}
        )
        mcp_mod.reset_policy_denials()
        tools = {t.name: t for t in await mcp_mod.get_mcp_tools(settings, Principal(user_id="u"))}
        out = await tools["notes_tavily_search"].ainvoke({"query": "a"})
        await tools["notes_tavily_extract"].ainvoke({"query": "b"})
        await tools["notes_tavily_search"].ainvoke({"query": "c"})
    assert out.startswith("MCP_SCOPE_ERROR (notes)") and "not a Policy Group deny" in out
    assert [n for n, _ in events] == ["mcp.scope_error"] * 3
    assert methods.count("tools/call") == 3  # never treated as a deny ⇒ nothing short-circuited


def test_http_403_nested_in_exception_groups_is_a_deny_but_401_is_not():
    from app.tools.mcp import _is_policy_denied

    def http_error(status: int) -> httpx.HTTPStatusError:
        req = httpx.Request("POST", "https://gw/tavily")
        return httpx.HTTPStatusError("x", request=req, response=httpx.Response(status, request=req))

    nested = ExceptionGroup("outer", [ExceptionGroup("inner", [http_error(403)])])
    assert _is_policy_denied(nested)
    assert not _is_policy_denied(ExceptionGroup("outer", [http_error(401)]))
    assert not _is_policy_denied(ToolException("upstream timeout"))


async def test_list_tools_trace_and_errors_hide_url_query_secret(monkeypatch, tmp_path, caplog):
    import app.tools.mcp as mcp_mod
    from app.auth.inbound import Principal

    spans: list = []

    class _Handle:
        def set(self, **kw):
            spans.append(("set", kw))

    @contextlib.contextmanager
    def step(name, *, input=None, **_):
        spans.append((name, input))
        yield _Handle()

    monkeypatch.setattr(mcp_mod.tracing, "step", step)
    errors: dict = {}
    with _fake_gateway(list_status=502) as (base, _):
        url = f"{base}/tavily?tavilyApiKey=SECRET-123"
        settings = _use_config(monkeypatch, tmp_path, {"tavily": {"url": url, "auth": "none"}})
        assert await mcp_mod.get_mcp_tools(settings, Principal(user_id="u"), errors) == []
    name, span_input = spans[0]
    assert name == "mcp.list_tools" and span_input["url"] == f"{base}/tavily"  # no query string
    assert (
        "502" in errors["tavily"]
    )  # the real (leaf) HTTP error, not "unhandled errors in a TaskGroup"
    leaked = [str(spans), str(errors), caplog.text, str(mcp_mod._failed)]
    assert not [x for x in leaked if "SECRET-123" in x]


# --- tool list: allow_tools, "prefix": false, 5-minute cache, per-user headers
async def test_allow_tools_and_prefix_false(monkeypatch, tmp_path):
    import app.tools.mcp as mcp_mod
    from app.auth.inbound import Principal

    with _fake_gateway() as (base, _):
        servers = {
            "web": {"url": f"{base}/web", "auth": "none", "allow_tools": ["tavily_search"]},
            "kb": {
                "url": f"{base}/kb",
                "auth": "none",
                "prefix": False,
                "allow_tools": ["tavily_extract"],
            },
        }
        settings = _use_config(monkeypatch, tmp_path, servers)
        tools = await mcp_mod.get_mcp_tools(settings, Principal(user_id="u"))
    # allow_tools uses the ORIGINAL MCP names; prefix=false keeps them as the agent-side names
    assert sorted(t.name for t in tools) == ["tavily_extract", "web_tavily_search"]


async def test_prefix_false_name_collision_fails_collect_tools(monkeypatch, tmp_path):
    """An unprefixed MCP tool named like a local tool: which one would the LLM call? ⇒ loud error."""
    import pytest

    from app.auth.inbound import Principal
    from app.tools import collect_tools

    with _fake_gateway(tools=("get_current_time",)) as (base, _):
        settings = _use_config(
            monkeypatch, tmp_path, {"kb": {"url": f"{base}/kb", "auth": "none", "prefix": False}}
        )
        with pytest.raises(ValueError, match="Duplicate tool names.*get_current_time"):
            await collect_tools(settings, Principal(user_id="u"))


def test_flags_must_be_booleans(monkeypatch, tmp_path, caplog):
    servers = {"kb": {"url": "https://x/mcp", "auth": "none", "prefix": "no"}}
    assert _load(monkeypatch, tmp_path, servers) == {}
    assert '"prefix" must be true or false' in caplog.text


async def test_static_tool_list_cached_five_minutes(monkeypatch, tmp_path):
    import app.tools.mcp as mcp_mod
    from app.auth.inbound import Principal

    with _fake_gateway() as (base, methods):
        settings = _use_config(
            monkeypatch, tmp_path, {"tavily": {"url": f"{base}/tavily", "auth": "none"}}
        )
        await mcp_mod.get_mcp_tools(settings, Principal(user_id="a"))
        await mcp_mod.get_mcp_tools(settings, Principal(user_id="b"))
        assert methods.count("tools/list") == 1  # 2nd request (another user) served from the cache
        ts, cached = mcp_mod._static_tools["tavily"]
        mcp_mod._static_tools["tavily"] = (ts - mcp_mod._STATIC_TTL_S - 1, cached)  # 5 min later
        await mcp_mod.get_mcp_tools(settings, Principal(user_id="a"))
        assert methods.count("tools/list") == 2


async def test_user_jwt_is_sent_per_user_and_never_cached(monkeypatch, tmp_path):
    import app.tools.mcp as mcp_mod
    from app.auth.inbound import Principal

    with _fake_gateway() as (base, methods):
        settings = _use_config(
            monkeypatch, tmp_path, {"gh": {"url": f"{base}/gh", "auth": "user_jwt"}}
        )
        alice = await mcp_mod.get_mcp_tools(settings, Principal(user_id="a", token="TOKEN-A"))
        bob = await mcp_mod.get_mcp_tools(settings, Principal(user_id="b", token="TOKEN-B"))
        assert "gh" not in mcp_mod._static_tools and methods.count("tools/list") == 2
        methods.auth.clear()
        await alice[0].ainvoke({"query": "x"})
        alice_headers = {a for m, a in methods.auth}
        methods.auth.clear()
        await bob[0].ainvoke({"query": "x"})
        bob_headers = {a for m, a in methods.auth}
    assert alice_headers == {"Bearer TOKEN-A"} and bob_headers == {"Bearer TOKEN-B"}


# --- negative cache: only real auth errors (status / PermissionError) are never cached
def test_auth_error_detection_ignores_digits_in_urls():
    from app.tools.mcp import _is_auth_error

    req = httpx.Request("POST", "https://gw/x")
    unauthorized = httpx.HTTPStatusError(
        "x", request=req, response=httpx.Response(401, request=req)
    )
    assert _is_auth_error(ExceptionGroup("tg", [unauthorized]))
    assert _is_auth_error(PermissionError("no user JWT"))
    # "gw-search-54037" contains "403": used to count as an auth error ⇒ the failure was never cached
    url_err = ConnectionError(
        "connect to https://gw-search-54037.agentbase-gateway.example/x failed"
    )
    assert not _is_auth_error(url_err)
    assert not _is_auth_error(ConnectionError("Unauthorized network zone 401-B"))


# --- mcp_servers.json: credentials only over https, parsed once
def test_credential_auth_requires_https_except_loopback(monkeypatch, tmp_path, caplog):
    servers = {
        "plain": {"url": "http://mcp.example.com/mcp", "auth": "iam"},
        "plain_jwt": {"url": "http://10.0.0.5:8080/mcp", "auth": "user_jwt"},
        "plain_none": {"url": "http://10.0.0.5:8080/mcp", "auth": "none"},
        "local": {"url": "http://127.0.0.1:8081/mcp", "auth": "user_jwt"},
        "local_name": {"url": "http://localhost:8081/mcp", "auth": "iam"},
    }
    assert set(_load(monkeypatch, tmp_path, servers)) == {"plain_none", "local", "local_name"}
    assert "'plain' skipped" in caplog.text and "'plain_jwt' skipped" in caplog.text
    assert "must be https://" in caplog.text


def test_config_parsed_once_and_reparsed_on_change(monkeypatch, tmp_path, caplog):
    import app.tools.mcp as mcp_mod
    from app.config import get_settings

    monkeypatch.setenv("ODD_HOST", "mcp.example.com")
    servers = {"odd": {"url": "https://${ODD_HOST}/mcp", "auth": "iam"}}
    _load(monkeypatch, tmp_path, servers)
    for _ in range(3):  # every request calls load_mcp_config
        assert set(mcp_mod.load_mcp_config(get_settings())) == {"odd"}
    assert caplog.text.count("'odd' uses auth=iam") == 1  # logged once, not per request
    monkeypatch.setenv("ODD_HOST", "other.example.com")  # a referenced ${VAR} changed
    assert mcp_mod.load_mcp_config(get_settings())["odd"]["url"] == "https://other.example.com/mcp"
    cfg = tmp_path / "mcp.json"
    cfg.write_text(json.dumps({"servers": {}}))  # the file changed
    assert mcp_mod.load_mcp_config(get_settings()) == {}


# --- IAMBearerAuth: cache, expiry, single-flight, 401 ⇒ one retry with a fresh token
def _iam_token(sub: str, n: int) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"sub": sub, "n": n}).encode()).decode()
    return f"h.{payload.rstrip('=')}.s"


def _fake_iam(monkeypatch, *, status: int = 200, delay: float = 0.0):
    """IAMBearerAuth whose token endpoint is an httpx.MockTransport; returns (auth, issued tokens)."""
    import asyncio

    from app.tools.mcp import IAMBearerAuth

    monkeypatch.setenv("GREENNODE_CLIENT_ID", "dummy-id")
    monkeypatch.setenv("GREENNODE_CLIENT_SECRET", "dummy-secret")
    issued: list[str] = []

    async def token_endpoint(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(delay)
        if status != 200:
            return httpx.Response(status, json={"error": "invalid_client"})
        issued.append(_iam_token("sa-123", len(issued)))
        return httpx.Response(200, json={"access_token": issued[-1], "expires_in": 1800})

    return IAMBearerAuth(transport=httpx.MockTransport(token_endpoint)), issued


async def test_iam_token_cached_until_expiry_and_single_flight(monkeypatch):
    import asyncio

    auth, issued = _fake_iam(monkeypatch, delay=0.05)
    # 4 `auth: iam` servers loading in parallel used to make 4 token requests
    tokens = await asyncio.gather(*(auth.atoken() for _ in range(4)))
    assert len(issued) == 1 and set(tokens) == {issued[0]}
    assert await auth.atoken() == issued[0] and len(issued) == 1  # cached
    assert auth.principal == "iam:sa-123"
    auth._exp = time.time() - 1  # expired
    assert await auth.atoken() == issued[1] and len(issued) == 2


async def test_gateway_401_retries_once_with_a_fresh_token(monkeypatch):
    auth, issued = _fake_iam(monkeypatch)
    await auth.atoken()
    auth._fetched_at -= 120  # an older token that the gateway now rejects (revoked / expired early)
    seen = []

    def gateway(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["authorization"])
        return httpx.Response(401 if seen[-1] == f"Bearer {issued[0]}" else 200)

    async with httpx.AsyncClient(auth=auth, transport=httpx.MockTransport(gateway)) as client:
        assert (await client.post("https://gw.test/x", json={})).status_code == 200
    assert seen == [f"Bearer {issued[0]}", f"Bearer {issued[1]}"]


async def test_persistent_401_with_a_fresh_token_does_not_loop(monkeypatch):
    auth, issued = _fake_iam(monkeypatch)
    calls = []

    def gateway(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(401)  # e.g. wrong inbound config: a new token would not help

    async with httpx.AsyncClient(auth=auth, transport=httpx.MockTransport(gateway)) as client:
        assert (await client.post("https://gw.test/x", json={})).status_code == 401
        assert (await client.post("https://gw.test/x", json={})).status_code == 401
    assert len(calls) == 2 and len(issued) == 1  # no retry storm, no IAM storm


# --- startup: log the Policy Group principal before any request (main.py lifespan)
async def test_log_iam_principal_at_startup(monkeypatch, tmp_path, caplog):
    import logging

    import app.tools.mcp as mcp_mod

    gw = "https://gw-a-b.agentbase-gateway.aiplatform.vngcloud.vn"
    settings = _use_config(monkeypatch, tmp_path, {"t": {"url": f"{gw}/t", "auth": "iam"}})
    auth, issued = _fake_iam(monkeypatch)
    monkeypatch.setattr(mcp_mod, "_iam_auth", auth)
    with caplog.at_level(logging.INFO, logger="app.tools.mcp"):
        await mcp_mod.log_iam_principal(settings)
    assert len(issued) == 1 and "IAM principal of the agent" in caplog.text
    assert "iam:sa-123" in caplog.text


async def test_log_iam_principal_never_fails_startup(monkeypatch, tmp_path, caplog):
    import app.tools.mcp as mcp_mod

    settings = _use_config(
        monkeypatch, tmp_path, {"t": {"url": "http://localhost:1/t", "auth": "none"}}
    )
    auth, issued = _fake_iam(monkeypatch)
    monkeypatch.setattr(mcp_mod, "_iam_auth", auth)
    await mcp_mod.log_iam_principal(settings)
    assert issued == []  # no auth=iam server ⇒ no token request at all

    settings = _use_config(
        monkeypatch, tmp_path, {"t": {"url": "https://gw.test/t", "auth": "iam"}}
    )
    rejected, _ = _fake_iam(monkeypatch, status=401)
    monkeypatch.setattr(mcp_mod, "_iam_auth", rejected)
    await mcp_mod.log_iam_principal(settings)  # IAM rejects the pair: logged, not raised
    assert "IAM principal not logged at startup" in caplog.text and "check-creds" in caplog.text

    monkeypatch.delenv("GREENNODE_CLIENT_ID")
    monkeypatch.chdir(tmp_path)  # no .greennode.json either
    monkeypatch.setattr(mcp_mod, "_iam_auth", mcp_mod.IAMBearerAuth())
    await mcp_mod.log_iam_principal(settings)
    assert "no IAM credentials are configured" in caplog.text
