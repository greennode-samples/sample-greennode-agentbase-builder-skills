"""Print a Langfuse v4 trace tree with latency / model / usage / cost / TTFT / prompt / errors — for quick debugging.

Langfuse v4 (events_only) NO LONGER has the old /api/public/traces, /sessions, /scores ⇒ use
/api/public/v2/observations (filtered by traceId) and /api/public/v3/scores.

  uv run python <skill-dir>/scripts/lf_trace.py <trace_id>            # 1 trace
  uv run python <skill-dir>/scripts/lf_trace.py --latest 5            # 5 latest traces (root agent.invoke)
  uv run python <skill-dir>/scripts/lf_trace.py --session <session>   # traces of 1 session
  uv run python <skill-dir>/scripts/lf_trace.py --latest 3 --audit    # + best-practice audit (exit 1 on FAIL)

--audit checks each trace against the Langfuse best practices as applied by agentbase-build-tracing (root I/O,
user/session/env, one agent per agent, model/usage/cost/thinking on generations, stable names, leaked secrets).
It complements — does not replace — reading https://langfuse.com/docs/observability/best-practices fresh.

Env: LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY, LANGFUSE_BASE_URL (or LANGFUSE_HOST). Run in
src/backend to use the project venv (needs httpx). Reads .env if present.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime

import httpx

FIELDS = "core,basic,time,io,metadata,model,usage,prompt,metrics"
# Only these may be `agent` observations: the request root, remote A2A agents, in-graph sub-agents
AGENT_NAMES = ("agent.invoke", "a2a.", "subagent.")
_DYNAMIC_NAME_RE = re.compile(r"\d{4,}|[0-9a-f]{8,}|-retry|_retry", re.IGNORECASE)
_SECRET_RES = (  # (what, pattern) — the matched value itself is never printed
    ("Bearer token", re.compile(r"Bearer\s+(?!\*\*\*)[A-Za-z0-9._~+/-]{8,}")),
    ("JWT", re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.")),
    ("API key", re.compile(r"\b(?:sk|vn|rk)-[A-Za-z0-9_-]{20,}")),
    (
        "password/key/token value",
        re.compile(
            r"(?i)(?:password|passwd|api_?key|access_token|secret)[\"']?\s*[:=]\s*[\"']?(?!\*\*\*)[^\s\"',&]{4,}"
        ),
    ),
)


def _load_env() -> None:
    try:
        from dotenv import find_dotenv, load_dotenv

        # usecwd: the project's .env (run from src/backend), not one next to this script in the skill directory
        load_dotenv(find_dotenv(usecwd=True))
    except ImportError:
        pass


def _ts(x: str | None) -> datetime | None:
    return datetime.fromisoformat(x.replace("Z", "+00:00")) if x else None


def client() -> httpx.Client:
    base = os.getenv("LANGFUSE_BASE_URL") or os.getenv("LANGFUSE_HOST") or "https://cloud.langfuse.com"
    pk, sk = os.getenv("LANGFUSE_PUBLIC_KEY"), os.getenv("LANGFUSE_SECRET_KEY")
    if not (pk and sk):
        sys.exit("Missing LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY")
    return httpx.Client(base_url=base, auth=(pk, sk), timeout=30)


def observations(c: httpx.Client, **params) -> list[dict]:
    r = c.get("/api/public/v2/observations", params={"limit": 300, "fields": FIELDS, **params})
    r.raise_for_status()
    return r.json()["data"]


def _text(x: object) -> str:
    return x if isinstance(x, str) else json.dumps(x, ensure_ascii=False, default=str)


def _empty(x: object) -> bool:
    return x is None or x == "" or x == {} or x == []


def audit(obs: list[dict]) -> list[tuple[str, str]]:
    """[(FAIL|WARN, message)] for one trace's observations (v2 observations API shape)."""
    out: list[tuple[str, str]] = []
    root = next((o for o in obs if o.get("isRootObservation")), obs[0])
    if _empty(root.get("input")):
        out.append(("FAIL", f"root {root['name']}: no input — set it to the user message"))
    if _empty(root.get("output")):
        out.append(("WARN", f"root {root['name']}: no output — set it to the answer (or the interrupt)"))
    elif isinstance(root.get("output"), dict) and {"response", "interrupt"} & set(root["output"]):
        out.append(("WARN", "root output is a JSON blob — the session's conversation line must be text "
                            "(answer / describe_interrupt), details in metadata"))
    if isinstance(root.get("input"), dict) and "resume" in root["input"]:
        out.append(("WARN", "root input is a JSON blob — use describe_decisions for resume turns"))
    for key in ("userId", "sessionId"):
        if not root.get(key):
            out.append(("WARN", f"root has no {key} — propagate_attributes(...) in trace_request"))
    if root.get("environment") in (None, "", "default"):
        out.append(("WARN", "environment not set — Langfuse(environment=APP_ENV)"))
    for o in obs:
        name, kind = o.get("name") or "", o.get("type")
        if kind == "AGENT" and not name.startswith(AGENT_NAMES):
            out.append(("WARN", f"{name}: typed agent — only {AGENT_NAMES} (see _CHAIN_TYPES in tracing.py)"))
        if _DYNAMIC_NAME_RE.search(name):
            out.append(("WARN", f"{name}: name looks per-request — move ids/counters to metadata"))
        if kind == "GENERATION":
            u, model = o.get("usageDetails") or {}, o.get("model")
            if not model:
                out.append(("FAIL", f"{name}: generation without model — no cost, no model filter"))
            elif "/" in name or name == model:
                out.append(("WARN", f"{name}: generation named after the model — use llm.<task>.generate"))
            failed = o.get("level") == "ERROR"  # e.g. primary model 403/503 before a fallback: no usage, no cost
            if not failed and not (u.get("input") and u.get("output")):
                out.append(("WARN", f"{name}: no input/output usage (LLM_STREAM_USAGE=true when streaming)"))
            if model and not failed and not o.get("totalCost"):
                out.append(("WARN", f"{name}: no cost — declare the price of {model} in Langfuse (Models)"))
            if u.get("output_reasoning") and '"thinking"' not in _text(o.get("output")):
                out.append(("WARN", f"{name}: {u['output_reasoning']} reasoning tokens but no thinking text"))
            inp = o.get("input")
            inp = json.loads(inp) if isinstance(inp, str) and inp[:1] in "[{" else inp
            msgs = inp.get("messages") if isinstance(inp, dict) else inp
            if isinstance(msgs, list) and any(
                isinstance(m, dict) and m.get("role") == "tool" and not m.get("tool_call_id") for m in msgs
            ):
                out.append(("WARN", f"{name}: tool definitions recorded as messages — the session view replays "
                                    "the history every turn; use tracing.langchain_callbacks()"))
        elif (
            kind not in ("EVENT", "AGENT", "CHAIN")  # chains carry no I/O by design (LangGraph state)
            and o.get("level") != "DEBUG"  # LangGraph internals the CallbackHandler marks as hidden
            and _empty(o.get("input"))
            and _empty(o.get("output"))
        ):
            out.append(("WARN", f"{name}: neither input nor output — add them or drop the observation"))
        blob = " ".join(_text(o.get(k)) for k in ("input", "output", "metadata", "statusMessage"))
        for what, rx in _SECRET_RES:
            if rx.search(blob):
                out.append(("FAIL", f"{name}: unmasked {what} exported — extend _mask_text in tracing.py"))
                break
    return list(dict.fromkeys(out))  # same finding on a repeated step (agent loop) ⇒ once


