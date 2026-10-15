"""Self-evaluation (evaluator–optimizer) — optional, OFF by default (REFLECTION_ENABLED).

The judge grades the turn's final answer; HOW it runs is decided per turn by `reflection_plan` (REFLECTION_MODE):

    gate        agent (final answer) -> reflect --pass--> END
                                          └--fail & retries left--> agent (with critique) -> reflect ...
                The user waits for the verdict (SSE `status` checking); a fail replaces the answer (SSE `reset`).
    background  agent (final answer) -> END: `done` / the JSON response goes out FIRST, then the judge only SCORES
                the answer (self_eval on the same trace) — no retry, nobody waits (judge_after_response).
    auto        (default) gate, except where a retry is not worth its wait ⇒ background:
                - the turn ran on the reasoning tier: a retry is another 20–90 s reasoning call (measured: an 85 s
                  reasoning turn, then the judge hit REFLECTION_TIMEOUT_S=20 s — the user waited 20 s for nothing);
                - the answer is longer than REFLECTION_GATE_MAX_CHARS: streamed, it has been read already, and
                  regenerating it costs as long again;
                - REFLECTION_MAX_RETRIES=0: a gate could only delay the answer.

- The judge uses the `judge` task (default tier: small) and returns JSON {"pass": bool, "score": 0..1, "critique"}.
  It sees the question, the tool results of THIS turn (truncated) and the answer — criteria like "accurate to the
  tool data" can't be judged without the data.
- A rejected answer (gate) is REMOVED from history (RemoveMessage) so it doesn't pollute short-term memory; the
  critique goes into the per-turn context message (app/graph/builder.py), not into the system prompt.
- Each judgment writes a `self_eval` score to the Langfuse trace => see the online quality distribution.
- Not judged at all (reflection_plan → None) when its verdict can't change anything or would be wrong: a "simple"
  turn (adaptive routing), a retry answer once the retries are used up, a turn where the user rejected a tool call.
- Judge error / timeout ⇒ the answer is kept (never blocks the user); WARNING event on the trace.
- Background judges: started by app/service.py inside the request's trace context (asyncio.create_task copies it ⇒
  the judge lands in the SAME trace, after the root span ended); independent of the HTTP connection (the client
  closes the stream at `done`); bounded by REFLECTION_BACKGROUND_TIMEOUT_S per call and
  REFLECTION_BACKGROUND_MAX_CONCURRENCY at once (more ⇒ not scored, event reflection.background_skipped); never
  raise; drained at shutdown (main.py lifespan → drain_background, before the Langfuse flush).
- Costs 1 extra LLM call per turn (+2 per gate retry): enable only for use cases that need high accuracy.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
from typing import Any, Literal

from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    HumanMessage,
    RemoveMessage,
    SystemMessage,
    ToolMessage,
)
from langgraph.graph import END

from app.config import Settings
from app.hitl import is_placeholder_tool_message, is_rejected_tool_message
from app.llm import get_llm, tier_for
from app.observability import tracing

log = logging.getLogger(__name__)

JUDGE_PROMPT = """You are a judge of the quality of an AI agent's answers.
Criteria: {criteria}
Judge facts against the tool results shown (when there are none, the answer must not claim data it could not have).
Return JSON only: {{"pass": true|false, "score": <0..1>, "critique": "<short feedback for fixing it>"}}"""
DEFAULT_CRITIQUE = "The answer did not meet the criteria. Make it accurate, complete and on point."
# What the judge sees of this turn's tool results — enough to check figures, bounded for latency/cost
TOOL_RESULT_MAX_CHARS = 1500
TOOL_RESULTS_MAX_CHARS = 6000
# Shutdown: pending background judges get this long to finish (their score is flushed), then they are cancelled.
# Keep it well under the platform's termination grace period.
DRAIN_TIMEOUT_S = 10.0

Mode = Literal["gate", "background"]


def _as_bool(v, default: bool) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ("true", "yes", "1", "pass", "đạt")
    if isinstance(v, int | float):
        return bool(v)
    return default


def _as_score(v, default: float) -> float:
    try:
        score = float(v)
    except (TypeError, ValueError):
        return default
    # json.loads accepts NaN/Infinity: NaN survives min/max clamping ⇒ treat a non-finite score as missing
    return min(max(score, 0.0), 1.0) if math.isfinite(score) else default


def _parse(text: str) -> dict:
    """Judge returns malformed JSON (null, "false" as a string, missing fields) ⇒ don't crash, default to PASS."""
    match = re.search(r"\{.*\}", text, re.S)
    try:
        data = json.loads(match.group(0)) if match else {}
    except json.JSONDecodeError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    return {
        "pass": _as_bool(data.get("pass"), True),
        "score": _as_score(data.get("score"), 1.0),
        "critique": str(data.get("critique") or ""),
    }


