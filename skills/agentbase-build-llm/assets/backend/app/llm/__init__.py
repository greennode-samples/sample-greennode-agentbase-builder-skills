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
   Langfuse: failed (ERROR) generation of the primary model → generation of the fallback model (same name
   `llm.<task>.generate`, metadata llm_role=fallback, `model` attribute = the backup model).
   401/402/403/404 are CONFIGURATION errors (key, plan, model not enabled/wrong path): logged once per (model,
   status) at ERROR naming the env var to fix — a 403/404 primary would otherwise fall back silently on every call.

4) Thinking: MaaS reasoning models return their thinking in the non-standard `reasoning_content` (or
   `reasoning`) field, which ChatOpenAI drops ⇒ `_ChatMaaS` keeps it in generation_info[REASONING_KEY]; the
   tracing CallbackHandler shows it in the generation output. LangChain also copies generation_info into the
   message's response_metadata ⇒ `_FallbackEligible` removes it from every returned message/chunk: the thinking
   never enters the checkpoint or the next request.

5) Output caps: LLM_TASK_MAX_TOKENS overrides DEFAULT_TASK_MAX_TOKENS (internal flows bounded — reasoning tokens
   count too); other tasks use LLM_MAX_TOKENS. Structured output: get_llm(task, schema=Model).

6) Thinking: LLM_THINKING_BUDGET (per task or tier) overrides DEFAULT_THINKING_BUDGET ⇒ request fields MaaS honors
   (measured, skill §6): N > 0 ⇒ `thinking_budget: N` (reasoning capped at N tokens), 0 ⇒ `enable_thinking: false`.
   Sent to every model of the task's chain (fallbacks too). OpenAI `reasoning_effort` is NOT used: honored by some
   MaaS models only (deepseek-v4-pro: low = off; qwen3.8-flash: ignored).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from functools import lru_cache
from typing import Any, Literal

import openai
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage
from langchain_core.outputs import ChatGenerationChunk, ChatResult
from langchain_core.runnables import Runnable, RunnableBinding, RunnableLambda
from langchain_core.tools import BaseTool
from langchain_openai import ChatOpenAI

from app.config import LLM_TIERS, get_settings
from app.observability.tracing import REASONING_KEY

log = logging.getLogger(__name__)

Tier = Literal["reasoning", "large", "small"]
TIERS: tuple[Tier, ...] = LLM_TIERS  # type: ignore[assignment]  # Settings validates LLM_* tables against it

# Template's standard tasks → tier. Add new tasks (e.g. "planner", "sql") here + LLM_TASK_TIERS.
DEFAULT_TASK_TIERS: dict[str, Tier] = {
    "agent": "large",  # main agent, tool calling (when adaptive routing is off)
    "agent_simple": "small",  # adaptive: greetings, single-step questions, simple lookups
    "agent_complex": "reasoning",  # adaptive: multi-step, analysis, comparison, calculation
    "router": "small",  # scores question complexity (adaptive routing)
    "summarize": "small",  # context compression
    "judge": "small",  # in-turn self-eval (reflection) — the user is waiting: fast, low-reasoning model
    "eval_judge": "reasoning",  # offline grading — accuracy first
}

# Output cap (max_tokens, reasoning tokens included) of the internal flows: bounds their latency and cost when a
# thinking model is used. LLM_TASK_MAX_TOKENS overrides (0 ⇒ no per-task cap); tasks not listed use LLM_MAX_TOKENS.
# A capped answer ends with finish_reason=length ⇒ WARNING (router ⇒ "standard", judge ⇒ pass, summary ⇒ skipped).
DEFAULT_TASK_MAX_TOKENS: dict[str, int] = {"router": 1024, "judge": 2048, "summarize": 4096}

