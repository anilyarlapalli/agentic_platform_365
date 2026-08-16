"""Spend: recorded per tenant, checked before dispatch, never after.

Two operations that must not be collapsed. :meth:`Ledger.check` runs *before* a
call and can refuse it; :meth:`Ledger.record` runs after and cannot. A design
that only records is a receipt system — the Azure build's ``llm_usage`` table
computed cost correctly for months while nothing was capable of stopping a call.

The tenant is the ceiling's subject because it is the boundary that owns the
budget. Per-model and per-workload dimensions are recorded for attribution but
are not enforcement scopes: a ceiling per model lets one workload exhaust
another's headroom inside the same tenant.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from platform_core.identity.principal import RequestContext


@dataclass(frozen=True, slots=True)
class UsageRecord:
    tenant_id: uuid.UUID
    principal_id: uuid.UUID
    run_id: uuid.UUID | None
    workload: str
    task: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    at: datetime
    cache_hit: bool = False
    # False when the provider returned no usage block. Surfaced rather than
    # coerced to zero, because silent under-counting is indistinguishable from
    # thrift right up until the invoice.
    usage_reported: bool = True


@dataclass(frozen=True, slots=True)
class BudgetStatus:
    tenant_id: uuid.UUID
    tokens_today: int
    daily_token_cap: int
    cost_this_month_usd: float
    monthly_cost_cap_usd: float
    reserved_tokens: int = 0
    reserved_cost_usd: float = 0.0
    # True when the totals came from cache rather than a fresh read. Exposed so
    # an operator can tell "under budget" from "under budget as of 5s ago".
    from_cache: bool = False

    @property
    def headroom_tokens(self) -> int:
        return max(0, self.daily_token_cap - self.tokens_today - self.reserved_tokens)

    @property
    def exhausted(self) -> bool:
        return (
            self.tokens_today + self.reserved_tokens >= self.daily_token_cap
            or self.cost_this_month_usd + self.reserved_cost_usd
            >= self.monthly_cost_cap_usd
        )


@runtime_checkable
class Ledger(Protocol):
    def check(self, ctx: RequestContext, *, estimated_tokens: int = 0) -> BudgetStatus:
        """Authorise spend. Raises :class:`BudgetExceededError` when over.

        ``estimated_tokens`` lets a large call be refused *before* it starts
        rather than after it lands: a 200k-token ingestion batch that would blow
        the ceiling should not be dispatched merely because the ceiling had room
        for one more small call.
        """
        ...

    def record(self, record: UsageRecord) -> None:
        """Persist actual usage. Must never raise into the caller.

        Losing a ledger row must not lose the answer that was already paid for
        and produced. Failures here are logged loudly and counted, because a
        ledger that silently stops recording is a budget that silently stops
        binding.
        """
        ...

    def status(self, ctx: RequestContext) -> BudgetStatus:
        ...

    def unattributed_spend(self, *, since: datetime | None = None) -> int:
        """How many usage rows carry no resolvable tenant. **Must be zero.**

        The platform's cost-attribution property is asserted against this in
        ``tests/properties/test_cost_attribution.py``. It exists as a first-class
        query rather than an ad-hoc one because "is all spend attributable" is a
        question that should be answerable at any moment, not reconstructed
        during an investigation.
        """
        ...

    def set_caps(
        self, tenant_id: uuid.UUID, *, daily_tokens: int | None, monthly_cost_usd: float | None
    ) -> None:
        ...
