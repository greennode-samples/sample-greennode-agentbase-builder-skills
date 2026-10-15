"""Adaptive routing (optional, LLM_ADAPTIVE_ROUTING): pick the agent's tier by question complexity.

    simple   → task agent_simple  (small)      : greetings, single-step, 1-tool lookup, definitions
    standard → task agent         (large)      : normal conversation, a few tool calls
    complex  → task agent_complex (reasoning)  : multi-step, comparison/analysis, calculation, planning

The router uses the small tier (1 short call, ~100 tokens). Error/unparseable ⇒ "standard" (safe).
It sees the request (truncated) + the last exchanges (truncated) + the rolling summary: a follow-up such as "and the
second one?" is only simple or complex in the light of what came before.
Enable only for mixed traffic: mostly simple questions (savings) + some needing reasoning (quality).
"""

from __future__ import annotations

import json
import logging
import re
from typing import Literal

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage

from app.llm import get_llm, model_for, tier_for
from app.observability import tracing

log = logging.getLogger(__name__)
Complexity = Literal["simple", "standard", "complex"]
TASK_BY_COMPLEXITY: dict[str, str] = {
    "simple": "agent_simple",
    "standard": "agent",
    "complex": "agent_complex",
}

ROUTER_PROMPT = """Classify the complexity of the request to pick a suitable model. Return only JSON
{"complexity": "simple"|"standard"|"complex", "reason": "<short>"}.
- simple: greetings, thanks, single-step/single-fact questions, simple lookups.
- standard: normal conversation, needs a few steps or a few tools.
- complex: multi-step reasoning, analysis/comparison across sources, calculation, planning, writing/reviewing code,
  questions with complex constraints."""


# The router only needs the gist: a pasted document must not make it slow or expensive
REQUEST_MAX_CHARS = 2000
CONTEXT_MAX_CHARS = 2000
CONTEXT_MESSAGES = 6  # last 3 exchanges
MESSAGE_MAX_CHARS = 400


def _clip(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    head = max_chars * 3 // 4
    return f"{text[:head]} […] {text[-(max_chars - head) :]}"


def recent_exchanges(messages: list[AnyMessage]) -> str:
    """The last user/assistant messages BEFORE the current request (tool calls/results skipped), each clipped,
    at most CONTEXT_MAX_CHARS in total (the most recent kept)."""
    last_h = max((i for i, m in enumerate(messages) if isinstance(m, HumanMessage)), default=0)
    lines: list[str] = []
    for m in messages[:last_h]:
        if isinstance(m, HumanMessage) and m.text.strip():
            lines.append(f"User: {_clip(m.text.strip(), MESSAGE_MAX_CHARS)}")
        elif isinstance(m, AIMessage) and not m.tool_calls and m.text.strip():
            lines.append(f"Assistant: {_clip(m.text.strip(), MESSAGE_MAX_CHARS)}")
    out: list[str] = []
    for line in reversed(lines[-CONTEXT_MESSAGES:]):
        if sum(len(x) + 1 for x in out) + len(line) > CONTEXT_MAX_CHARS:
            break
        out.append(line)
    return "\n".join(reversed(out))


async def classify(text: str, summary: str = "", recent: str = "") -> Complexity:
    """`recent`: recent_exchanges(messages) — the conversation right before the request."""
    context = f"Conversation summary: {_clip(summary, CONTEXT_MAX_CHARS)}\n\n" if summary else ""
    if recent:
        context += f"Recent messages:\n{recent}\n\n"
    with tracing.step("llm.route", input={"text": text[:500]}) as st:
        try:
            out = await get_llm("router").ainvoke(
                [
                    SystemMessage(ROUTER_PROMPT),
                    HumanMessage(f"{context}Request: {_clip(text, REQUEST_MAX_CHARS)}"),
                ],
                config={"run_name": "router.call"},
            )
            # .text: content may be a list of blocks (str() of it is not JSON)
            match = re.search(r"\{.*\}", out.text, re.S)
            data = json.loads(match.group(0)) if match else {}
            level = data.get("complexity")
            level = level if level in TASK_BY_COMPLEXITY else "standard"
        except Exception as e:  # noqa: BLE001 — a router error must not break the chat turn
            st.set(level="WARNING", status_message=f"{type(e).__name__}: {e}")
            return "standard"
        st.set(
            output={
                "complexity": level,
                "task": TASK_BY_COMPLEXITY[level],
                "reason": data.get("reason"),
            }
        )
        task = TASK_BY_COMPLEXITY[level]
        log.info("llm.route complexity=%s task=%s model=%s", level, task, model_for(tier_for(task)))
        return level  # type: ignore[return-value]
