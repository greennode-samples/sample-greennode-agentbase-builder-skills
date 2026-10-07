"""Prompt Management: a blank/invalid LANGFUSE_PROMPT_CACHE_TTL must not silently switch to the local prompt."""

from __future__ import annotations

import pytest

import app.prompts as prompts


class _Prompt:
    is_fallback = False

    def compile(self) -> str:
        return "REMOTE PROMPT"


class _Client:
    def __init__(self):
        self.kwargs: dict = {}

    def get_prompt(self, name, **kwargs):
        self.kwargs = kwargs
        return _Prompt()


@pytest.mark.parametrize(("ttl", "expected"), [("", 300), ("5m", 300), ("60", 60)])
def test_cache_ttl_env(monkeypatch, ttl, expected):
    client = _Client()
    monkeypatch.setattr(prompts, "get_client", lambda: client)
    monkeypatch.setenv("LANGFUSE_PROMPT_NAME", "agent-system")
    monkeypatch.setenv("LANGFUSE_PROMPT_CACHE_TTL", ttl)
    text, linked = prompts.get_system_prompt()
    assert text == "REMOTE PROMPT" and linked is not None
    assert client.kwargs["cache_ttl_seconds"] == expected
