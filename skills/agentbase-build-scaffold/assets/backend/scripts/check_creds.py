"""Check the credentials this agent needs. Prints only OK/FAIL and non-secret identifiers — never a secret.

    make check-creds        (= cd src/backend && uv run python -m scripts.check_creds)

A CLI tool, not app runtime code — hence it lives outside app/ and may read the environment directly.

The USER sets the secrets with `make set-secret` in their own terminal (hidden input, scripts/set_secret.py) — never
by pasting them into a chat with a coding agent, which cannot type into that prompt:
    .greennode.json  IAM service account {"client_id", "client_secret"}  — /agentbase (IAM setup)   NAME=iam
    .env             LLM_API_KEY — /agentbase-llm (it can save the key itself)                    NAME=LLM_API_KEY
                     LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY (optional) — Langfuse project      NAME=langfuse
                     LLM_MODEL (+ LLM_MODEL_* tiers, fallbacks) are not secrets: plain .env values
Every model the agent may call is pinged once (a key can be valid while one tier's model is not enabled for it).
Exit code 1 if a required credential is missing or rejected (a fallback model failing is a WARN).
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable

import httpx
from dotenv import dotenv_values

from app.config import GREENNODE_LLM_BASE_URL, get_settings, normalize_iam_env

Result = tuple[str, str]  # (status: OK | FAIL | WARN | SKIP, detail)
SET = "YOU run `make set-secret NAME={}` in your own terminal"  # hint: never "paste it in the chat"
TIER_NAMES = ("reasoning", "large", "small")


def _env() -> dict[str, str]:
    """`.env` overlaid by the process env — read directly so a check still runs when Settings is invalid."""
    return {k: v for k, v in {**dotenv_values(".env"), **os.environ}.items() if v}


def check_iam() -> Result:
    normalize_iam_env()
    if not (os.getenv("GREENNODE_CLIENT_ID") and os.getenv("GREENNODE_CLIENT_SECRET")):
        return "FAIL", f"not configured — {SET.format('iam')} (writes .greennode.json)"
    from app.tools.mcp import IAMBearerAuth

    auth = IAMBearerAuth()
    try:
        auth.refresh()
    except httpx.HTTPStatusError as e:
        return (
            "FAIL",
            f"rejected by IAM (HTTP {e.response.status_code}) — wrong client_id/secret pair? "
            f"Re-enter both: {SET.format('iam')}",
        )
    except httpx.HTTPError as e:
        return "FAIL", f"IAM unreachable ({type(e).__name__})"
    return "OK", f"token issued · principal {auth.principal} (use it in Policy Group)"


def check_agent_identity() -> Result:
    """Read-only. Only needed when tools use AgentBase Identity providers (app/identity.py)."""
    from greennode_agentbase.core.config import get_config_value

    name = get_config_value("GREENNODE_AGENT_IDENTITY")
    if not name:
        return "SKIP", (
            "not set — needed only for Identity providers: `make set-secret NAME=agent_identity` "
            "(otherwise the SDK would create a NEW identity on first use)"
        )
    if name.startswith("<"):
        return (
            "FAIL",
            "agent_identity still holds the .example placeholder — `make set-secret NAME=agent_identity` "
            "(the dev identity's name) or remove the key",
        )
    return "OK", f"agent identity '{name}'"


def _json_env(env: dict[str, str], name: str, default: object) -> object:
    try:
        return json.loads(env[name]) if env.get(name) else default
    except ValueError:
        return default  # malformed: reported by the Settings check


def _models_from_env(env: dict[str, str]) -> dict[str, list[str]]:
    """Same resolution as app.llm (model_for / fallbacks_for), from the raw env — used when Settings is invalid."""
    large = env.get("LLM_MODEL_LARGE") or env.get("LLM_MODEL", "")
    shared = _json_env(env, "LLM_FALLBACK_MODELS", [])
    per_tier = _json_env(env, "LLM_TIER_FALLBACKS", {})
    roles: dict[str, list[str]] = {}
    for tier in TIER_NAMES:
        primary = env.get(f"LLM_MODEL_{tier.upper()}") or large
        roles.setdefault(primary, []).append(f"{tier} primary")
        chain = per_tier.get(tier, shared) if isinstance(per_tier, dict) else shared
        for m in chain if isinstance(chain, list) else []:
            if isinstance(m, str) and m and m != primary:
                roles.setdefault(m, []).append(f"{tier} fallback")
    return roles


def llm_models(env: dict[str, str]) -> dict[str, list[str]]:
    """model → its roles ("large primary", "small fallback"…): every model the agent may call, deduplicated."""
    try:
        get_settings()
    except ValueError:
        return {m: r for m, r in _models_from_env(env).items() if m}
    from app.llm import TIERS, fallbacks_for, model_for

    roles: dict[str, list[str]] = {}
    for tier in TIERS:
        roles.setdefault(model_for(tier), []).append(f"{tier} primary")
        for m in fallbacks_for(tier):
            roles.setdefault(m, []).append(f"{tier} fallback")
    return {m: r for m, r in roles.items() if m}


def _ping_model(base: str, key: str, model: str) -> Result:
    try:  # 1-token completion: proves key AND model path in one call
        r = httpx.post(
            f"{base}/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={
                "model": model,
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 1,
            },
            timeout=30,
        )
    except httpx.HTTPError as e:
        return "FAIL", f"LLM endpoint unreachable ({type(e).__name__})"
    if r.status_code == 403 and "not allowed" in r.text.lower():
        # MaaS: valid key, but this model is not enabled for it (seen: 403 "model not allowed for this key")
        return (
            "FAIL",
            f"model {model!r} not allowed for this LLM_API_KEY — enable it for the key on AIP",
        )
    if r.status_code in (401, 403):
        return (
            "FAIL",
            f"LLM_API_KEY rejected (HTTP {r.status_code}) — re-enter it: {SET.format('LLM_API_KEY')}",
        )
    if r.status_code >= 400:
        return (
            "FAIL",
            f"model {model!r} not usable (HTTP {r.status_code}) — use the model `path` on AIP",
        )
    return "OK", f"key accepted · model {model}"


def check_llm(env: dict[str, str]) -> list[tuple[str, Result]]:
    """One line per model: FAIL if a tier's primary is unusable, WARN if only a fallback is."""
    key, models = env.get("LLM_API_KEY"), llm_models(env)
    if not key:
        hint = f"/agentbase-llm creates one and can save it, or {SET.format('LLM_API_KEY')}"
        return [("LLM (MaaS)", ("FAIL", f"LLM_API_KEY not set in .env — {hint}"))]
    if not models:
        hint = "the model `path` from /agentbase-llm (not a secret)"
        return [("LLM (MaaS)", ("FAIL", f"LLM_MODEL not set in .env — {hint}"))]
    base = env.get("LLM_BASE_URL", GREENNODE_LLM_BASE_URL).rstrip("/")
    lines = []
    for model, roles in models.items():  # one ping per distinct model
        status, detail = _ping_model(base, key, model)
        if status == "FAIL" and not any(r.endswith("primary") for r in roles):
            status = "WARN"  # only a fallback is broken: the agent runs, without that safety net
        lines.append(("LLM (MaaS)", (status, f"[{', '.join(roles)}] {detail}")))
    return lines


