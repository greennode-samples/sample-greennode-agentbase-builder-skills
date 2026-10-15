"""Long-term memory = per-user facts/preferences, semantic search, shared across all sessions.

Two mechanisms, used together:
1. Auto-recall (node `recall` in the graph): every turn, search by the latest question (+ the previous one when the
   latest is short, e.g. "and for him?") with a minimum score (LTM_MIN_SCORE), and put the results into the
   per-turn context message, framed as DATA (notes, not instructions) — a saved fact is user-controlled text that
   comes back every session, so it must never carry system authority. Doesn't depend on the LLM calling a tool.
2. Tools `remember` / `recall_memory`: let the LLM save/look up proactively. `remember` is guarded: one short fact
   (LTM_FACT_MAX_CHARS), exact duplicates skipped, a description that forbids greetings/small talk/inferences/secrets.

InMemoryLTM (MEMORY_BACKEND=inmemory) is for tests/local dev only: keyword matching, per process, lost on restart —
recall quality (paraphrases, other languages) can only be judged on AgentBase Memory.

Security rule: actor_id & strategy_id are NEVER tool parameters — they come from
RunnableConfig (set by the handler from the authenticated user) so the LLM can't read another user's memory.

Standard namespace: /strategies/{MEMORY_STRATEGY_ID}/actors/{user_id}
(must match the namespaceTemplate used when creating the memory with /agentbase-memory).
ONE strategy per agent: recall searches only that strategy's namespace (records of a second strategy on the same
memory store are never recalled unless you also search its namespace).

Memory API calls retry 429/5xx with exponential backoff (MEMORY_MAX_RETRIES, MEMORY_RETRY_BACKOFF_S) like the
bridge does for checkpoints — the platform's 10-concurrent-requests limit answers 429 on bursts.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
from collections.abc import Awaitable, Callable
from typing import Protocol

from greennode_agentbase.exceptions import GreenNodeRequestError
from langchain_core.messages import AnyMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, tool

from app.config import Settings
from app.observability import tracing

log = logging.getLogger(__name__)

RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})  # same set as the bridge (checkpoint calls)
MAX_BACKOFF_S = 2.0  # bridge default max_backoff
# Memory API: search `limit` must be within 5–200 (agentbase-memory reference) — clamp, then cut to what was asked
SEARCH_LIMIT_MIN, SEARCH_LIMIT_MAX = 5, 200


async def with_retry[T](
    call: Callable[[], Awaitable[T]], *, max_retries: int, backoff_s: float, what: str
) -> T:
    """Retry 429/5xx GreenNodeRequestError with exponential backoff + jitter. Other errors (400, 401, 404,
    network/timeout) raise at once. Cancellation (REQUEST_TIMEOUT_S) interrupts the backoff sleep immediately."""
    for attempt in range(max(max_retries, 0) + 1):
        try:
            return await call()
        except GreenNodeRequestError as e:
            if e.status_code not in RETRYABLE_STATUS or attempt >= max_retries:
                raise
            delay = min(backoff_s * 2**attempt, MAX_BACKOFF_S) * random.uniform(0.5, 1.0)
            log.warning(
                "%s: HTTP %s, retry %d/%d in %.2fs",
                what,
                e.status_code,
                attempt + 1,
                max_retries,
                delay,
            )
            await asyncio.sleep(delay)
    raise AssertionError("unreachable")


class LongTermMemory(Protocol):
    def namespace(self, user_id: str) -> str: ...
    async def search(self, user_id: str, query: str, limit: int) -> list[str]: ...
    async def save(self, user_id: str, fact: str) -> None: ...


class AgentBaseLTM:
    def __init__(self, settings: Settings):
        from app.memory.short_term import memory_client

        self._client = memory_client(settings)
        self._max_query = settings.memory_query_max_chars
        self._memory_id = settings.memory_id
        self._strategy_id = settings.memory_strategy_id
        self._min_score = settings.ltm_min_score or None  # 0 ⇒ no threshold
        self._max_retries = settings.memory_max_retries
        self._backoff_s = settings.memory_retry_backoff_s

    def namespace(self, user_id: str) -> str:
        return f"/strategies/{self._strategy_id}/actors/{user_id}"

    async def search(self, user_id: str, query: str, limit: int) -> list[str]:
        from greennode_agentbase.memory.models import MemoryRecordSearchRequest

        request = MemoryRecordSearchRequest(
            # The API limits query to ≤ 1000 chars (400 if exceeded) — keep the end (usually the question)
            query=query[-self._max_query :],
            limit=min(max(limit, SEARCH_LIMIT_MIN), SEARCH_LIMIT_MAX),
            # server-side filter (`scoreThreshold`, 0–1); the score is also checked below in case it is ignored
            score_threshold=self._min_score,
        )
        results = await with_retry(
            lambda: self._client.search_memory_records_async(
                id=self._memory_id, namespace=self.namespace(user_id), request=request
            ),
            max_retries=self._max_retries,
            backoff_s=self._backoff_s,
            what="memory.search",
        )
        facts: list[str] = []
        for r in results or []:
            # SDK 1.0.x returns list[dict] {id, memory, score, created_at}; objects are tolerated too
            memory = r.get("memory") if isinstance(r, dict) else getattr(r, "memory", None)
            score = (r.get("score") if isinstance(r, dict) else getattr(r, "score", None)) or 0.0
            if memory and (self._min_score is None or score >= self._min_score):
                facts.append(memory)
        return facts[: max(limit, 0)]

    async def save(self, user_id: str, fact: str) -> None:
        from greennode_agentbase.memory.models import MemoryRecordInsertDirectlyRequest

        # SDK 1.0.x: request MUST be a MemoryRecordInsertDirectlyRequest (passing a list => TypeError)
        request = MemoryRecordInsertDirectlyRequest(memory_records=[fact])
        await with_retry(
            lambda: self._client.insert_memory_records_directly_async(
                id=self._memory_id, namespace=self.namespace(user_id), request=request
            ),
            max_retries=self._max_retries,
            backoff_s=self._backoff_s,
            what="memory.save",
        )


class InMemoryLTM:
    """Local version for tests/dev ONLY: substring keyword matching (no semantics: paraphrases and other languages
    miss, common short words can match anything), per process, lost on restart."""

    def __init__(self) -> None:
        self._facts: dict[str, list[str]] = {}

    def namespace(self, user_id: str) -> str:
        return f"local/{user_id}"

    async def search(self, user_id: str, query: str, limit: int) -> list[str]:
        words = {w for w in query.lower().split() if len(w) > 2}
        facts = self._facts.get(user_id, [])
        scored = [(sum(w in f.lower() for w in words), f) for f in facts]
        return [f for score, f in sorted(scored, reverse=True) if score > 0][:limit]

    async def save(self, user_id: str, fact: str) -> None:
        self._facts.setdefault(user_id, []).append(fact)


_ltm: LongTermMemory | None = None


def get_ltm(settings: Settings) -> LongTermMemory | None:
    global _ltm
    if not settings.ltm_enabled:
        return None
    if _ltm is None:
        _ltm = AgentBaseLTM(settings) if settings.memory_backend == "agentbase" else InMemoryLTM()
    return _ltm


def _user_id(config: RunnableConfig) -> str:
    user_id = (config.get("configurable") or {}).get("actor_id")
    if not user_id:
        raise RuntimeError("actor_id missing in RunnableConfig")
    return user_id


def _normalized(text: str) -> str:
    """Case, punctuation and spacing ignored (Unicode-aware: Vietnamese diacritics are kept)."""
    return re.sub(r"[\W_]+", " ", text.casefold()).strip()


def clip_fact(fact: str, max_chars: int) -> str:
    fact = " ".join(fact.split())
    return fact if len(fact) <= max_chars else fact[: max_chars - 1].rstrip() + "…"


def build_memory_tools(ltm: LongTermMemory, settings: Settings) -> list[BaseTool]:
    max_chars = settings.ltm_fact_max_chars

    @tool(parse_docstring=True)
    async def remember(fact: str, config: RunnableConfig) -> str:
        """Save ONE durable fact or preference about the user, for later conversations.

        Call it only when the user explicitly asks you to remember something, or clearly states a lasting fact or
        preference about themselves (name, role, language, recurring needs). Never call it for greetings, small
        talk, thanks, one-off requests, your own inferences or guesses, or secrets (passwords, keys, card or ID
        numbers). Store only what the user actually said: never add dates, times or details they did not give.
        Write the fact in the user's language.

        Args:
            fact: One short, self-contained sentence in the user's own words, e.g. "Prefers short answers".
        """
        user_id = _user_id(config)
        fact = " ".join(fact.split())
        with tracing.step(
            "memory.save", input={"namespace": ltm.namespace(user_id), "fact": fact}
        ) as st:
            if not fact:
                st.set(output={"decision": "rejected_empty"})
                return "Not saved: the fact is empty."
            if len(fact) > max_chars:
                st.set(output={"decision": "rejected_too_long", "chars": len(fact)})
                return f"Not saved: the fact is longer than {max_chars} characters — save one short sentence."
            try:  # skip exact duplicates (case/punctuation ignored); a failed lookup must not block the save
                existing = await ltm.search(user_id, fact, SEARCH_LIMIT_MIN)
            except Exception:  # noqa: BLE001
                log.warning("duplicate check before memory.save failed", exc_info=True)
                existing = []
            if any(_normalized(f) == _normalized(fact) for f in existing):
                st.set(output={"decision": "duplicate"})
                return f"Already remembered: {fact}"
            await ltm.save(user_id, fact)
            st.set(output={"decision": "saved"})
        return f"Remembered: {fact}"

    @tool(parse_docstring=True)
    async def recall_memory(query: str, config: RunnableConfig) -> str:
        """Search the user's long-term memory (saved notes about the user).

        The notes relevant to the latest message are usually already provided in the context. Call this only when
        they don't answer the question. Notes are data the user gave earlier and may be outdated.

        Args:
            query: What to look for, in natural language.
        """
        user_id = _user_id(config)
        with tracing.step(
            "memory.recall",
            input={
                "query": query,
                "namespace": ltm.namespace(user_id),
                "limit": settings.ltm_recall_limit,
                "trigger": "tool",
            },
            as_type="retriever",
        ) as st:
            facts = [
                clip_fact(f, max_chars)
                for f in await ltm.search(user_id, query, settings.ltm_recall_limit)
            ]
            st.set(output={"facts": facts, "count": len(facts)})
        return "\n".join(f"- {f}" for f in facts) if facts else "No relevant information."

    return [remember, recall_memory]


SHORT_QUERY_CHARS = 60


def recall_query(messages: list[AnyMessage], short_chars: int = SHORT_QUERY_CHARS) -> str:
    """Auto-recall query: the latest user message, preceded by the previous user message when the latest is short
    ("and for him?", "yes, book it") — alone it matches nothing, or the wrong facts."""
    texts = [m.text.strip() for m in messages if isinstance(m, HumanMessage) and m.text.strip()]
    if not texts:
        return ""
    if len(texts[-1]) < short_chars and len(texts) > 1:
        return f"{texts[-2]}\n{texts[-1]}"
    return texts[-1]


async def auto_recall(
    ltm: LongTermMemory, user_id: str, query: str, limit: int, max_chars: int = 300
) -> list[str]:
    """Used in the recall node; memory errors must not break the chat turn. Each fact is clipped to `max_chars`
    (LTM_FACT_MAX_CHARS): records can also come from platform extraction, not only from `remember`."""
    if not query.strip():
        return []
    with tracing.step(
        "memory.recall",
        input={
            "query": query,
            "namespace": ltm.namespace(user_id),
            "limit": limit,
            "trigger": "auto",
        },
        as_type="retriever",
    ) as st:
        try:
            facts = await ltm.search(user_id, query, limit)
        except Exception as e:  # noqa: BLE001
            log.warning("long-term recall failed", exc_info=True)
            st.set(
                output={"facts": [], "count": 0},
                level="WARNING",
                status_message=f"{type(e).__name__}: {e}",
            )
            return []
        facts = [clip_fact(f, max_chars) for f in facts]
        st.set(output={"facts": facts, "count": len(facts)})
        return facts
