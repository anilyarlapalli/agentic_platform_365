"""One LLM seam, one decorator chain, one fixed order.

## What this replaces

The Azure build wraps ``chat.completions.create`` **twice** —
``telemetry.instrument_llm()`` and ``token_budget.install()`` patch it
independently, each with its own ``_wrapped`` sentinel — and ``llm_retry`` adds a
third layer. Which wrapper ends up outermost is decided by the order
``bootstrap.init`` happens to call them in. So the relationship between the
budget check, the retry and the span is an emergent property of import order
rather than a decision anyone made or can point at.

Here the order is a list, applied once, asserted by a test:

    identity → cancellation → budget_reservation → cache → trace → retry →
    dispatch → meter → budget_settlement

## Why that order, specifically

**reservation before cache.** The other way round, a cache stampede bypasses the
ceiling entirely: a thousand concurrent misses all consult a cache that has
nothing yet, and none of them has committed headroom. A hit releases its
reservation immediately.

**cache after budget, before retry.** A cache hit must not consume a retry
budget or emit a dispatch span; it is not a call.

**retry inside one reservation.** Three attempts at one logical call must not
re-authorise three times against a ceiling that had room for one.

**meter after dispatch.** Usage is only knowable from the response. ``call_llm``
in the engine returns ``response.choices[0].message.content`` and discards
``usage`` entirely, which is why the Azure build's first budget implementation
read a string as a dict, caught its own exception, and left the ledger
permanently empty while reporting itself installed and healthy.

## Attribution cannot be forgotten

Every call takes a :class:`RequestContext`. There is no ambient context
variable, because that is precisely the mechanism by which the Azure build loses
attribution: ``token_budget.set_context`` is called on the ingest and eval paths
only, so chat and onboarding spend bills to ``"unknown"`` — and, since the
ContextVar is never reset, a worker that ran an ingest for one domain then bills
a later onboarding step to it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import random
import time
from datetime import UTC, datetime

from platform_core.correctness.cancellation import (
    cancellation_point,
    interruptible_sleep,
)
from platform_core.identity.principal import RequestContext
from platform_core.observability.ledger import ledger
from platform_core.observability.telemetry import (
    bind_request_context,
    pseudonym,
    record_llm,
    start_span,
)
from platform_core.ports.errors import TransientError
from platform_core.ports.ledger import UsageRecord
from platform_core.ports.llm import ChatRequest, ChatResponse, TokenUsage
from platform_core.settings import get_settings

logger = logging.getLogger("platform.observability.llm")

# USD per 1K tokens, (input, output). One table, so one number is quoted
# everywhere — the alternative is a cost estimate in the ledger that disagrees
# with the one in the trace.
MODEL_PRICING: dict[str, tuple[float, float]] = {
    "gpt-4o": (0.0025, 0.01),
    "gpt-4o-mini": (0.00015, 0.0006),
    "gpt-4.1": (0.002, 0.008),
    "gpt-4.1-mini": (0.0004, 0.0016),
    "gpt-4.1-nano": (0.0001, 0.0004),
    "gpt-5": (0.00125, 0.01),
    "gpt-5-mini": (0.00025, 0.002),
    "o3-mini": (0.0011, 0.0044),
    "text-embedding-3-small": (0.00002, 0.0),
    "text-embedding-3-large": (0.00013, 0.0),
}
# An unpriced model must not silently cost zero — that reads as thrift in every
# report. Priced at the most expensive known rate so the error is visible and
# conservative.
UNKNOWN_MODEL_PRICING = (0.0025, 0.01)


def price(model: str, input_tokens: int, output_tokens: int) -> float:
    rates = MODEL_PRICING.get(model)
    if rates is None:
        logger.warning(
            "no pricing for model %r — charging the highest known rate so the gap "
            "is visible rather than free", model,
        )
        rates = UNKNOWN_MODEL_PRICING
    return (input_tokens * rates[0] + output_tokens * rates[1]) / 1000


# ── the chain ────────────────────────────────────────────────────────────
#
# Named and ordered here so the order is a value that can be asserted, rather
# than an emergent consequence of which module was imported first.
CHAIN_ORDER: tuple[str, ...] = (
    "identity",
    "cancellation",
    "budget_reservation",
    "cache",
    "trace",
    "retry",
    "dispatch",
    "meter",
    "budget_settlement",
)


class InstrumentedLLM:
    """Wraps a raw client with the full chain. The only way a call is made."""

    def __init__(self, raw_client, *, cache=None) -> None:
        self._raw = raw_client
        self._cache = cache

    # ── public surface ────────────────────────────────────────────────────

    def chat(self, ctx: RequestContext, request: ChatRequest) -> ChatResponse:
        return self._call(ctx, request, kind="chat")

    def embed(self, ctx: RequestContext, texts: list[str], *,
              model: str | None = None) -> list[list[float]]:
        """Embedding is metered exactly like chat.

        This is where ingestion actually spends: a corpus rebuild is thousands
        of calls against one for a chat turn. A budget that meters chat and not
        embedding bounds the cheap path and leaves the expensive one open.
        """
        settings = get_settings()
        model = model or settings.embedding_model
        _identity(ctx)
        cancellation_point(ctx)

        estimated = sum(self.token_count(t, model=model) for t in texts)
        reservation = ledger.reserve(
            ctx,
            model=model,
            estimated_tokens=estimated,
            estimated_cost_usd=price(model, estimated, 0),
        )

        started = time.monotonic()
        try:
            with start_span(
                "gen_ai.embed",
                attributes=_span_attributes(ctx, model=model, operation="embeddings"),
            ) as span:
                bind_request_context(ctx)
                cancellation_point(ctx)
                response = self._raw.embeddings.create(model=model, input=texts)
                usage = getattr(response, "usage", None)
                input_tokens = int(getattr(usage, "prompt_tokens", 0) or estimated)
                span.set_attribute("gen_ai.usage.input_tokens", input_tokens)
        except Exception:
            record_llm(
                provider=_provider_name(), operation="embeddings", model=model,
                outcome="error", duration_ms=(time.monotonic() - started) * 1000,
            )
            raise

        self._record(
            ctx, model=model, input_tokens=input_tokens, output_tokens=0,
            cache_hit=False, usage_reported=usage is not None,
            reservation=reservation,
        )
        record_llm(
            provider=_provider_name(), operation="embeddings", model=model,
            outcome="success", duration_ms=(time.monotonic() - started) * 1000,
            input_tokens=input_tokens,
        )
        return [item.embedding for item in response.data]

    def token_count(self, text: str, *, model: str | None = None) -> int:
        try:
            import tiktoken

            enc = tiktoken.encoding_for_model(model or "gpt-4o")
        except Exception:
            try:
                import tiktoken

                enc = tiktoken.get_encoding("cl100k_base")
            except Exception:
                # Character approximation. Wrong, but bounded and never a crash;
                # chunk sizing feeds retrieval quality, so it is worth saying
                # that this path is degraded rather than silently accepting it.
                logger.debug("tiktoken unavailable — using a character approximation")
                return max(1, len(text) // 4)
        return len(enc.encode(text))

    # ── the chain, in order ───────────────────────────────────────────────

    def _call(self, ctx: RequestContext, request: ChatRequest, *, kind: str) -> ChatResponse:
        started = time.monotonic()

        # 1. identity — refuse a call that could not be attributed.
        _identity(ctx)
        cancellation_point(ctx)

        # 2. budget — reserve once, before anything else happens. The output
        # ceiling is included because concurrent prompt-only checks can all fit
        # and then collectively overspend on generated tokens.
        estimated_input = sum(
            self.token_count(str(m.get("content", "")), model=request.model)
            for m in request.messages
        )
        estimated_output = (
            request.max_tokens
            if request.max_tokens is not None
            else get_settings().llm_default_max_output_tokens
        )
        reservation = ledger.reserve(
            ctx,
            model=request.model,
            estimated_tokens=estimated_input + estimated_output,
            estimated_cost_usd=price(request.model, estimated_input, estimated_output),
        )

        # 3. cache — after authorisation, so a stampede cannot bypass the cap.
        cache_key = _cache_key(ctx, request) if request.cacheable else None
        if cache_key and self._cache is not None:
            hit = self._cache.get(ctx, cache_key)
            if hit is not None:
                ledger.release(reservation, reason="cache hit; no provider dispatch")
                # Recorded at zero cost against a real request, so cache
                # effectiveness is measurable rather than inferred from a drop
                # in spend that could equally be a drop in traffic.
                self._record(
                    ctx, model=request.model, input_tokens=0, output_tokens=0,
                    cache_hit=True, usage_reported=True,
                )
                record_llm(
                    provider="cache", operation=kind, model=request.model,
                    outcome="cache_hit", duration_ms=(time.monotonic() - started) * 1000,
                )
                return ChatResponse(**{**hit, "cache_hit": True})

        # 4-5. retry around dispatch — inside the budget check, so N attempts at
        # one logical call are authorised once.
        provider_started = time.monotonic()
        try:
            with start_span(
                f"gen_ai.{kind}",
                attributes=_span_attributes(ctx, model=request.model, operation=kind),
            ) as span:
                bind_request_context(ctx)
                cancellation_point(ctx)
                response, attempts = self._dispatch_with_retry(ctx, request)
                usage = getattr(response, "usage", None)
                input_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
                output_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
                content = response.choices[0].message.content or ""
                cost = price(request.model, input_tokens, output_tokens)
                span.set_attributes(
                    {
                        "gen_ai.usage.input_tokens": input_tokens,
                        "gen_ai.usage.output_tokens": output_tokens,
                        "platform.cost_usd": cost,
                        "platform.attempts": attempts,
                    }
                )
        except Exception:
            record_llm(
                provider=_provider_name(), operation=kind, model=request.model,
                outcome="error", duration_ms=(time.monotonic() - provider_started) * 1000,
            )
            raise

        # 6. meter — from the response, which is the only trustworthy source.
        self._record(
            ctx, model=request.model, input_tokens=input_tokens,
            output_tokens=output_tokens, cache_hit=False,
            usage_reported=usage is not None, cost=cost,
            reservation=reservation,
        )

        raw_tool_calls = getattr(response.choices[0].message, "tool_calls", None) or []
        tool_calls = [
            call.model_dump(mode="json") if hasattr(call, "model_dump") else dict(call)
            for call in raw_tool_calls
        ]
        result = ChatResponse(
            content=content,
            model=request.model,
            usage=TokenUsage(input_tokens, output_tokens, reported=usage is not None),
            cost_usd=cost,
            finish_reason=response.choices[0].finish_reason or "stop",
            tool_calls=tool_calls,
            cache_hit=False,
            attempts=attempts,
            latency_ms=(time.monotonic() - started) * 1000,
        )

        if cache_key and self._cache is not None:
            self._cache.put(ctx, cache_key, result)

        record_llm(
            provider=_provider_name(), operation=kind, model=request.model,
            outcome="success", duration_ms=(time.monotonic() - provider_started) * 1000,
            input_tokens=input_tokens, output_tokens=output_tokens, cost_usd=cost,
        )
        return result

    def _dispatch_with_retry(self, ctx: RequestContext, request: ChatRequest):
        """Retry transient failures with jittered backoff.

        Jitter is not a refinement. Without it, N callers throttled at the same
        instant retry at the same instant, and the herd reproduces the throttle
        it is backing off from.
        """
        last: Exception | None = None
        for attempt in range(1, 4):
            cancellation_point(ctx)
            try:
                kwargs = {
                    "model": request.model,
                    "messages": request.messages,
                }
                if request.temperature is not None:
                    kwargs["temperature"] = request.temperature
                if request.max_tokens is not None:
                    kwargs["max_tokens"] = request.max_tokens
                if request.tools:
                    kwargs["tools"] = request.tools
                if request.response_format:
                    kwargs["response_format"] = request.response_format
                return self._raw.chat.completions.create(**kwargs), attempt
            except Exception as exc:
                status = getattr(exc, "status_code", None)
                transient = status == 429 or (status is not None and status >= 500)
                if not transient or attempt == 3:
                    if transient:
                        raise TransientError(
                            f"LLM call failed after {attempt} attempts", cause=exc
                        ) from exc
                    raise
                last = exc
                # Honour the server's own backoff when it supplies one: guessing
                # against a service that has told you how long to wait is how a
                # throttle becomes a thundering herd.
                retry_after = getattr(exc, "retry_after", None)
                delay = float(retry_after) if retry_after else (2 ** (attempt - 1))
                delay += random.uniform(0, delay * 0.25)
                logger.warning(
                    "LLM attempt %d failed (%s) — retrying in %.1fs",
                    attempt, type(exc).__name__, delay,
                )
                interruptible_sleep(ctx, delay)
        raise TransientError("LLM call exhausted retries", cause=last)

    def _record(self, ctx: RequestContext, *, model: str, input_tokens: int,
                output_tokens: int, cache_hit: bool, usage_reported: bool,
                cost: float | None = None, reservation=None) -> None:
        record = UsageRecord(
            tenant_id=ctx.tenant.id,
            principal_id=ctx.principal.id,
            run_id=ctx.run_id,
            workload=ctx.labels.get("workload", "unknown"),
            task=ctx.labels.get("task", "unknown"),
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost if cost is not None else price(model, input_tokens, output_tokens),
            at=datetime.now(UTC),
            cache_hit=cache_hit,
            usage_reported=usage_reported,
        )
        if reservation is None:
            ledger.record(record)
        else:
            ledger.settle(reservation, record)


# ── chain steps that are not methods ─────────────────────────────────────


class UnattributableCall(RuntimeError):
    """A call that could not be charged to anyone. Refused rather than made."""


def _identity(ctx: RequestContext) -> None:
    """Refuse a call with no tenant. The first link in the chain.

    Cheap, and it is the difference between this platform and the one it was
    built from: there is no path here that produces spend nobody owns, because a
    call without a tenant does not happen.
    """
    if ctx is None or ctx.principal is None or ctx.tenant is None:
        raise UnattributableCall(
            "an LLM call requires a RequestContext carrying a tenant; refusing "
            "rather than spending against an unknown owner"
        )


def _cache_key(ctx: RequestContext, request: ChatRequest) -> str:
    """Tenant-scoped cache key.

    The tenant is *in the key*, not merely checked on read. A shared key space
    would let one tenant's answer be served to another — the worst possible
    cache bug, because it is a correct-looking wrong answer that no error
    surfaces.
    """
    payload = json.dumps(
        {
            "tenant": str(ctx.tenant.id),
            "model": request.model,
            "messages": request.messages,
            "temperature": request.temperature,
            "tools": request.tools,
        },
        sort_keys=True, default=str,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _provider_name() -> str:
    provider = get_settings().llm_provider
    return "openai" if provider == "cassette" else provider


def _span_attributes(ctx: RequestContext, *, model: str, operation: str) -> dict[str, object]:
    """GenAI conventions plus privacy-safe platform causality; never content."""
    return {
        "gen_ai.provider.name": _provider_name(),
        "gen_ai.operation.name": operation,
        "gen_ai.request.model": model,
        "platform.tenant.id": pseudonym(ctx.tenant.id),
        "platform.run.id": str(ctx.run_id) if ctx.run_id else "",
        "platform.task": ctx.labels.get("task", ""),
        "platform.release": get_settings().release,
    }


def build_client(*, cache=None) -> InstrumentedLLM:
    """The only constructor. Raw clients are not exposed.

    A raw client handed to application code is a path around the chain — no
    budget, no ledger row, no span — so there is exactly one way to get one and
    it is already wrapped.
    """
    settings = get_settings()
    from openai import OpenAI

    if settings.llm_provider == "ollama":
        raw = OpenAI(base_url=settings.ollama_base_url, api_key="ollama")
    else:
        raw = OpenAI(
            api_key=settings.openai_api_key.get_secret_value()
            if settings.openai_api_key else "",
            base_url=settings.openai_base_url,
        )
    return InstrumentedLLM(raw, cache=cache)
