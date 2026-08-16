"""The model call. One seam, and everything that must happen around it.

The Azure build wraps ``chat.completions.create`` **twice** —
``telemetry.instrument_llm()`` and ``token_budget.install()`` each patch it
independently, each with its own ``_wrapped`` sentinel, and ``llm_retry`` adds a
third layer. Which wrapper ends up outermost depends on the order
``bootstrap.init`` happens to run them in, so the relationship between the
budget check, the retry and the span is an emergent property of import order
rather than a decision anyone made.

Here there is one interface and one decorator chain, applied in a fixed order
that is asserted by a test:

    identity → cancellation → budget_reservation → cache → trace → retry →
    dispatch → meter → budget_settlement

The order is not arbitrary:

* **reservation before cache** prevents a cache stampede from collectively
  bypassing a ceiling; a cache hit releases the reservation and records zero.
* **retry inside one reservation** so a retried call is not re-authorised —
  otherwise three attempts of one logical call can each pass a ceiling that only
  had room for one.
* **meter after dispatch** because usage is only knowable from the response, and
  the response is the only trustworthy source. ``call_llm`` in the engine
  returns ``response.choices[0].message.content`` and discards ``usage``
  entirely, which is why the Azure build's first budget implementation read a
  string as a dict, logged the exception, and left the ledger permanently empty
  while reporting itself installed and healthy.

Every method takes a :class:`RequestContext`. There is no ambient context
variable, because that is the mechanism by which the Azure build lost
attribution: ``token_budget.set_context`` is called on the ingest and eval paths
only, so chat and onboarding spend bills to the string ``"unknown"`` — and,
since the ContextVar is never reset, a worker that ran an ingest for one domain
then bills a later onboarding step to it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from platform_core.identity.principal import RequestContext


@dataclass(frozen=True, slots=True)
class TokenUsage:
    input_tokens: int
    output_tokens: int
    # None means the provider did not report usage. Distinct from zero, which
    # would silently under-count the cost view — the Azure build emits a
    # dedicated `tokens.missing_metadata` counter for exactly this reason.
    reported: bool = True

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True, slots=True)
class ChatRequest:
    messages: list[dict[str, Any]]
    model: str
    temperature: float | None = None
    max_tokens: int | None = None
    tools: list[dict[str, Any]] | None = None
    response_format: dict[str, Any] | None = None
    # Set by the caller when a call is safe to serve from cache. Off by default:
    # a cache that decides for itself will eventually serve a stale answer to a
    # question whose correct answer changed.
    cacheable: bool = False


@dataclass(frozen=True, slots=True)
class ChatResponse:
    content: str
    model: str
    usage: TokenUsage
    cost_usd: float
    finish_reason: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    # Provenance, so a result can be traced to what produced it.
    cache_hit: bool = False
    attempts: int = 1
    latency_ms: float = 0.0
    cassette_key: str | None = None


@runtime_checkable
class LLMClient(Protocol):
    def chat(self, ctx: RequestContext, request: ChatRequest) -> ChatResponse:
        """Dispatch a chat completion.

        Raises :class:`BudgetExceededError` **before** dispatching when the
        tenant is over a ceiling — the ceiling has to stop the spend, not report
        it. Raises :class:`TransientError` for throttles and 5xx so the caller's
        requeue logic can branch on the type.
        """
        ...

    def embed(self, ctx: RequestContext, texts: list[str], *,
              model: str | None = None) -> list[list[float]]:
        """Embed texts. Metered and charged like any other call.

        Embedding is where an ingestion actually spends: a corpus rebuild is
        thousands of calls, versus one for a chat turn. A budget that meters
        chat and not embedding bounds the cheap path and leaves the expensive
        one open.
        """
        ...

    def token_count(self, text: str, *, model: str | None = None) -> int:
        """Count tokens locally, without a network call.

        Needed for chunk sizing, which feeds retrieval quality directly, and for
        pre-flight budget estimates.
        """
        ...