def current_turn(messages: list[AnyMessage]) -> list[AnyMessage]:
    """Messages after the latest user message (this turn's tool calls, tool results and answer)."""
    last_h = max((i for i, m in enumerate(messages) if isinstance(m, HumanMessage)), default=-1)
    return messages[last_h + 1 :]


def _clip(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"…[{len(text) - max_chars} chars cut]"


def tool_results_text(messages: list[AnyMessage]) -> str:
    """This turn's tool results for the judge (bridge placeholders skipped), each and all truncated."""
    lines, total = [], 0
    for m in current_turn(messages):
        if not isinstance(m, ToolMessage) or is_placeholder_tool_message(m):
            continue
        content = m.content
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False)
        line = f"- {m.name or 'tool'} ({m.status}): {_clip(content, TOOL_RESULT_MAX_CHARS)}"
        if total + len(line) > TOOL_RESULTS_MAX_CHARS:
            lines.append("- …[more tool results cut]")
            break
        lines.append(line)
        total += len(line)
    return "\n".join(lines)


def reflection_plan(state: dict, settings: Settings) -> tuple[Mode, str] | None:
    """How this turn's final answer is judged: (mode, reason) or None (not judged). The graph gates on "gate"
    (route_after_agent → should_reflect); app/service.py starts the judge after the response on "background"."""
    if not settings.reflection_enabled:
        return None
    task = state.get("llm_task") or "agent"
    if settings.reflection_skip_simple and task == "agent_simple":
        return None  # simple message (adaptive routing only): not worth 1–2 extra LLM calls (measured: +27s)
    rounds = state.get("reflection_round", 0)
    if rounds and rounds >= settings.reflection_max_retries:
        return None  # retries used up: pass or fail, this answer is final ⇒ the judge can't change anything
    # The user rejected a tool call: the answer explains what was NOT done — "not accurate to the tool data" would
    # be a spurious fail, and a retry would discard the explanation
    if any(is_rejected_tool_message(m) for m in current_turn(state["messages"])):
        return None
    if settings.reflection_mode != "auto":
        return settings.reflection_mode, "configured"
    if settings.reflection_max_retries == 0:
        return "background", "no_retries"
    if tier_for(task) == "reasoning":
        return "background", "reasoning_tier"
    if len(state["messages"][-1].text) > settings.reflection_gate_max_chars:
        return "background", "long_answer"
    return "gate", "short_answer"


def should_reflect(state: dict, settings: Settings) -> bool:
    """Whether the final answer goes to the `reflect` node — the judge the user waits for (route_after_agent)."""
    plan = reflection_plan(state, settings)
    return plan is not None and plan[0] == "gate"


async def judge_answer(
    messages: list[AnyMessage],
    settings: Settings,
    *,
    timeout_s: float,
    config: dict[str, Any] | None = None,
) -> dict | None:
    """Grade the last message (the answer) ⇒ verdict {pass, score, critique} + `self_eval` score on the current
    trace, or None when the judge timed out / failed (WARNING event; the caller keeps the answer). Never raises."""
    answer = messages[-1]
    question = next((m for m in reversed(messages) if isinstance(m, HumanMessage)), None)
    tools = tool_results_text(messages)
    tools_part = f"Tool results this turn:\n{tools or 'none'}\n\n"
    cfg: dict[str, Any] = {**(config or {}), "run_name": "reflection.judge"}
    cfg["tags"] = ["evaluation", *(cfg.get("tags") or [])]
    try:
        async with asyncio.timeout(timeout_s):
            # self-eval flow (default tier: small) — inside the try: a config error keeps the answer
            judge = get_llm("judge")
            result = await judge.ainvoke(
                [
                    SystemMessage(JUDGE_PROMPT.format(criteria=settings.reflection_criteria)),
                    HumanMessage(
                        f"Question:\n{question.text if question else ''}\n\n{tools_part}"
                        f"Answer:\n{answer.text}"
                    ),
                ],
                config=cfg,
            )
    except TimeoutError:  # keep the answer
        tracing.event(
            "reflection.judge_timeout", level="WARNING", metadata={"timeout_s": timeout_s}
        )
        return None
    except Exception as e:  # noqa: BLE001 — a judge failure must not break the existing answer
        tracing.event(
            "reflection.judge_failed",
            level="WARNING",
            metadata={"error": f"{type(e).__name__}: {str(e)[:200]}"},
        )
        return None
    verdict = _parse(result.text)
    tracing.score_trace("self_eval", verdict["score"], comment=verdict["critique"])
    return verdict


