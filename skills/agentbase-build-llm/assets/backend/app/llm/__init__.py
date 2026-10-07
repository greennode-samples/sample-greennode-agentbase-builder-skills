"""LLM factory: capability TIERS + a TASK → tier table + FALLBACK. The ONLY place that creates LLMs.

Standard LLM source: GreenNode AI Platform (MaaS) `https://maas-llm-aiplatform-hcm.api.vngcloud.vn/v1`
(OpenAI-compatible). Model = the `path` field on AIP (/agentbase-llm). Choosing a model per tier: skill
agentbase-build-llm.

1) Capability tiers (each tier: 1 primary model + its own fallback chain)
   reasoning : multi-step reasoning, planning, analysis/math/code, hard grading      (LLM_MODEL_REASONING)
   large     : strong general-purpose + reliable tool calling — default agent       (LLM_MODEL_LARGE | LLM_MODEL)
   small     : fast/cheap — router, classification, extraction, summaries, simple Qs (LLM_MODEL_SMALL)
   Empty tier ⇒ use `large`.

2) Task → tier: LLM_TASK_TIERS overrides DEFAULT_TASK_TIERS. Code only calls get_llm("<task>").

3) Fallback: LLM_TIER_FALLBACKS {"large": [...], ...} or LLM_FALLBACK_MODELS (shared). Fall back only on
   INFRA/MODEL errors (connection lost, timeout, 429, 5xx, model removed/not permitted, provider error inside a
   streamed response); NOT on request errors (400, 401, 422...).
   Langfuse: failed (ERROR) generation of the primary model → generation of the fallback model.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from functools import lru_cache
from typing import Any, Literal

import openai
from langchain_core.language_models import BaseChatModel
from langchain_core.runnables import Runnable, RunnableBinding
from langchain_core.tools import BaseTool
from langchain_openai import ChatOpenAI

from app.config import get_settings

log = logging.getLogger(__name__)

Tier = Literal["reasoning", "large", "small"]
TIERS: tuple[Tier, ...] = ("reasoning", "large", "small")

# Template's standard tasks → tier. Add new tasks (e.g. "planner", "sql") here + LLM_TASK_TIERS.
DEFAULT_TASK_TIERS: dict[str, Tier] = {
    "agent": "large",  # main agent, tool calling (when adaptive routing is off)
    "agent_simple": "small",  # adaptive: greetings, single-step questions, simple lookups
    "agent_complex": "reasoning",  # adaptive: multi-step, analysis, comparison, calculation
    "router": "small",  # scores question complexity (adaptive routing)
    "summarize": "small",  # context compression
    "judge": "large",  # in-turn self-eval (reflection) — must be fast
    "eval_judge": "reasoning",  # offline grading — accuracy first
}


class ProviderStreamError(openai.APIError):
    """The provider failed INSIDE a 200 streaming response: vLLM-style `data: {"error": {...}}` SSE chunk (engine
    overloaded/crashed mid-answer). The openai SDK raises that as a BARE `openai.APIError` (no HTTP status) — an
    infrastructure failure, but the fallback matches exception CLASSES and APIError is also the base of the request
    errors (BadRequestError, AuthenticationError...) ⇒ `_FallbackEligible` re-raises exactly-APIError as this
    subclass, and only this subclass is in FALLBACK_ERRORS."""


def _provider_stream_error(e: openai.APIError) -> ProviderStreamError:
    err = ProviderStreamError(e.message, e.request, body=e.body)
    err.__cause__ = e
    return err


@contextmanager
def _stream_errors_fall_back() -> Iterator[None]:
    try:
        yield
    except openai.APIError as e:
        if (
            type(e) is not openai.APIError
        ):  # 4xx subclasses, context overflow, validation… ⇒ unchanged
            raise
        raise _provider_stream_error(e) from e


# Errors worth retrying on another model. langchain_openai errors (OpenAIRateLimitError, ...) inherit from these.
FALLBACK_ERRORS: tuple[type[BaseException], ...] = (
    openai.APIConnectionError,  # includes APITimeoutError
    openai.RateLimitError,
    openai.InternalServerError,
    openai.NotFoundError,  # model removed / wrong path
    openai.PermissionDeniedError,  # model not enabled for this key
    ProviderStreamError,  # SSE error chunk mid-stream (see _FallbackEligible)
)


class _FallbackEligible(RunnableBinding):
    """Binding around each model in the chain: a bare `openai.APIError` → `ProviderStreamError` (fallback-eligible).
    Every other exception passes through unchanged."""

    def invoke(self, input: Any, config: Any = None, **kwargs: Any) -> Any:
        with _stream_errors_fall_back():
            return super().invoke(input, config, **kwargs)

    async def ainvoke(self, input: Any, config: Any = None, **kwargs: Any) -> Any:
        with _stream_errors_fall_back():
            return await super().ainvoke(input, config, **kwargs)

    def stream(self, input: Any, config: Any = None, **kwargs: Any) -> Iterator[Any]:
        with _stream_errors_fall_back():
            yield from super().stream(input, config, **kwargs)

    async def astream(self, input: Any, config: Any = None, **kwargs: Any) -> AsyncIterator[Any]:
        with _stream_errors_fall_back():
            async for chunk in super().astream(input, config, **kwargs):
                yield chunk

    # batch(return_exceptions=True) — used by RunnableWithFallbacks.batch — returns errors in place of outputs
    def batch(self, inputs: list, config: Any = None, **kwargs: Any) -> list:
        with _stream_errors_fall_back():
            return [_bare_to_provider(o) for o in super().batch(inputs, config, **kwargs)]

    async def abatch(self, inputs: list, config: Any = None, **kwargs: Any) -> list:
        with _stream_errors_fall_back():
            return [_bare_to_provider(o) for o in await super().abatch(inputs, config, **kwargs)]


def _bare_to_provider(o: Any) -> Any:
    return _provider_stream_error(o) if type(o) is openai.APIError else o


def tier_for(task: str) -> Tier:
    s = get_settings()
    tier = s.llm_task_tiers.get(task) or DEFAULT_TASK_TIERS.get(task) or "large"
    if tier not in TIERS:
        raise ValueError(f"LLM_TASK_TIERS[{task}]={tier} is invalid (only {TIERS})")
    return tier  # type: ignore[return-value]


def model_for(tier: Tier) -> str:
    s = get_settings()
    large = s.llm_model_large or s.llm_model
    return {"reasoning": s.llm_model_reasoning, "large": large, "small": s.llm_model_small}[
        tier
    ] or large


def fallbacks_for(tier: Tier) -> list[str]:
    s = get_settings()
    chain = s.llm_tier_fallbacks.get(tier, s.llm_fallback_models)
    primary, out = model_for(tier), []
    for m in chain:
        if m and m != primary and m not in out:
            out.append(m)
    return out


@lru_cache(maxsize=32)
def _chat(model: str, tier: Tier, role: str) -> ChatOpenAI:
    s = get_settings()
    return ChatOpenAI(
        model=model,
        base_url=s.llm_base_url,
        api_key=s.llm_api_key,
        temperature=s.llm_temperature if tier != "small" else 0,
        max_tokens=s.llm_max_tokens,
        timeout=_timeout_s(tier),  # reasoning models run longer
        # With fallbacks the PRIMARY doesn't retry (switch fast); fallbacks keep retries so a 429 still gets the
        # SDK's backoff — the MaaS rate limit is per account, shared by every model in the chain.
        max_retries=0 if role == "primary" and fallbacks_for(tier) else s.llm_max_retries,
        streaming=s.llm_streaming,
        stream_usage=s.llm_stream_usage,
        tags=[f"llm.{tier}", f"llm.{role}"],
        metadata={"llm_tier": tier, "llm_role": role, "llm_base_url": s.llm_base_url},
    )


def _timeout_s(tier: Tier) -> float:
    """Per-attempt timeout. httpx timeout = per read ⇒ when streaming it is an INACTIVITY timeout (time to first
    token / between chunks), not a cap on the whole answer."""
    return get_settings().llm_timeout_s * (2 if tier == "reasoning" else 1)


# openai SDK retry sleep (openai/_constants.py, checked by a test): 0.5 s × 2^n, capped at 8 s; jitter only
# shortens it. A server `Retry-After` (SDK honours up to 120 s) REPLACES it — that is not budgeted here.
SDK_INITIAL_RETRY_DELAY_S = 0.5
SDK_MAX_RETRY_DELAY_S = 8.0


def retry_sleep_s(retries: int) -> float:
    """Upper bound of the SDK's exponential backoff across `retries` retries of ONE model (0.5 + 1 + 2 + 4 + 8 + 8…)."""
    return sum(
        min(SDK_INITIAL_RETRY_DELAY_S * 2**n, SDK_MAX_RETRY_DELAY_S) for n in range(max(retries, 0))
    )