def check_langfuse(env: dict[str, str]) -> Result:
    pk, sk = env.get("LANGFUSE_PUBLIC_KEY"), env.get("LANGFUSE_SECRET_KEY")
    if not (pk and sk):
        return (
            "SKIP",
            "LANGFUSE_* not set — tracing disabled (optional: `make set-secret NAME=langfuse`)",
        )
    base = env.get("LANGFUSE_BASE_URL", "https://cloud.langfuse.com").rstrip("/")
    try:
        r = httpx.get(f"{base}/api/public/projects", auth=(pk, sk), timeout=15)
    except httpx.HTTPError as e:
        return "FAIL", f"Langfuse unreachable ({type(e).__name__})"
    if r.status_code != 200:
        return "FAIL", f"Langfuse keys rejected (HTTP {r.status_code}) — {SET.format('langfuse')}"
    return "OK", f"keys accepted · {base}"


def check_config() -> Result:
    try:
        s = get_settings()
    except (
        ValueError
    ) as e:  # pydantic ValidationError: report the validator's message, not the URL footer
        errors = getattr(e, "errors", None)
        return "FAIL", errors()[0]["msg"] if callable(errors) else str(e)
    return "OK", f"APP_ENV={s.app_env} AUTH_MODE={s.auth_mode} MEMORY_BACKEND={s.memory_backend}"


def main() -> int:
    env = _env()
    checks: list[tuple[str, Callable[[], Result | list[tuple[str, Result]]]]] = [
        ("IAM (.greennode.json)", check_iam),
        ("Agent identity", check_agent_identity),
        ("LLM (MaaS)", lambda: check_llm(env)),
        ("Langfuse", lambda: check_langfuse(env)),
        ("Settings (.env)", check_config),
    ]
    failed = False
    for name, fn in checks:
        out = fn()
        for line_name, (status, detail) in out if isinstance(out, list) else [(name, out)]:
            failed |= status == "FAIL"
            print(f"{status:<4}  {line_name:<22} {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
