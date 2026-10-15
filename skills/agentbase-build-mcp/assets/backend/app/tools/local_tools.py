"""Tools written directly in the agent (in-process).

Rules:
- Only put small, pure-logic tools here that need not be shared with other agents. Shared tools
  or tools calling internal systems => write an MCP server (/agentbase-build-mcp-server) and connect via
  AgentBase MCP Gateway (Connector + Policy Group).
- POC when the internal system has no API yet: mock adapter in-process here, behind a setting BLOCKED in prod;
  keep the tool name so moving to MCP later needs no change to HITL_TOOLS / eval expected_tools.
- The docstring is the "prompt" for the LLM: describe when to use it and what the parameters mean.
- User identity: NEVER take it from tool parameters. Declare a `config: RunnableConfig` parameter (LangChain
  injects it, the LLM doesn't see it) then read:
      user_id = config["configurable"]["actor_id"]           # = authenticated user
      claims  = config["configurable"].get("user_claims", {})  # only claims in AUTH_FORWARD_CLAIMS
- External service secrets: store them in AgentBase Identity and inject with the helpers in app/identity.py
  (agent_api_key / agent_access_token / user_access_token / user_api_key — /agentbase-build-identity), never
  in the prod .env and never as a parameter of the @tool itself.
"""

from __future__ import annotations

import logging
import math
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, tool

from app.config import get_settings
from app.observability import tracing

log = logging.getLogger(__name__)


@tool
def get_current_time(timezone: str = "Asia/Ho_Chi_Minh") -> str:
    """Get the current date and time in an IANA timezone (default Asia/Ho_Chi_Minh)."""
    return datetime.now(ZoneInfo(timezone)).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- knowledge (RAG POC)
# Internal documents (.md/.txt, UTF-8) in KNOWLEDGE_DIR (default app/knowledge/, kept in the image via an
# exception in .dockerignore). Keyword search on whole words — POC only: documents in ONE language, up to a
# few dozen files. Vietnamese-aware: Unicode NFC + casefold, and a diacritic-folded match ("nghi phep" finds
# "nghỉ phép"; an exact-diacritics match ranks higher). No synonyms, no cross-language matching.
# Prod (many documents, semantic / multilingual search, frequent updates): an MCP server with a vector store
# and multilingual embeddings; keep the tool name with `"prefix": false` in mcp_servers.json and keep the
# `[Source: …]` citation format.
_WORD_RE = re.compile(r"\w+")
# Function words that match almost every passage (checked on the query's own spelling, so English "an"
# is dropped while Vietnamese "ăn" is kept).
_STOPWORDS = frozenset(
    "the and or of to in on for is are was be what which how many much do does did can with about my me "
    "you your it this that there at by from as an not no "
    "là của và được có không cho các những một với trong thì mà này đó gì nào bao nhiêu tôi bạn mình "
    "em anh chị ở về để khi như thế sao hãy xin cần phải bị đã sẽ đang rồi ạ nhé vậy".split()
)
# Letters only Vietnamese uses (ă đ ĩ ũ ơ ư + the Latin Extended Additional block ạ…ỹ)
_VIETNAMESE = re.compile("[ăđĩũơư\u1ea0-\u1ef9]")
_FOLDED_MATCH_WEIGHT = 0.7  # "nam" for "năm": still a hit, ranked below an exact-diacritics match


def _norm(text: str) -> str:
    """NFC + casefold: the same text typed with combining marks (NFD, e.g. macOS) matches."""
    return unicodedata.normalize("NFC", text).casefold()


def _fold(word: str) -> str:
    """Diacritic-insensitive form: strip combining marks and map đ→d ("nghỉ phép" → "nghi phep")."""
    decomposed = unicodedata.normalize("NFD", word)
    return "".join(c for c in decomposed if unicodedata.category(c) != "Mn").replace("đ", "d")


def _words(text: str) -> list[str]:
    return [w for w in _WORD_RE.findall(_norm(text)) if len(w) > 1]


@dataclass(frozen=True)
class _Chunk:
    file: str
    heading: str
    text: str
    exact: frozenset[str]  # normalized words of heading + passage
    folded: frozenset[str]  # the same, diacritic-folded


@dataclass(frozen=True)
class _Knowledge:
    chunks: list[_Chunk]
    idf: dict[str, float]  # folded word -> inverse document frequency (rare words weigh more)
    language: str | None  # "Vietnamese" / "English" (None = unknown): tells the LLM how to query


