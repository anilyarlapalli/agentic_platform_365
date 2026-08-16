"""Spend: authorised before the call, recorded after, always attributed.

## The two operations are not symmetrical

:meth:`check` runs **before** dispatch and can refuse. :meth:`record` runs after
and cannot. A design that only records is a receipt system — the Azure build's
``llm_usage`` table computed cost correctly for months while nothing was capable
of stopping a call, and its first budget implementation wrapped the wrong
function, read a string as a dict, caught its own exception, and left the ledger
permanently empty while reporting itself installed and healthy.

## The per-task policy

Decided 2026-08-12. The question is narrower than "how strict is the budget":
over-budget **always** refuses. This governs only the *unknown* state, when the
ledger cannot be read at all.

What makes that state dangerous is that ``check`` and ``record`` share one
Postgres. The outage that hides the ceiling also swallows the writes, so failing
open produces spend that is uncapped **and unrecorded** — the ledger ends up with
a hole exactly the size of the outage, unreconstructable afterwards.

Blast radius differs by orders of magnitude, so the policy does too:

===============  ==============  ==========================================
task             on unknown      why
===============  ==============  ==========================================
chat, query      **fail open**   one call, cents, a user is waiting
ingest,          **fail closed** thousands of calls, nobody waiting — the
onboard, eval                    "month of budget in an afternoon" case
===============  ==============  ==========================================

Revisit if a read path ever moves off Postgres: the availability argument that
makes fail-open correct in the Azure build would start applying here too.

## Attribution is structural

Every method takes a :class:`RequestContext`, and ``llm_usage.tenant_id`` is
``NOT NULL``. There is no ``"unknown"`` to fall back to, so an unattributable
charge cannot be written at all — which is what lets
``tests/properties/test_cost_attribution.py`` assert that unattributed spend is
exactly zero and have the assertion mean something.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.orm import Session

from platform_core.db.engine import tenant_session
from platform_core.identity.principal import RequestContext
from platform_core.observability.telemetry import (
    record_budget_decision,
    record_budget_ledger_write,
)
from platform_core.ports.errors import BudgetExceededError
from platform_core.ports.ledger import BudgetStatus, UsageRecord
from platform_core.settings import get_settings

logger = logging.getLogger("platform.observability.ledger")

# Tasks where a waiting human outweighs the cost of one unmetered call.
INTERACTIVE_TASKS: frozenset[str] = frozenset({"chat", "query"})


def fails_open(task: str) -> bool:
    """Whether an unreadable ledger should permit this task's call.

    Unknown tasks fail **closed**. A task nobody classified is more likely to be
    a new background job than a new interactive path, and the asymmetry of being
    wrong points the same way: refusing an interactive call costs one answer,
    permitting a background one can cost a month of budget.
    """
    return task in INTERACTIVE_TASKS


@dataclass(frozen=True, slots=True)
class _Window:
    at: float
    tokens_today: int
    cost_this_month: float


@dataclass(frozen=True, slots=True)
class BudgetReservation:
    id: uuid.UUID | None
    tenant_id: uuid.UUID
    estimated_tokens: int
    estimated_cost_usd: float
    # None means the configured interactive fail-open policy admitted the call
    # while Postgres was unavailable. It remains explicitly unmetered.
    metered: bool = True


class PostgresLedger:
    """Implements :class:`platform_core.ports.ledger.Ledger`."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._cache: dict[uuid.UUID, _Window] = {}

    # ── authorisation ─────────────────────────────────────────────────────

    def check(self, ctx: RequestContext, *, estimated_tokens: int = 0) -> BudgetStatus:
        settings = get_settings()
        task = ctx.labels.get("task", "")

        try:
            status = self.status(ctx)
        except Exception:
            open_it = fails_open(task) and not settings.budget_fail_closed
            logger.error(
                "budget ledger unreadable for tenant %s (task=%s) — %s",
                ctx.tenant.slug, task or "<unset>",
                "allowing the call" if open_it else "refusing the call",
                exc_info=True,
            )
            if open_it:
                # Recorded as a distinct, countable event. A period of
                # unattributed spend must be visible afterwards even though the
                # row could not be written at the time.
                _count_unmetered(ctx, task)
                record_budget_decision("check", "unmetered_fail_open")
                return BudgetStatus(
                    tenant_id=ctx.tenant.id, tokens_today=0,
                    daily_token_cap=settings.daily_token_cap,
                    cost_this_month_usd=0.0,
                    monthly_cost_cap_usd=settings.monthly_cost_cap_usd,
                    from_cache=False,
                )
            record_budget_decision("check", "unavailable_fail_closed")
            raise BudgetExceededError(
                f"budget ledger unreadable for {ctx.tenant.slug!r} and task {task!r} "
                f"fails closed"
            ) from None

        # Refuse *before* spending, including for a call whose size alone would
        # breach. A 200k-token batch should not be dispatched merely because the
        # ceiling had room for one more small call.
        projected = status.tokens_today + status.reserved_tokens + max(0, estimated_tokens)
        if status.exhausted or projected >= status.daily_token_cap:
            record_budget_decision("check", "refused")
            raise BudgetExceededError(
                f"{ctx.tenant.slug}: {status.tokens_today:,} tokens today"
                f"{f' + {estimated_tokens:,} estimated' if estimated_tokens else ''} "
                f"against a {status.daily_token_cap:,} daily cap; "
                f"${status.cost_this_month_usd:.2f} this month against "
                f"${status.monthly_cost_cap_usd:.2f}"
            )
        record_budget_decision("check", "allowed")
        return status

    def reserve(
        self,
        ctx: RequestContext,
        *,
        model: str,
        estimated_tokens: int,
        estimated_cost_usd: float,
    ) -> BudgetReservation:
        """Atomically hold tenant headroom before dispatching an external call."""
        if estimated_tokens < 0 or estimated_cost_usd < 0:
            raise ValueError("budget estimates cannot be negative")
        settings = get_settings()
        task = ctx.labels.get("task", "")
        try:
            with tenant_session(ctx.tenant) as session:
                session.execute(
                    text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                    {"key": f"budget:{ctx.tenant.id}"},
                )
                session.execute(
                    text(
                        "UPDATE budget_reservation SET status = 'expired', "
                        "release_reason = 'reservation TTL elapsed' "
                        "WHERE status = 'reserved' AND expires_at <= now()"
                    )
                )
                caps = session.execute(
                    text(
                        "SELECT daily_token_cap, monthly_cost_cap_usd "
                        "FROM tenant WHERE id = :tenant"
                    ),
                    {"tenant": ctx.tenant.id},
                ).one_or_none()
                totals = session.execute(
                    text(
                        "SELECT "
                        "  coalesce((SELECT sum(total_tokens) FROM llm_usage "
                        "    WHERE at >= date_trunc('day', now())), 0) AS tokens_today, "
                        "  coalesce((SELECT sum(cost_usd) FROM llm_usage "
                        "    WHERE at >= date_trunc('month', now())), 0) AS cost_month, "
                        "  coalesce((SELECT sum(estimated_tokens) FROM budget_reservation "
                        "    WHERE status = 'reserved' AND expires_at > now()), 0) "
                        "    AS reserved_tokens, "
                        "  coalesce((SELECT sum(estimated_cost_usd) FROM budget_reservation "
                        "    WHERE status = 'reserved' AND expires_at > now()), 0) "
                        "    AS reserved_cost"
                    )
                ).one()
                daily_cap = int(
                    caps.daily_token_cap
                    if caps is not None and caps.daily_token_cap is not None
                    else settings.daily_token_cap
                )
                monthly_cap = float(
                    caps.monthly_cost_cap_usd
                    if caps is not None and caps.monthly_cost_cap_usd is not None
                    else settings.monthly_cost_cap_usd
                )
                projected_tokens = (
                    int(totals.tokens_today)
                    + int(totals.reserved_tokens)
                    + estimated_tokens
                )
                projected_cost = (
                    float(totals.cost_month)
                    + float(totals.reserved_cost)
                    + estimated_cost_usd
                )
                if projected_tokens >= daily_cap or projected_cost >= monthly_cap:
                    record_budget_decision("reserve", "refused")
                    raise BudgetExceededError(
                        f"{ctx.tenant.slug}: reservation would commit "
                        f"{projected_tokens:,}/{daily_cap:,} daily tokens and "
                        f"${projected_cost:.2f}/${monthly_cap:.2f} monthly cost"
                    )
                reservation_id = session.execute(
                    text(
                        "INSERT INTO budget_reservation "
                        "(tenant_id, principal_id, request_id, task, model, "
                        " estimated_tokens, estimated_cost_usd, expires_at) "
                        "VALUES (:tenant, :principal, :request, :task, :model, :tokens, "
                        " :cost, now() + :ttl) RETURNING id"
                    ),
                    {
                        "tenant": ctx.tenant.id,
                        "principal": ctx.principal.id,
                        "request": ctx.request_id,
                        "task": task or "unknown",
                        "model": model,
                        "tokens": estimated_tokens,
                        "cost": Decimal(str(estimated_cost_usd)),
                        "ttl": timedelta(seconds=settings.budget_reservation_ttl_seconds),
                    },
                ).scalar_one()
        except BudgetExceededError:
            raise
        except Exception:
            open_it = fails_open(task) and not settings.budget_fail_closed
            logger.error(
                "budget reservation unavailable for tenant %s (task=%s) — %s",
                ctx.tenant.slug,
                task or "<unset>",
                "allowing the call" if open_it else "refusing the call",
                exc_info=True,
            )
            if not open_it:
                record_budget_decision("reserve", "unavailable_fail_closed")
                raise BudgetExceededError(
                    f"budget reservation unavailable for {ctx.tenant.slug!r} and "
                    f"task {task!r} fails closed"
                ) from None
            _count_unmetered(ctx, task)
            record_budget_decision("reserve", "unmetered_fail_open")
            return BudgetReservation(
                id=None,
                tenant_id=ctx.tenant.id,
                estimated_tokens=estimated_tokens,
                estimated_cost_usd=estimated_cost_usd,
                metered=False,
            )

        record_budget_decision("reserve", "allowed")
        record_budget_ledger_write("reservation", "succeeded")
        return BudgetReservation(
            id=reservation_id,
            tenant_id=ctx.tenant.id,
            estimated_tokens=estimated_tokens,
            estimated_cost_usd=estimated_cost_usd,
        )

    def release(self, reservation: BudgetReservation, *, reason: str) -> None:
        if reservation.id is None:
            return
        try:
            with tenant_session(reservation.tenant_id) as session:
                session.execute(
                    text(
                        "UPDATE budget_reservation SET status = 'released', "
                        "release_reason = :reason, settled_at = now() "
                        "WHERE id = :id AND status = 'reserved'"
                    ),
                    {"id": reservation.id, "reason": reason[:200]},
                )
            record_budget_ledger_write("release", "succeeded")
        except Exception:
            # Retaining the reservation until TTL is safer than silently
            # reopening headroom after an uncertain provider outcome.
            logger.error("could not release budget reservation %s", reservation.id, exc_info=True)
            record_budget_ledger_write("release", "failed")

    def settle(self, reservation: BudgetReservation, record: UsageRecord) -> None:
        """Convert one reservation into one usage row in a single transaction."""
        if reservation.id is None:
            self.record(record)
            return
        try:
            with tenant_session(record.tenant_id) as session:
                session.execute(
                    text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                    {"key": f"budget:{record.tenant_id}"},
                )
                transitioned = session.execute(
                    text(
                        "UPDATE budget_reservation SET status = 'settled', "
                        "actual_tokens = :tokens, actual_cost_usd = :cost, settled_at = now() "
                        "WHERE id = :id AND status IN ('reserved','expired') RETURNING id"
                    ),
                    {
                        "id": reservation.id,
                        "tokens": record.input_tokens + record.output_tokens,
                        "cost": Decimal(str(record.cost_usd)),
                    },
                ).scalar_one_or_none()
                if transitioned is None:
                    existing = session.execute(
                        text("SELECT status FROM budget_reservation WHERE id = :id"),
                        {"id": reservation.id},
                    ).scalar_one_or_none()
                    if existing == "settled":
                        record_budget_ledger_write("settlement", "idempotent")
                        return
                    raise RuntimeError(
                        f"reservation {reservation.id} cannot settle from {existing!r}"
                    )
                _insert_usage(session, record)
        except Exception:
            logger.error(
                "could not settle reservation %s for %d tokens — spend occurred and "
                "is NOT in the ledger",
                reservation.id,
                record.input_tokens + record.output_tokens,
                exc_info=True,
            )
            record_budget_ledger_write("settlement", "failed")
            return
        record_budget_ledger_write("settlement", "succeeded")
        self._add_to_cache(record)

    # ── recording ─────────────────────────────────────────────────────────

    def record(self, record: UsageRecord) -> None:
        """Persist a charge. Never raises into the caller.

        Losing a ledger row must not lose the answer that was already paid for
        and produced. Failures are logged at ERROR and counted, because a ledger
        that silently stops recording is a budget that silently stops binding.
        """
        try:
            with tenant_session(record.tenant_id) as s:
                _insert_usage(s, record)
        except Exception:
            logger.error(
                "could not record %d tokens for tenant %s — spend has occurred and is "
                "NOT in the ledger", record.input_tokens + record.output_tokens,
                record.tenant_id, exc_info=True,
            )
            record_budget_ledger_write("usage", "failed")
            return

        record_budget_ledger_write("usage", "succeeded")
        # Keep the cached window consistent with what was just written, so a
        # burst of calls inside one cache period still converges on the cap
        # rather than all reading the same stale total.
        self._add_to_cache(record)

    def _add_to_cache(self, record: UsageRecord) -> None:
        with self._lock:
            hit = self._cache.get(record.tenant_id)
            if hit:
                self._cache[record.tenant_id] = _Window(
                    at=hit.at,
                    tokens_today=hit.tokens_today + record.input_tokens + record.output_tokens,
                    cost_this_month=hit.cost_this_month + record.cost_usd,
                )

    # ── reporting ─────────────────────────────────────────────────────────

    def status(self, ctx: RequestContext) -> BudgetStatus:
        settings = get_settings()
        now = time.time()

        with self._lock:
            cached = self._cache.get(ctx.tenant.id)
        from_cache = bool(cached and now - cached.at < settings.budget_cache_seconds)

        if from_cache:
            tokens, cost = cached.tokens_today, cached.cost_this_month
        else:
            with tenant_session(ctx.tenant) as s:
                row = s.execute(
                    text(
                        "SELECT "
                        "  coalesce(sum(total_tokens) FILTER "
                        "    (WHERE at >= date_trunc('day', now())), 0) AS tokens_today, "
                        "  coalesce(sum(cost_usd) FILTER "
                        "    (WHERE at >= date_trunc('month', now())), 0) AS cost_month "
                        "FROM llm_usage WHERE tenant_id = :t "
                        "  AND at >= date_trunc('month', now())"
                    ),
                    {"t": ctx.tenant.id},
                ).one()
            tokens, cost = int(row.tokens_today), float(row.cost_month)
            with self._lock:
                self._cache[ctx.tenant.id] = _Window(now, tokens, cost)

        with tenant_session(ctx.tenant) as session:
            reserved = session.execute(
                text(
                    "SELECT coalesce(sum(estimated_tokens), 0) AS tokens, "
                    "coalesce(sum(estimated_cost_usd), 0) AS cost "
                    "FROM budget_reservation "
                    "WHERE status = 'reserved' AND expires_at > now()"
                )
            ).one()

        caps = self._caps(ctx)
        return BudgetStatus(
            tenant_id=ctx.tenant.id,
            tokens_today=tokens,
            daily_token_cap=caps[0],
            cost_this_month_usd=cost,
            monthly_cost_cap_usd=caps[1],
            reserved_tokens=int(reserved.tokens),
            reserved_cost_usd=float(reserved.cost),
            from_cache=from_cache,
        )

    def _caps(self, ctx: RequestContext) -> tuple[int, float]:
        settings = get_settings()
        with tenant_session(ctx.tenant) as s:
            row = s.execute(
                text(
                    "SELECT daily_token_cap, monthly_cost_cap_usd FROM tenant WHERE id = :t"
                ),
                {"t": ctx.tenant.id},
            ).one_or_none()
        if row is None:
            return settings.daily_token_cap, settings.monthly_cost_cap_usd
        return (
            int(
                row.daily_token_cap
                if row.daily_token_cap is not None
                else settings.daily_token_cap
            ),
            float(
                row.monthly_cost_cap_usd
                if row.monthly_cost_cap_usd is not None
                else settings.monthly_cost_cap_usd
            ),
        )

    def unattributed_spend(self, *, since: datetime | None = None) -> int:
        """Usage rows with no resolvable tenant. Structurally always zero.

        ``llm_usage.tenant_id`` is ``NOT NULL`` with a foreign key, so this
        cannot be non-zero without the schema having changed. It exists as a
        first-class query because "is all spend attributable" should be
        answerable at any moment rather than reconstructed during an incident —
        and because the equivalent number in the Azure build is not zero.
        """
        # The owner role: this is a platform-wide operator query, deliberately
        # not something the request path or the relay can run. In production it
        # is invoked from an admin CLI holding that credential, which is why it
        # is not reachable from any HTTP route.
        from platform_core.db.engine import owner_session

        with owner_session() as s:
            return int(
                s.execute(
                    text(
                        "SELECT count(*) FROM llm_usage u "
                        "LEFT JOIN tenant t ON t.id = u.tenant_id "
                        "WHERE t.id IS NULL "
                        "  AND (CAST(:since AS timestamptz) IS NULL OR u.at >= :since)"
                    ),
                    {"since": since},
                ).scalar_one()
            )

    def set_caps(
        self, tenant_id: uuid.UUID, *, daily_tokens: int | None,
        monthly_cost_usd: float | None,
    ) -> None:
        from platform_core.db.engine import owner_session

        # Through the owner: `tenant` is deliberately not writable by the app
        # role, so a compromised app credential cannot raise its own ceiling.
        with owner_session() as s:
            s.execute(
                text(
                    "UPDATE tenant SET daily_token_cap = coalesce(:d, daily_token_cap), "
                    "  monthly_cost_cap_usd = coalesce(:m, monthly_cost_cap_usd) "
                    "WHERE id = :t"
                ),
                {"d": daily_tokens, "m": monthly_cost_usd, "t": tenant_id},
            )
        with self._lock:
            self._cache.pop(tenant_id, None)

    def invalidate(self, tenant_id: uuid.UUID | None = None) -> None:
        with self._lock:
            if tenant_id is None:
                self._cache.clear()
            else:
                self._cache.pop(tenant_id, None)