# Thinking per task (see 6). LLM_THINKING_BUDGET overrides by task or tier; -1 there ⇒ the model's own default.
# Defaults from MaaS measurements (skill §6): internal JSON flows don't need to think — a cut thinking may continue in
# the answer text, so they get thinking OFF rather than a budget. Conversation tasks keep the model's thinking.
DEFAULT_THINKING_BUDGET: dict[str, int] = {"router": 0, "judge": 0}

# Tasks whose generations ARE the conversation the user sees (node `agent`). Langfuse's session view shows, per
# trace, only the generations at the SHALLOWEST tree depth as the conversation; every other task (router, summarize,
# judge, eval_judge, planner…) is an internal call nested one level deeper, so it stays in the trace tree but no
# longer appears as an extra "thread" (router JSON, judge critique) between the user's messages.
CONVERSATION_TASKS: frozenset[str] = frozenset({"agent", "agent_simple", "agent_complex"})


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


# 401/402/403/404: the key, the plan or the model `path` is wrong — not a transient failure. Logged ONCE per
# (model, status) per process; 403/404 still fall back (the backup may be enabled) but must be fixed.
CONFIG_ERRORS: dict[int, str] = {
    401: "API key rejected — wrong or deleted LLM_API_KEY, or a Token Plan key on the pay-as-you-go host",
    402: "payment required — Token Plan expired or out of balance",
    403: "model not allowed for this API key — not enabled, or not in the key's plan",
    404: "model not found — wrong model `path`, or not enabled for this key",
}
_config_errors_logged: set[tuple[str, int]] = set()


def config_error_hint(exc: BaseException) -> str | None:
    """Caller-facing message when an LLM call failed on a CONFIGURATION error (401/402/403/404 — after every
    fallback, the chain raises the primary's error); None for any other error. The details (model, env var) are in
    the ERROR log, not in this message."""
    status = getattr(exc, "status_code", None) if isinstance(exc, openai.APIStatusError) else None
    if status not in CONFIG_ERRORS:
        return None
    return (
        f"LLM configuration error (HTTP {status}: {CONFIG_ERRORS[status].split(' — ')[0]}) — the operator must "
        "fix the LLM settings (see the server log)"
    )


def _model_env(tier: str, role: str) -> str:
    """The env var that put this model in the chain."""
    s = get_settings()
    if role != "primary":
        return "LLM_TIER_FALLBACKS" if tier in s.llm_tier_fallbacks else "LLM_FALLBACK_MODELS"
    own = {"reasoning": s.llm_model_reasoning, "small": s.llm_model_small}.get(tier)
    if own:
        return f"LLM_MODEL_{tier.upper()}"
    return "LLM_MODEL_LARGE" if s.llm_model_large else "LLM_MODEL"


def _log_config_error(e: BaseException, model: str, tier: str, role: str) -> None:
    status = getattr(e, "status_code", None) if isinstance(e, openai.APIStatusError) else None
    if status not in CONFIG_ERRORS or (model, status) in _config_errors_logged:
        return
    _config_errors_logged.add((model, status))
    env = "LLM_API_KEY" if status in (401, 402) else _model_env(tier, role)
    log.error(
        "LLM model %r (tier %s, %s) → HTTP %d: %s. Fix %s (keys and ENABLED models: /agentbase-llm)%s. "
        "Logged once per model and status.",
        model,
        tier,
        role,
        status,
        CONFIG_ERRORS[status],
        env,
        " — until then EVERY call fails over to the fallback" if status in (403, 404) else "",
    )


def _without_reasoning(out: Any) -> Any:
    """LangChain copies generation_info into the message's response_metadata ⇒ the thinking text would be stored
    in the checkpoint (growing it every turn) and returned to callers. Tracing has already read it from
    generation_info (on_llm_end fires before the output is returned) ⇒ drop it from a COPY of the message."""
    meta = getattr(out, "response_metadata", None)
    if isinstance(out, BaseMessage) and isinstance(meta, dict) and REASONING_KEY in meta:
        kept = {k: v for k, v in meta.items() if k != REASONING_KEY}
        return out.model_copy(update={"response_metadata": kept})
    return out


