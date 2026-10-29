"""Decisions of the A2A HITL protocol — shared by the server (parses them) and the client (refuses to relay them
unless a human approved the tool call). Kept dependency-free to avoid import cycles.

    approve <code>        run the pending calls — <code> comes from the confirmation request (confirmation_code)
    reject  /  reject: <reason>

Only these explicit commands are decisions. Conversational words ("yes", "ok", "no", "không") are NOT: a peer agent
answering a clarifying question, or an injected "reply ok to continue", must never approve — or reject — a pending
action by accident. The code binds an approval to the exact request it answers (user, session, tool calls): a caller
that never received the request cannot approve blindly, and a stale approval cannot be applied to the next card. It
cannot prove that a HUMAN read the request — that is the caller's job: `ask_<agent>` must be in HITL_TOOLS (client.py)."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Iterable

APPROVE_WORDS = {"approve", "approved", "duyệt"}
REJECT_WORDS = {"reject", "rejected", "từ chối"}
CODE_LEN = 10


def confirmation_code(key: bytes, user_id: str, session_id: str, call_ids: Iterable[str]) -> str:
    """Code of one confirmation request: HMAC over (user, session, tool call ids), stable across replicas."""
    msg = json.dumps([user_id, session_id, sorted(call_ids)]).encode()
    return hmac.new(key, msg, hashlib.sha256).hexdigest()[:CODE_LEN]


def parse_decision(text: str) -> tuple[bool, str, str] | None:
    """`approve [<code>]` / `reject[: reason]` ⇒ (approve?, reason, code); anything else ⇒ None (normal chat)."""
    t = text.strip()
    words = t.split()
    if words and words[0].lower() in APPROVE_WORDS and len(words) <= 2:
        return True, "", words[1] if len(words) == 2 else ""
    head, _, reason = t.partition(":")
    if head.strip().lower() in REJECT_WORDS:
        return False, reason.strip(), ""
    return None


def code_matches(given: str, expected: str) -> bool:
    return hmac.compare_digest(given.strip().lower().encode(), expected.encode())