def build_reflect_node(settings: Settings):
    """The gate: the user waits for this verdict; a fail with retries left removes the answer and retries."""

    async def reflect(state: dict) -> dict:
        messages = state["messages"]
        answer = messages[-1]
        verdict = await judge_answer(messages, settings, timeout_s=settings.reflection_timeout_s)
        rounds = state.get("reflection_round", 0)
        if verdict is None or verdict["pass"] or rounds >= settings.reflection_max_retries:
            return {"critique": "", "reflection_round": 0}
        # The answer is removed below, so a retry MUST follow: routing keys on a non-empty critique, and a
        # judge that says "fail" without one would otherwise end the turn with an empty reply.
        critique = verdict["critique"] or DEFAULT_CRITIQUE
        return {
            "messages": [RemoveMessage(id=answer.id)],
            "critique": critique,
            "reflection_round": rounds + 1,
        }

    return reflect


def route_after_reflect(state: dict) -> str:
    return "agent" if state.get("critique") else END


# --------------------------------------------------------------------------- background (after the response)
# Strong references: the event loop keeps only weak ones (an unreferenced task may be garbage-collected mid-run)
_background: set[asyncio.Task] = set()


def judge_after_response(state: dict, settings: Settings) -> bool:
    """Start the background judge for this turn when reflection_plan says "background". Call it from the request,
    INSIDE its trace context (app/service.py `_Run._result`): the task inherits that context ⇒ its observations and
    the self_eval score land in the same trace. Returns whether a judge was started. Never raises."""
    try:
        messages = state.get("messages") or []
        answer = messages[-1] if messages else None
        if not isinstance(answer, AIMessage) or answer.tool_calls or not answer.text:
            return False  # nothing to grade (interrupted / empty turn)
        plan = reflection_plan(state, settings)
        if plan is None or plan[0] != "background":
            return False
        running = len(_background)
        if running >= settings.reflection_background_max_concurrency:
            tracing.event(
                "reflection.background_skipped",
                level="WARNING",
                metadata={
                    "running": running,
                    "max_concurrency": settings.reflection_background_max_concurrency,
                },
            )
            return False
        # this turn's snapshot (a list copy — the next turn builds a new state)
        task = asyncio.create_task(_judge_later(list(messages), settings, plan[1]))
        _background.add(task)
        task.add_done_callback(_background.discard)
        return True
    except Exception:  # noqa: BLE001 — scoring must never break a response that is ready
        log.warning("could not start the background judge", exc_info=True)
        return False


async def _judge_later(messages: list[AnyMessage], settings: Settings, reason: str) -> None:
    # Outside the graph: no inherited RunnableConfig ⇒ pass the Langfuse handler explicitly (the gate's judge gets
    # it from the reflect node's config)
    config = {
        "callbacks": tracing.langchain_callbacks(),
        "metadata": {"reflection_mode": "background", "reflection_reason": reason},
    }
    try:
        await judge_answer(
            messages, settings, timeout_s=settings.reflection_background_timeout_s, config=config
        )
    except Exception:  # noqa: BLE001 — judge_answer records its own failures; nothing may escape the task
        log.warning("background judge failed", exc_info=True)


def background_pending() -> int:
    return sum(not t.done() for t in _background)


async def drain_background(timeout_s: float = DRAIN_TIMEOUT_S, *, cancel: bool = True) -> int:
    """Shutdown (main.py lifespan, BEFORE tracing.shutdown_tracing flushes): wait up to `timeout_s` for running
    background judges, then cancel the rest (cancel=False: leave them running — eval items share one loop).
    Returns how many were still running (cancelled ones lose their score)."""
    pending = {t for t in _background if not t.done()}
    if not pending:
        return 0
    _, pending = await asyncio.wait(pending, timeout=timeout_s)
    if not cancel:
        return len(pending)
    for t in pending:
        t.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
        log.warning("shutdown: cancelled %d background judge(s) still running", len(pending))
    return len(pending)