# Langfuse table filter encoding (`column;type;key;operator;value`, URL-encoded): only the root observation of each
# trace ⇒ the session page reads as the conversation (one user → assistant line per turn)
CONVERSATION_FILTER = "isRootObservation%3Bboolean%3B%3B%3D%3Btrue"


def session_links(c: httpx.Client, session_id: str) -> None:
    r = c.get("/api/public/projects")
    if r.status_code != 200 or not r.json().get("data"):
        return
    base = f"{c.base_url}".rstrip("/") + f"/project/{r.json()['data'][0]['id']}/sessions/{session_id}"
    print(f"session {session_id}\n  conversation: {base}?filter={CONVERSATION_FILTER}\n  every step:   {base}")


def print_tree(c: httpx.Client, trace_id: str, *, do_audit: bool = False) -> int:
    """Prints the tree (+ audit); returns the number of FAIL findings."""
    obs = sorted(observations(c, traceId=trace_id), key=lambda o: o["startTime"])
    if not obs:
        print(f"(no observations for trace {trace_id} — ingestion may lag a few seconds)")
        return 0
    by = {o["id"]: o for o in obs}
    children: dict[str | None, list[dict]] = {}
    for o in obs:  # obs is sorted by start time ⇒ siblings stay in start order
        parent = o.get("parentObservationId")
        children.setdefault(parent if parent in by else None, []).append(o)

    def walk(parent: str | None, depth: int):  # depth-first: every observation right under its parent
        for o in children.get(parent, []):
            yield o, depth
            yield from walk(o["id"], depth + 1)

    t0 = _ts(obs[0]["startTime"])
    root = next((o for o in obs if o.get("isRootObservation")), obs[0])
    gens = [o for o in obs if o["type"] == "GENERATION"]
    cost = sum(o.get("totalCost") or 0 for o in gens)
    usage = lambda k: sum((o.get("usageDetails") or {}).get(k, 0) for o in gens)  # noqa: E731
    print(f"\n=== trace {trace_id} | {root['name']} user={root.get('userId')} session={root.get('sessionId')} "
          f"env={root.get('environment')} version={root.get('version')}")
    print(f"    generations={len(gens)} cost={cost:.6f} in={usage('input')} out={usage('output')} "
          f"reasoning={usage('output_reasoning')} cache_read={usage('input_cache_read')}")
    for o, d in walk(None, 0):
        st, en = _ts(o["startTime"]), _ts(o.get("endTime"))
        dur = f"{(en - st).total_seconds():.2f}s" if en else "OPEN"
        lvl = "" if o.get("level") in (None, "DEFAULT") else f" [{o['level']}]"
        line = f"{'  ' * d}{o['name']} <{o['type']}> +{(st - t0).total_seconds():.1f}s {dur}{lvl}"
        if o["type"] == "GENERATION":
            u = o.get("usageDetails") or {}
            role = (o.get("metadata") or {}).get("llm_role") if isinstance(o.get("metadata"), dict) else None
            line += (f" | {o.get('model')}{f' ({role})' if role else ''} in={u.get('input')} out={u.get('output')} "
                     f"reason={u.get('output_reasoning')} cache={u.get('input_cache_read')} "
                     f"ttft={o.get('timeToFirstToken')} cost={o.get('totalCost')}")
            if o.get("promptName"):
                line += f" prompt={o['promptName']}:{o.get('promptVersion')}"
        if lvl and o.get("statusMessage"):
            line += f"\n{'  ' * d}    ↳ {o['statusMessage'][:200]}"
        print(line)
    # fields=core,details: without `details` the v3 scores API returns no comment (self_eval critique, feedback text)
    scores = c.get("/api/public/v3/scores", params={"traceId": trace_id, "limit": 50, "fields": "core,details"})
    if scores.status_code == 200 and scores.json().get("data"):
        for s in scores.json()["data"]:
            comment = f" — {s['comment'][:120]}" if s.get("comment") else ""
            print(f"    score {s.get('name')}={s.get('value')}{comment}")
    if not do_audit:
        return 0
    findings = audit(obs)
    for level, msg in findings:
        print(f"    {level}: {msg}")
    print(f"    audit: {'clean' if not findings else f'{len(findings)} finding(s)'}")
    return sum(level == "FAIL" for level, _ in findings)


def main() -> None:
    _load_env()
    ap = argparse.ArgumentParser()
    ap.add_argument("trace_id", nargs="?")
    ap.add_argument("--latest", type=int)
    ap.add_argument("--session")
    ap.add_argument("--audit", action="store_true", help="check against the best practices")
    a = ap.parse_args()
    c = client()
    if a.trace_id:
        sys.exit(1 if print_tree(c, a.trace_id, do_audit=a.audit) else 0)
    roots = observations(c, name="agent.invoke", limit=100)
    if a.session:
        roots = [o for o in roots if o.get("sessionId") == a.session]
        session_links(c, a.session)
    fails = 0
    for o in sorted(roots, key=lambda o: o["startTime"], reverse=True)[: a.latest or 3]:
        fails += print_tree(c, o["traceId"], do_audit=a.audit)
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