def _budget(tier: Tier) -> tuple[int, float, float]:
    """(attempts, per-attempt timeout, total retry sleep) of ONE call through the tier's chain. With fallbacks the
    primary makes 1 attempt (max_retries=0) and each fallback 1 + LLM_MAX_RETRIES; without, the primary retries."""
    n_fallbacks, retries = len(fallbacks_for(tier)), get_settings().llm_max_retries
    retrying_models = n_fallbacks or 1
    attempts = (1 if n_fallbacks else 0) + retrying_models * (1 + retries)
    return attempts, _timeout_s(tier), retrying_models * retry_sleep_s(retries)


def worst_case_s(tier: Tier) -> float:
    """Every attempt of every model in the chain hangs until its timeout, + the SDK's retry sleeps."""
    attempts, per_attempt, sleep = _budget(tier)
    return attempts * per_attempt + sleep


@lru_cache(maxsize=8)
def _warn_if_over_budget(tier: Tier) -> None:
    """If the worst case exceeds REQUEST_TIMEOUT_S the request is cancelled before the last model in the chain can
    answer — the end of the fallback chain is then useless. Logged once per tier (first get_llm of that tier)."""
    s = get_settings()
    attempts, per_attempt, sleep = _budget(tier)
    worst = attempts * per_attempt + sleep
    if worst > s.request_timeout_s:
        log.warning(
            "LLM tier %s: worst case %d attempts × %.0fs + %.1fs retry backoff = %.1fs > REQUEST_TIMEOUT_S=%.0fs "
            "(%d fallback(s), LLM_MAX_RETRIES=%d) — lower LLM_TIMEOUT_S / LLM_MAX_RETRIES / the number of "
            "fallbacks, or raise REQUEST_TIMEOUT_S (recommended values: skill agentbase-build-llm §4)",
            tier,
            attempts,
            per_attempt,
            sleep,
            worst,
            s.request_timeout_s,
            len(fallbacks_for(tier)),
            s.llm_max_retries,
        )