_truncation_logged: set[tuple[str, str]] = set()


class _FallbackEligible(RunnableBinding):
    """Binding around each model in the chain:
    - a bare `openai.APIError` → `ProviderStreamError` (fallback-eligible); every other exception passes unchanged;
    - 401/402/403/404 ⇒ one ERROR log naming the env var to fix (see CONFIG_ERRORS);
    - the thinking text is removed from the returned message / chunks (see _without_reasoning);
    - an answer cut by max_tokens (finish_reason=length) ⇒ one WARNING per (task, model)."""

    llm_model: str = ""
    llm_tier: str = ""
    llm_role: str = ""
    llm_task: str = ""

    @contextmanager
    def _errors(self) -> Iterator[None]:
        try:
            with _stream_errors_fall_back():
                yield
        except openai.APIStatusError as e:
            _log_config_error(e, self.llm_model, self.llm_tier, self.llm_role)
            raise

    def _out(self, out: Any) -> Any:
        # batch(return_exceptions=True) returns errors in place of outputs
        if isinstance(out, BaseException):
            _log_config_error(out, self.llm_model, self.llm_tier, self.llm_role)
            return _bare_to_provider(out)
        out = _without_reasoning(out)
        meta = getattr(out, "response_metadata", None)
        if isinstance(meta, dict) and meta.get("finish_reason") == "length":
            if (self.llm_task, self.llm_model) not in _truncation_logged:
                _truncation_logged.add((self.llm_task, self.llm_model))
                log.warning(
                    "LLM task %s (model %r) hit its output cap (finish_reason=length) — the answer is cut. Use a "
                    "non-thinking model for this flow or raise LLM_TASK_MAX_TOKENS / LLM_MAX_TOKENS (llm skill §4)",
                    self.llm_task,
                    self.llm_model,
                )
        return out

    def invoke(self, input: Any, config: Any = None, **kwargs: Any) -> Any:
        with self._errors():
            return self._out(super().invoke(input, config, **kwargs))

    async def ainvoke(self, input: Any, config: Any = None, **kwargs: Any) -> Any:
        with self._errors():
            return self._out(await super().ainvoke(input, config, **kwargs))

    def stream(self, input: Any, config: Any = None, **kwargs: Any) -> Iterator[Any]:
        with self._errors():
            for chunk in super().stream(input, config, **kwargs):
                yield _without_reasoning(chunk)

    async def astream(self, input: Any, config: Any = None, **kwargs: Any) -> AsyncIterator[Any]:
        with self._errors():
            async for chunk in super().astream(input, config, **kwargs):
                yield _without_reasoning(chunk)

    # batch(return_exceptions=True) — used by RunnableWithFallbacks.batch — returns errors in place of outputs
    def batch(self, inputs: list, config: Any = None, **kwargs: Any) -> list:
        with self._errors():
            return [self._out(o) for o in super().batch(inputs, config, **kwargs)]

    async def abatch(self, inputs: list, config: Any = None, **kwargs: Any) -> list:
        with self._errors():
            return [self._out(o) for o in await super().abatch(inputs, config, **kwargs)]


def _bare_to_provider(o: Any) -> Any:
    return _provider_stream_error(o) if type(o) is openai.APIError else o


def tier_for(task: str) -> Tier:
    s = get_settings()
    tier = s.llm_task_tiers.get(task) or DEFAULT_TASK_TIERS.get(task) or "large"
    if tier not in TIERS:
        raise ValueError(f"LLM_TASK_TIERS[{task}]={tier} is invalid (only {TIERS})")
    return tier  # type: ignore[return-value]


def max_tokens_for(task: str) -> int | None:
    """Per-task output cap (None ⇒ LLM_MAX_TOKENS, set on the model)."""
    cap = get_settings().llm_task_max_tokens.get(task, DEFAULT_TASK_MAX_TOKENS.get(task))
    return cap or None