def _insert_usage(session: Session, record: UsageRecord) -> None:
    session.execute(
        text(
            "INSERT INTO llm_usage (tenant_id, principal_id, run_id, workload, "
            "  task, model, input_tokens, output_tokens, cost_usd, cache_hit, "
            "  usage_reported, release) "
            "VALUES (:t, :p, :r, :w, :task, :model, :in, :out, :cost, :hit, "
            "  :reported, :rel)"
        ),
        {
            "t": record.tenant_id,
            "p": record.principal_id,
            "r": record.run_id,
            "w": record.workload,
            "task": record.task,
            "model": record.model,
            "in": record.input_tokens,
            "out": record.output_tokens,
            "cost": Decimal(str(record.cost_usd)),
            "hit": record.cache_hit,
            "reported": record.usage_reported,
            "rel": get_settings().release,
        },
    )


_unmetered_calls: dict[tuple[str, str], int] = {}
_unmetered_lock = threading.Lock()


def _count_unmetered(ctx: RequestContext, task: str) -> None:
    """Count calls permitted while the ledger was unreadable.

    Fail-open is a deliberate choice, not a free one. The spend it permits is
    invisible in the ledger by definition, so it has to be visible somewhere —
    otherwise "we failed open for six minutes" is unrecoverable after the fact.
    """
    with _unmetered_lock:
        key = (ctx.tenant.slug, task)
        _unmetered_calls[key] = _unmetered_calls.get(key, 0) + 1


def unmetered_call_count() -> dict[tuple[str, str], int]:
    """Calls this process permitted without metering. Surfaced on health."""
    with _unmetered_lock:
        return dict(_unmetered_calls)


ledger = PostgresLedger()