def get_chat_model(task: str = "agent") -> BaseChatModel:
    """PRIMARY model for the task (no fallback)."""
    tier = tier_for(task)
    return _chat(model_for(tier), tier, "primary")


def get_llm(task: str = "agent", tools: list[BaseTool] | None = None) -> Runnable:
    """Runnable for the task: the tier's primary model (+tools) with that tier's fallback chain.

    Names on Langfuse (the trace tree shows which task called which model):
      llm.<task> | <caller run_name>    task chain (metadata llm_task, llm_tier, prompt link)
        <model>                         primary model GENERATION (model, usage, cost, TTFT)
        <model> (fallback)              backup model GENERATION — only when the primary fails
    """
    tier = tier_for(task)
    primary_model = model_for(tier)
    _warn_if_over_budget(tier)

    def _gen(model: str, role: str) -> Runnable:
        r: Runnable = _chat(model, tier, role)
        if tools:
            r = r.bind_tools(tools)
        display = model if role == "primary" else f"{model} (fallback)"
        # RunnableWithFallbacks passes the chain's run_name down to child models (overriding with_config) ⇒ use
        # config_factories (applied LAST) so the generation always carries the model name on Langfuse.
        # _FallbackEligible: provider errors inside a streamed response fall back too (see ProviderStreamError).
        return _FallbackEligible(bound=r, config_factories=[lambda _cfg: {"run_name": display}])

    primary = _gen(primary_model, "primary")
    backups = [_gen(m, "fallback") for m in fallbacks_for(tier)]
    # ALWAYS wrap in 1 chain (even without fallbacks) ⇒ a caller-set run_name only renames the chain,
    # the inner generation always carries the model name; `langfuse_prompt` metadata passed at call time
    # attaches to this chain ⇒ only that call's generation gets linked to the prompt.
    return primary.with_fallbacks(backups, exceptions_to_handle=FALLBACK_ERRORS).with_config(
        run_name=f"llm.{task}", metadata={"llm_task": task, "llm_tier": tier}
    )