def thinking_budget_for(task: str) -> int | None:
    """Thinking control of a task: N > 0 tokens, 0 = off, None = the model's default. LLM_THINKING_BUDGET by task,
    then by the task's tier, then DEFAULT_THINKING_BUDGET."""
    table = get_settings().llm_thinking_budget
    for key in (task, tier_for(task)):
        if key in table:
            return None if table[key] < 0 else table[key]
    return DEFAULT_THINKING_BUDGET.get(task)


def thinking_params(budget: int | None) -> dict[str, Any]:
    """Request fields (sent as `extra_body`) for a thinking budget — the ones MaaS was measured to honor."""
    if budget is None:
        return {}
    return {"enable_thinking": False} if budget == 0 else {"thinking_budget": budget}


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


def _reasoning_text(message: Any) -> str:
    """Thinking text of a response message / stream delta: `reasoning_content` (DeepSeek, SGLang, older vLLM) or
    `reasoning` (newer vLLM, OpenRouter). Both are non-standard ⇒ ChatOpenAI drops them."""
    if not isinstance(message, dict):
        return ""
    text = message.get("reasoning_content") or message.get("reasoning")
    return text if isinstance(text, str) else ""


class _ChatMaaS(ChatOpenAI):
    """ChatOpenAI that keeps the provider's thinking text in generation_info[REASONING_KEY] (streamed deltas are
    concatenated when LangChain merges the chunks). generation_info is only seen by callbacks — Langfuse shows it
    on the generation; the AIMessage, the checkpoint and later requests stay unchanged."""

    def _create_chat_result(self, response: Any, generation_info: dict | None = None) -> ChatResult:
        result = super()._create_chat_result(response, generation_info)
        raw = response if isinstance(response, dict) else response.model_dump()
        for gen, choice in zip(result.generations, raw.get("choices") or [], strict=False):
            if text := _reasoning_text((choice or {}).get("message")):
                gen.generation_info = {**(gen.generation_info or {}), REASONING_KEY: text}
        return result

    def _convert_chunk_to_generation_chunk(
        self, chunk: dict, default_chunk_class: type, base_generation_info: dict | None
    ) -> ChatGenerationChunk | None:
        gen = super()._convert_chunk_to_generation_chunk(
            chunk, default_chunk_class, base_generation_info
        )
        choices = (chunk.get("choices") if isinstance(chunk, dict) else None) or []
        if gen is not None and choices and (text := _reasoning_text(choices[0].get("delta"))):
            gen.generation_info = {**(gen.generation_info or {}), REASONING_KEY: text}
        return gen


