"""Self-evaluation loop (evaluator–optimizer) — optional, OFF by default (REFLECTION_ENABLED).

    agent (final answer) -> reflect --pass--> END
                              └--fail & retries left--> agent (with critique) -> reflect ...

- The judge uses the `judge` task (default tier: small — the user is waiting) and returns JSON
  {"pass": bool, "score": 0..1, "critique": "..."}. It sees the question, the tool results of THIS turn (truncated)
  and the answer — criteria like "accurate to the tool data" can't be judged without the data.
- A rejected answer is REMOVED from history (RemoveMessage) so it doesn't pollute short-term memory; the critique
  goes into the per-turn context message (app/graph/builder.py), not into the system prompt.
- Each judgment writes a `self_eval` score to the Langfuse trace => see the online quality distribution.
- Skipped (should_reflect) when its verdict can't change anything or would be wrong: a "simple" turn (adaptive
  routing), a retry answer once the retries are used up, a turn where the user rejected a tool call (HITL).
- Judge error / timeout (REFLECTION_TIMEOUT_S) ⇒ the answer is kept (never blocks the user).
- Costs 1 extra LLM call per turn (+2 per retry: agent + judge): enable only for use cases that need high accuracy.
"""

from __future__ import annotations

import asyncio
import json
import math
import re

from langchain_core.messages import (
    AnyMessage,
    HumanMessage,
    RemoveMessage,
    SystemMessage,
    ToolMessage,
)
from langgraph.graph import END

from app.config import Settings
from app.hitl import is_placeholder_tool_message, is_rejected_tool_message
from app.llm import get_llm
from app.observability import tracing

JUDGE_PROMPT = """You are a judge of the quality of an AI agent's answers.
Criteria: {criteria}
Judge facts against the tool results shown (when there are none, the answer must not claim data it could not have).
Return JSON only: {{"pass": true|false, "score": <0..1>, "critique": "<short feedback for fixing it>"}}"""
DEFAULT_CRITIQUE = "The answer did not meet the criteria. Make it accurate, complete and on point."
# What the judge sees of this turn's tool results — enough to check figures, bounded for latency/cost
TOOL_RESULT_MAX_CHARS = 1500
TOOL_RESULTS_MAX_CHARS = 6000


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


def should_reflect(state: dict, settings: Settings) -> bool:
    """Whether the final answer of this turn goes to the judge (route_after_agent in app/graph/builder.py)."""
    if not settings.reflection_enabled:
        return False
    if settings.reflection_skip_simple and state.get("llm_task") == "agent_simple":
        return False  # simple message (adaptive routing only): not worth 1–2 extra LLM calls (measured: +27s)
    rounds = state.get("reflection_round", 0)
    if rounds and rounds >= settings.reflection_max_retries:
        return False  # retries used up: pass or fail, this answer is final ⇒ the judge can't change anything
    # The user rejected a tool call: the answer explains what was NOT done — "not accurate to the tool data" would
    # be a spurious fail, and a retry would discard the explanation
    return not any(is_rejected_tool_message(m) for m in current_turn(state["messages"]))


def build_reflect_node(settings: Settings):
    async def reflect(state: dict) -> dict:
        messages = state["messages"]
        answer = messages[-1]
        question = next((m for m in reversed(messages) if isinstance(m, HumanMessage)), None)
        tools = tool_results_text(messages)
        tools_part = f"Tool results this turn:\n{tools or 'none'}\n\n"
        try:
            async with asyncio.timeout(settings.reflection_timeout_s):
                # in-turn self-eval flow (default tier: small) — inside the try: a config error keeps the answer
                judge = get_llm("judge")
                result = await judge.ainvoke(
                    [
                        SystemMessage(JUDGE_PROMPT.format(criteria=settings.reflection_criteria)),
                        HumanMessage(
                            f"Question:\n{question.text if question else ''}\n\n{tools_part}"
                            f"Answer:\n{answer.text}"
                        ),
                    ],
                    config={"run_name": "reflection.judge", "tags": ["evaluation"]},
                )
        except TimeoutError:  # the user is waiting: keep the answer
            tracing.event(
                "reflection.judge_timeout",
                level="WARNING",
                metadata={"timeout_s": settings.reflection_timeout_s},
            )
            return {"critique": "", "reflection_round": 0}
        except Exception as e:  # noqa: BLE001 — a judge failure must not break the existing answer
            tracing.event(
                "reflection.judge_failed",
                level="WARNING",
                metadata={"error": f"{type(e).__name__}: {str(e)[:200]}"},
            )
            return {"critique": "", "reflection_round": 0}
        verdict = _parse(result.text)
        rounds = state.get("reflection_round", 0)
        tracing.score_trace("self_eval", verdict["score"], comment=verdict["critique"])
        if verdict["pass"] or rounds >= settings.reflection_max_retries:
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
