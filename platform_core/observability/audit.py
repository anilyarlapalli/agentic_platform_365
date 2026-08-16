"""Append-only, hash-chained audit.

## What the chain buys

Each event carries the digest of the previous event for its tenant, and its own
digest covers both its content and that link. Altering any historical row
invalidates every digest after it, so tampering is *detectable* rather than
merely discouraged.

That matters because the alternative — an ordinary table — proves nothing. A log
the application can rewrite is a log that says whatever the application says it
says, which is exactly the property an auditor is trying to avoid relying on.

Enforcement is at the database: `UPDATE` and `DELETE` raise from a trigger, and
neither grant is issued to any role. :func:`verify_chain` re-derives every digest
and reports the first break.

## What is audited

Authorisation decisions (both outcomes — a denial is often the interesting one),
privileged actions, and anything that moves money or data. Deliberately **not**
every read: an audit log nobody can search is one nobody uses, and volume is the
usual reason.

## Scope

Chained **per tenant**, not globally. A global chain would serialise every
tenant's audit writes behind whichever tenant is busiest, and one tenant's write
rate would become another's latency.

Contrast with the Azure build, whose audit log is HITL-approval-shaped: there is
no record of who ingested what, who changed a budget, or who granted a reviewer.
Those facts exist scattered across `ingest_jobs.created_by` and
`llm_usage.actor`, with no single answer to "what did this admin do".
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from platform_core.db.engine import tenant_session
from platform_core.identity.principal import Principal, RequestContext
from platform_core.settings import get_settings

logger = logging.getLogger("platform.observability.audit")


class Outcome(StrEnum):
    ALLOWED = "allowed"
    DENIED = "denied"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class AuditUnavailable(RuntimeError):
    """A mandatory audit event could not be durably appended."""


@dataclass(frozen=True, slots=True)
class AuditEvent:
    id: int
    at: datetime
    actor_subject: str
    action: str
    resource_type: str | None
    resource_id: str | None
    outcome: str
    detail: dict[str, Any]
    prev_hash: str | None
    hash: str


def _digest(
    *, tenant_id: uuid.UUID, actor_subject: str, action: str, resource_type: str | None,
    resource_id: str | None, outcome: str, detail: dict, at: str, prev_hash: str | None,
) -> str:
    """The chain link.

    ``sort_keys`` and a fixed field order matter: the digest has to be
    reproducible from the stored row months later, by code that may serialise
    dictionaries in a different order. A digest that depends on iteration order
    is a chain that breaks for no reason and stops being believed.
    """
    payload = json.dumps(
        {
            "tenant_id": str(tenant_id),
            "actor_subject": actor_subject,
            "action": action,
            "resource_type": resource_type,
            "resource_id": resource_id,
            "outcome": outcome,
            "detail": detail,
            "at": at,
            "prev_hash": prev_hash,
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def append_in_session(
    session: Session,
    ctx: RequestContext | None,
    *,
    action: str,
    outcome: Outcome,
    principal: Principal | None = None,
    resource_type: str | None = None,
    resource_id: str | None = None,
    detail: dict[str, Any] | None = None,
) -> str:
    """Append in the caller's tenant transaction.

    Security-sensitive database mutations use this form so the state change
    and its audit event commit or roll back together.
    """
    actor = principal or (ctx.principal if ctx else None)
    if actor is None:
        raise AuditUnavailable(f"audit event {action!r} has no actor")

    tenant = actor.tenant
    payload = detail or {}
    scoped_tenant = session.execute(
        text("SELECT NULLIF(current_setting('app.tenant_id', true), '')::uuid")
    ).scalar_one_or_none()
    if scoped_tenant != tenant.id:
        raise AuditUnavailable(
            f"audit tenant {tenant.id} does not match transaction scope {scoped_tenant}"
        )

    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext('audit:' || :t))"),
        {"t": str(tenant.id)},
    )
    prev = session.execute(
        text(
            "SELECT coalesce(" 
            "  (SELECT hash FROM audit_event WHERE tenant_id = :t "
            "   ORDER BY id DESC LIMIT 1), "
            "  (SELECT through_hash FROM audit_chain_anchor WHERE tenant_id = :t)"
            ")"
        ),
        {"t": tenant.id},
    ).scalar_one_or_none()
    at = session.execute(text("SELECT now()" )).scalar_one()
    digest = _digest(
        tenant_id=tenant.id, actor_subject=actor.subject, action=action,
        resource_type=resource_type, resource_id=resource_id,
        outcome=str(outcome), detail=payload, at=at.isoformat(), prev_hash=prev,
    )
    session.execute(
        text(
            "INSERT INTO audit_event (tenant_id, principal_id, actor_subject, "
            "  action, resource_type, resource_id, outcome, detail, request_id, "
            "  run_id, release, at, prev_hash, hash) "
            "VALUES (:t, :p, :subj, :action, :rtype, :rid, :outcome, :detail, "
            "  :req, :run, :rel, :at, :prev, :hash)"
        ),
        {
            "t": tenant.id, "p": actor.id, "subj": actor.subject,
            "action": action, "rtype": resource_type, "rid": resource_id,
            "outcome": str(outcome), "detail": json.dumps(payload),
            "req": ctx.request_id if ctx else None,
            "run": ctx.run_id if ctx else None,
            "rel": get_settings().release, "at": at,
            "prev": prev, "hash": digest,
        },
    )
    return digest


def record(
    ctx: RequestContext | None,
    *,
    action: str,
    outcome: Outcome,
    principal: Principal | None = None,
    resource_type: str | None = None,
    resource_id: str | None = None,
    detail: dict[str, Any] | None = None,
    required: bool = False,
) -> str | None:
    """Append an event. A required event fails closed if it cannot be written.

    Best-effort outcome events return ``None`` on failure. Privileged admission
    uses ``required=True`` and refuses to begin if the trail is unavailable.

    ``ctx`` may be None for a denied authentication, where there is no
    authenticated principal yet; ``principal`` then supplies whatever identity is
    known.
    """
    actor = principal or (ctx.principal if ctx else None)
    if actor is None:
        logger.error("audit event %r has no actor — not recorded", action)
        if required:
            raise AuditUnavailable(f"audit event {action!r} has no actor")
        return None

    tenant = actor.tenant
    payload = detail or {}

    try:
        with tenant_session(tenant) as s:
            # A transaction-scoped advisory lock, keyed per tenant, serialises
            # concurrent appends so two events cannot claim the same predecessor
            # and fork the chain.
            #
            # Not `SELECT ... FOR UPDATE`: row locking requires UPDATE privilege
            # on the table, and this table is append-only precisely so the app
            # role does *not* hold UPDATE. The two requirements are in direct
            # conflict, and the immutability guarantee is the more important one
            # — so the lock moves off the row.
            #
            # An advisory lock is also the more honest primitive here. There is
            # no row being modified to lock; what needs serialising is the act
            # of appending, which is not a row at all until it exists.
            s.execute(
                text("SELECT pg_advisory_xact_lock(hashtext('audit:' || :t))"),
                {"t": str(tenant.id)},
            )
            prev = s.execute(
                text(
                    "SELECT coalesce(" 
                    "  (SELECT hash FROM audit_event WHERE tenant_id = :t "
                    "   ORDER BY id DESC LIMIT 1), "
                    "  (SELECT through_hash FROM audit_chain_anchor WHERE tenant_id = :t)"
                    ")"
                ),
                {"t": tenant.id},
            ).scalar_one_or_none()

            at = s.execute(text("SELECT now()")).scalar_one()
            digest = _digest(
                tenant_id=tenant.id, actor_subject=actor.subject, action=action,
                resource_type=resource_type, resource_id=resource_id,
                outcome=str(outcome), detail=payload, at=at.isoformat(), prev_hash=prev,
            )

            s.execute(
                text(
                    "INSERT INTO audit_event (tenant_id, principal_id, actor_subject, "
                    "  action, resource_type, resource_id, outcome, detail, request_id, "
                    "  run_id, release, at, prev_hash, hash) "
                    "VALUES (:t, :p, :subj, :action, :rtype, :rid, :outcome, :detail, "
                    "  :req, :run, :rel, :at, :prev, :hash)"
                ),
                {
                    "t": tenant.id, "p": actor.id, "subj": actor.subject,
                    "action": action, "rtype": resource_type, "rid": resource_id,
                    "outcome": str(outcome), "detail": json.dumps(payload),
                    "req": ctx.request_id if ctx else None,
                    "run": ctx.run_id if ctx else None,
                    "rel": get_settings().release, "at": at,
                    "prev": prev, "hash": digest,
                },
            )
        try:
            from platform_core.observability.telemetry import record_audit_write

            record_audit_write("succeeded", required=required)
        except Exception:
            pass
        return digest
    except Exception as exc:
        logger.error("could not record audit event %r for %s", action, tenant.slug,
                     exc_info=True)
        try:
            from platform_core.observability.telemetry import record_audit_write

            record_audit_write("failed", required=required)
        except Exception:
            pass
        if required:
            raise AuditUnavailable(
                f"mandatory audit event {action!r} could not be recorded"
            ) from exc
        return None


def recent(ctx: RequestContext, *, limit: int = 100) -> list[AuditEvent]:
    with tenant_session(ctx.tenant) as s:
        rows = s.execute(
            text(
                "SELECT id, at, actor_subject, action, resource_type, resource_id, "
                "  outcome, detail, prev_hash, hash FROM audit_event "
                "ORDER BY id DESC LIMIT :n"
            ),
            {"n": min(max(limit, 1), 500)},
        ).all()
    return [
        AuditEvent(
            id=r.id, at=r.at, actor_subject=r.actor_subject, action=r.action,
            resource_type=r.resource_type, resource_id=r.resource_id,
            outcome=r.outcome, detail=r.detail, prev_hash=r.prev_hash, hash=r.hash,
        )
        for r in rows
    ]


@dataclass(frozen=True, slots=True)
class ChainVerification:
    tenant_slug: str
    events_checked: int
    intact: bool
    first_break_id: int | None = None
    reason: str | None = None
    anchor_event_id: int | None = None
    events_anchored: int = 0


def verify_chain(tenant_id: uuid.UUID, tenant_slug: str = "") -> ChainVerification:
    """Re-derive every digest and report the first break.

    This is what makes the chain a control rather than a decoration. A hash
    nobody recomputes is a hash nobody can rely on, so verification is a
    first-class operation, runnable on demand and asserted in the property tests.
    """
    # A tenant session, not the relay credential. Verification is per-tenant by
    # construction — the chain is per-tenant — so it needs no cross-tenant
    # privilege, and asking for one would widen a credential for no reason.
    with tenant_session(tenant_id) as s:
        anchor = s.execute(
            text(
                "SELECT through_event_id, through_hash, events_anchored "
                "FROM audit_chain_anchor WHERE tenant_id = :t"
            ),
            {"t": tenant_id},
        ).one_or_none()
        rows = s.execute(
            text(
                "SELECT id, tenant_id, actor_subject, action, resource_type, "
                "  resource_id, outcome, detail, at, prev_hash, hash "
                "FROM audit_event WHERE tenant_id = :t ORDER BY id"
            ),
            {"t": tenant_id},
        ).all()

    expected_prev: str | None = anchor.through_hash if anchor else None
    anchor_id = anchor.through_event_id if anchor else None
    events_anchored = int(anchor.events_anchored) if anchor else 0
    for row in rows:
        if anchor_id is not None and row.id <= anchor_id:
            return ChainVerification(
                tenant_slug=tenant_slug,
                events_checked=len(rows),
                intact=False,
                first_break_id=row.id,
                reason=f"event {row.id} is at or before retention anchor {anchor_id}",
                anchor_event_id=anchor_id,
                events_anchored=events_anchored,
            )
        if row.prev_hash != expected_prev:
            return ChainVerification(
                tenant_slug=tenant_slug, events_checked=len(rows), intact=False,
                first_break_id=row.id,
                reason=(
                    f"event {row.id} claims predecessor {row.prev_hash!r} but the "
                    f"previous event hashed to {expected_prev!r} — a row was "
                    f"inserted, removed or reordered"
                ),
                anchor_event_id=anchor_id,
                events_anchored=events_anchored,
            )

        recomputed = _digest(
            tenant_id=row.tenant_id, actor_subject=row.actor_subject, action=row.action,
            resource_type=row.resource_type, resource_id=row.resource_id,
            outcome=row.outcome, detail=row.detail, at=row.at.isoformat(),
            prev_hash=row.prev_hash,
        )
        if recomputed != row.hash:
            return ChainVerification(
                tenant_slug=tenant_slug, events_checked=len(rows), intact=False,
                first_break_id=row.id,
                reason=(
                    f"event {row.id} content does not match its stored digest — "
                    f"the row was altered after it was written"
                ),
                anchor_event_id=anchor_id,
                events_anchored=events_anchored,
            )
        expected_prev = row.hash

    return ChainVerification(
        tenant_slug=tenant_slug,
        events_checked=len(rows),
        intact=True,
        anchor_event_id=anchor_id,
        events_anchored=events_anchored,
    )