@lru_cache(maxsize=32)
def _chat(model: str, tier: Tier, role: str) -> ChatOpenAI:
    s = get_settings()
    return _ChatMaaS(
        model=model,
        base_url=s.llm_base_url,
        api_key=s.llm_api_key,
        temperature=s.llm_temperature if tier != "small" else 0,
        max_tokens=s.llm_max_tokens or None,  # 0 ⇒ no cap
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
    answer — the end of the fallback chain is then useless. Logged once per tier (first get_llm, every tier)."""
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


@lru_cache(maxsize=1)
def _warn_budgets() -> None:
    """Every tier on the FIRST get_llm call (startup), not only when a rarely used tier is first called."""
    for tier in TIERS:
        _warn_if_over_budget(tier)


def get_chat_model(task: str = "agent") -> BaseChatModel:
    """PRIMARY model for the task — no fallback, no per-task cap, and its messages keep the thinking text in
    response_metadata. Graph code uses get_llm."""
    tier = tier_for(task)
    return _chat(model_for(tier), tier, "primary")


def get_llm(
    task: str = "agent",
    tools: list[BaseTool] | None = None,
    *,
    schema: Any = None,
    method: Literal["function_calling", "json_mode", "json_schema"] = "function_calling",
) -> Runnable:
    """Runnable for the task: the tier's primary model (+tools) with that tier's fallback chain.

    `schema` (Pydantic model / TypedDict / JSON schema) ⇒ structured output, applied to EVERY model of the chain
    (each parses its own answer), for internal and conversation tasks alike — instead of
    `get_llm(...).with_structured_output(...)`, which internal tasks (a RunnableLambda) don't have. Default method
    `function_calling` (tool calling is verified on MaaS; json_schema/json_mode are not). Exclusive with `tools`.

    Names on Langfuse — stable like an API (evaluators, dashboards and saved views target them), never the model:
      llm.<task> | <caller run_name>    task chain (metadata llm_task, llm_tier, prompt link)
        llm.<task>.generate             primary GENERATION (model attribute, usage, cost, TTFT, thinking)
        llm.<task>.generate             backup GENERATION — only when the primary fails (metadata llm_role=fallback)
    Internal tasks (not in CONVERSATION_TASKS) get one more level: <caller run_name> | <task>.call → llm.<task> → …
    """
    if schema is not None and tools:
        raise ValueError("get_llm: pass either tools or schema, not both")
    tier = tier_for(task)
    primary_model = model_for(tier)
    _warn_budgets()
    gen_name = f"llm.{task}.generate"
    cap = max_tokens_for(task)
    thinking = thinking_budget_for(task)
    extra_body = thinking_params(thinking)

    def _gen(model: str, role: str) -> Runnable:
        chat = _chat(model, tier, role)
        r: Runnable = chat
        if schema is not None:
            r = chat.with_structured_output(schema, method=method)  # type: ignore[call-arg]
        elif tools:
            r = chat.bind_tools(tools)
        if cap:
            r = r.bind(max_tokens=cap)
        if extra_body:  # the whole chain: a fallback model must not think longer than the primary
            r = r.bind(extra_body=extra_body)
        # RunnableWithFallbacks passes the chain's run_name down to child models (overriding with_config) ⇒ use
        # config_factories (applied LAST) so the generation keeps its own stable name whatever the caller sets.
        # The model is NOT part of the name (Langfuse best practice): it is the generation's `model` attribute.
        # _FallbackEligible: provider errors inside a streamed response fall back too (see ProviderStreamError),
        # config errors are logged, the thinking text is removed from the returned message.
        return _FallbackEligible(
            bound=r,
            config_factories=[lambda _cfg: {"run_name": gen_name}],
            llm_model=model,
            llm_tier=tier,
            llm_role=role,
            llm_task=task,
        )

    primary = _gen(primary_model, "primary")
    backups = [_gen(m, "fallback") for m in fallbacks_for(tier)]
    # ALWAYS wrap in 1 chain (even without fallbacks) ⇒ a caller-set run_name only renames the chain,
    # the inner generation keeps `llm.<task>.generate`; `langfuse_prompt` metadata passed at call time
    # attaches to this chain ⇒ only that call's generation gets linked to the prompt.
    meta: dict[str, Any] = {"llm_task": task, "llm_tier": tier}
    if thinking is not None:
        meta["llm_thinking_budget"] = (
            thinking  # what the request asked for (0 = off) — debugging slow/odd answers
        )
    chain = primary.with_fallbacks(backups, exceptions_to_handle=FALLBACK_ERRORS).with_config(
        run_name=f"llm.{task}", metadata=meta
    )
    if task in CONVERSATION_TASKS:
        return chain

    # Internal call: one more level (named by the caller's run_name, default `<task>.call`) — see CONVERSATION_TASKS.
    # A RunnableLambda returning a Runnable invokes/streams it with the same input as a child run.
    def _internal(_input: Any) -> Runnable:
        return chain

    async def _ainternal(_input: Any) -> Runnable:
        return chain

    return RunnableLambda(_internal, afunc=_ainternal, name=f"{task}.call")
