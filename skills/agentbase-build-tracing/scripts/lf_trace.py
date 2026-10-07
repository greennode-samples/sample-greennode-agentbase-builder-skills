"""Print a Langfuse v4 trace tree with latency / model / usage / cost / TTFT / prompt / errors — for quick debugging.

Langfuse v4 (events_only) NO LONGER has the old /api/public/traces, /sessions, /scores ⇒ use
/api/public/v2/observations (filtered by traceId) and /api/public/v3/scores.

  uv run python <skill-dir>/scripts/lf_trace.py <trace_id>            # 1 trace
  uv run python <skill-dir>/scripts/lf_trace.py --latest 5            # 5 latest traces (root agent.invoke)
  uv run python <skill-dir>/scripts/lf_trace.py --session <session>   # traces of 1 session

Env: LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY, LANGFUSE_BASE_URL (or LANGFUSE_HOST). Run in
src/backend to use the project venv (needs httpx). Reads .env if present.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime

import httpx

FIELDS = "core,basic,time,io,metadata,model,usage,prompt,metrics"


def _load_env() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv()
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


def print_tree(c: httpx.Client, trace_id: str) -> None:
    obs = sorted(observations(c, traceId=trace_id), key=lambda o: o["startTime"])
    if not obs:
        print(f"(no observations for trace {trace_id} — ingestion may lag a few seconds)")
        return
    by = {o["id"]: o for o in obs}

    def depth(o: dict) -> int:
        d = 0
        while o.get("parentObservationId") in by:
            o, d = by[o["parentObservationId"]], d + 1
        return d

    t0 = _ts(obs[0]["startTime"])
    root = next((o for o in obs if o.get("isRootObservation")), obs[0])
    gens = [o for o in obs if o["type"] == "GENERATION"]
    cost = sum(o.get("totalCost") or 0 for o in gens)
    usage = lambda k: sum((o.get("usageDetails") or {}).get(k, 0) for o in gens)  # noqa: E731
    print(f"\n=== trace {trace_id} | {root['name']} user={root.get('userId')} session={root.get('sessionId')} "
          f"env={root.get('environment')} version={root.get('version')}")
    print(f"    generations={len(gens)} cost={cost:.6f} in={usage('input')} out={usage('output')} "
          f"reasoning={usage('output_reasoning')} cache_read={usage('input_cache_read')}")
    for o in obs:
        st, en = _ts(o["startTime"]), _ts(o.get("endTime"))
        dur = f"{(en - st).total_seconds():.2f}s" if en else "OPEN"
        lvl = "" if o.get("level") in (None, "DEFAULT") else f" [{o['level']}]"
        line = f"{'  ' * depth(o)}{o['name']} <{o['type']}> +{(st - t0).total_seconds():.1f}s {dur}{lvl}"
        if o["type"] == "GENERATION":
            u = o.get("usageDetails") or {}
            line += (f" | {o.get('model')} in={u.get('input')} out={u.get('output')} "
                     f"reason={u.get('output_reasoning')} cache={u.get('input_cache_read')} "
                     f"ttft={o.get('timeToFirstToken')} cost={o.get('totalCost')}")
            if o.get("promptName"):
                line += f" prompt={o['promptName']}:{o.get('promptVersion')}"
        if lvl and o.get("statusMessage"):
            line += f"\n{'  ' * depth(o)}    ↳ {o['statusMessage'][:200]}"
        print(line)
    scores = c.get("/api/public/v3/scores", params={"traceId": trace_id, "limit": 50})
    if scores.status_code == 200 and scores.json().get("data"):
        print("    scores:", [(s.get("name"), s.get("value")) for s in scores.json()["data"]])


def main() -> None:
    _load_env()
    ap = argparse.ArgumentParser()
    ap.add_argument("trace_id", nargs="?")
    ap.add_argument("--latest", type=int)
    ap.add_argument("--session")
    a = ap.parse_args()
    c = client()
    if a.trace_id:
        print_tree(c, a.trace_id)
        return
    roots = observations(c, name="agent.invoke", limit=100)
    if a.session:
        roots = [o for o in roots if o.get("sessionId") == a.session]
    for o in sorted(roots, key=lambda o: o["startTime"], reverse=True)[: a.latest or 3]:
        print_tree(c, o["traceId"])


if __name__ == "__main__":
    main()
