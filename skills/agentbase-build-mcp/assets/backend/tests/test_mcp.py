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


def _tool(behavior):
    async def coro(query: str):
        return behavior(query)

    return StructuredTool.from_function(
        coroutine=coro,
        name="tavily_search",
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


async def test_denied_connector_short_circuits_other_tools():
    from app.tools.mcp import reset_policy_denials

    calls = []

    def deny(q):
        calls.append(q)
        raise ToolException("Request denied by policy.")

    reset_policy_denials()
    t1, t2 = _guard_policy(_tool(deny), "stock"), _guard_policy(_tool(deny), "stock")
    assert (await t1.ainvoke({"query": "a"})).startswith("POLICY_DENIED")
    assert (await t2.ainvoke({"query": "b"})).startswith("POLICY_DENIED")
    assert calls == ["a"]  # 2nd tool no longer calls the gateway
    reset_policy_denials()  # new request ⇒ retry normally
    await t2.ainvoke({"query": "c"})
    assert calls == ["a", "c"]


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
@contextlib.contextmanager
def _fake_gateway(*, list_status: int = 200, call_status: int = 200):
    methods: list[str] = []

    async def endpoint(request: Request) -> Response:
        if request.method != "POST":
            return Response(status_code=405)
        msg = await request.json()
        method = msg.get("method", "")
        methods.append(method)
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
                "tools": [
                    {"name": n, "description": n, "inputSchema": schema}
                    for n in ("tavily_search", "tavily_extract")
                ]
            }
        elif method == "tools/call":
            if call_status != 200:
                body = {"message": "No policy allows this request"}
                return JSONResponse(body, status_code=call_status)
            result = {"content": [{"type": "text", "text": "ok"}], "isError": False}
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
    assert first.startswith("POLICY_DENIED") and second.startswith("POLICY_DENIED")
    assert methods.count("tools/call") == 1  # the 2nd tool never reached the gateway
    denied = [kw["metadata"]["short_circuit"] for n, kw in events if n == "mcp.policy_denied"]
    assert denied == [False, True]


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