def _read_text(f: Path, base: Path) -> str | None:
    try:
        return f.read_text(encoding="utf-8-sig")  # -sig: tolerate a UTF-8 BOM (Windows editors)
    except (UnicodeDecodeError, OSError) as e:
        # One bad file must not break every chat request (this runs in collect_tools)
        log.warning(
            "knowledge file %s skipped (%s) — save it as UTF-8",
            f.relative_to(base),
            type(e).__name__,
        )
        return None


def _language(words: list[str]) -> str | None:
    if not words:
        return None
    if sum(bool(_VIETNAMESE.search(w)) for w in words) / len(words) > 0.1:
        return "Vietnamese"
    return "English" if all(w.isascii() for w in words) else None


@lru_cache(maxsize=1)
def _knowledge_index(directory: str) -> _Knowledge:
    """Passages split on blank lines, each tagged with its file and nearest markdown heading."""
    chunks: list[_Chunk] = []
    base = Path(directory)
    for f in sorted([*base.rglob("*.md"), *base.rglob("*.txt")]):
        if (content := _read_text(f, base)) is None:
            continue
        heading = f.stem
        for block in re.split(r"\n\s*\n", content):
            block = block.strip()
            if not block:
                continue
            if block.startswith("#"):
                first, _, rest = block.partition("\n")
                heading = first.lstrip("#").strip()
                if not rest.strip():
                    continue  # a bare heading: indexed with the passages below it
            words = _words(f"{heading}\n{block}")
            chunks.append(
                _Chunk(
                    str(f.relative_to(base)),
                    heading,
                    block,
                    frozenset(words),
                    frozenset(_fold(w) for w in words),
                )
            )
    df = Counter(w for c in chunks for w in c.folded)
    idf = {w: math.log(1 + len(chunks) / n) for w, n in df.items()}
    return _Knowledge(chunks, idf, _language([w for c in chunks for w in c.exact]))


@tool
def search_knowledge(query: str) -> str:
    """Search internal documents (policies, procedures, FAQ) for passages relevant to the question.
    Always use this tool before answering questions about policies/regulations; cite [Source: ...].
    Keyword search on whole words over documents written in ONE language:
    write the query in the documents' language (translate it if needed), using the key terms.

    Args:
        query: The key terms to look up, in the documents' language.
    """
    s = get_settings()
    kb = _knowledge_index(s.knowledge_dir)
    words = list(dict.fromkeys(_words(query)))
    words = [w for w in words if w not in _STOPWORDS] or words
    with tracing.step("knowledge.search", input={"query": query}, as_type="retriever") as st:
        scored = []
        for c in kb.chunks:
            score = sum(
                kb.idf.get(_fold(w), 0.0)
                * (1.0 if w in c.exact else _FOLDED_MATCH_WEIGHT if _fold(w) in c.folded else 0.0)
                for w in words
            )
            if score:
                scored.append((score, c))
        top = sorted(scored, key=lambda x: -x[0])[: s.knowledge_top_k]
        st.set(output={"hits": [(c.file, c.heading, round(sc, 2)) for sc, c in top]})
    if not top:
        lang = kb.language or "the documents' language"
        return (
            f"NO_MATCH: no internal document passage matches {query!r}. The documents are written in "
            f"{lang}: retry ONCE with synonyms / other key terms in {lang}. If that finds nothing "
            "either, tell the user this information is not available in the internal documents yet."
        )
    return "\n\n".join(
        f"[Source: {c.file} — {c.heading}]\n{c.text[: s.knowledge_snippet_chars]}" for _, c in top
    )


# --------------------------------------------------------------------------- per-user tool example
@tool
def whoami(config: RunnableConfig) -> str:
    """Tell who the current user is according to the system (use when the user asks about their account)."""
    conf = config.get("configurable") or {}
    claims = conf.get("user_claims") or {}
    return f"user_id={conf.get('actor_id')} claims={sorted(claims)}"


# Example tool calling an external service with an API key stored in AgentBase Identity
# (decorate the INNER function so the key never appears in the tool schema — /agentbase-build-identity):
#
# from app.identity import agent_api_key
#
# @agent_api_key("weather-api-key")
# async def _fetch_weather(city: str, *, api_key: str) -> dict: ...
#
# @tool
# async def get_weather(city: str) -> str:
#     """Current weather for a city."""
#     return str(await _fetch_weather(city))


def get_local_tools() -> list[BaseTool]:
    tools: list[BaseTool] = [get_current_time]
    kb = _knowledge_index(get_settings().knowledge_dir)
    if kb.chunks:  # only enabled when documents exist
        # Tell the LLM the documents' language: keyword search cannot match a query in another language
        desc = search_knowledge.description.replace("the documents' language", kb.language or "")
        tools.append(
            search_knowledge.model_copy(update={"description": desc})
            if kb.language
            else search_knowledge
        )
    return tools
