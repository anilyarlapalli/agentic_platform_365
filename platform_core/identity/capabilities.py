"""What an actor may do, expressed as capabilities rather than roles.

A role answers "what kind of actor is this". A capability answers "may this
actor do this thing". Collapsing them is how ``admin`` becomes the answer to
every question: the Azure build gates ingestion, onboarding and the LLM-backend
switch all on the single ``admin`` role, and then has to invent a side channel —
a per-domain reviewer grants table — the moment one action needs finer authority
than that. The grants table is the right instinct; it just arrives as an
exception rather than as the model.

Here the model is capabilities from the start:

* Roles **map to** capabilities. That mapping is the policy, in one place.
* Capabilities can also be **granted directly**, scoped to a resource, with an
  expiry. That is what a per-collection reviewer is, without a new concept.
* Every check is against a capability. Nothing checks a role at a call site, so
  changing which role can do what is a policy edit rather than a code search.

The critical property: :func:`require` is **deny by default**. An action with no
mapping and no grant is refused, so adding a new action without deciding its
authority fails closed rather than inheriting whatever the nearest route had.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import text

from platform_core.db.engine import tenant_session
from platform_core.identity.principal import Principal, RequestContext, Role


class Capability(StrEnum):
    """Every distinct thing an actor can be authorised to do.

    Deliberately fine-grained. Two capabilities that always travel together cost
    nothing; one capability covering two actions that later need to diverge
    costs a migration and a security review.
    """

    # ── reading ──────────────────────────────────────────────────────────
    # "Read back who I am." Deliberately its own capability rather than reusing
    # QUERY_READ: a console needs to resolve the caller's identity before it
    # knows whether that caller may query anything at all, and borrowing an
    # unrelated capability for it would make "can see their own name" and "can
    # read the corpus" impossible to separate later.
    SESSION_READ = "session:read"
    QUERY_READ = "query:read"
    DOCUMENT_READ = "document:read"
    RUN_READ = "run:read"
    EVAL_READ = "eval:read"
    USAGE_READ = "usage:read"
    AUDIT_READ = "audit:read"
    # Reading a drafted taxonomy. Separate from both SCHEMA_AUTHOR and
    # SCHEMA_APPROVE because neither covers it: authoring is owner-only,
    # approving is the reviewer's, and a reviewer must be able to *read* a draft
    # before deciding on it. Folding reads into SCHEMA_APPROVE would mean
    # granting the power to approve in order to grant the power to look.
    SCHEMA_READ = "schema:read"

    # ── writing ──────────────────────────────────────────────────────────
    DOCUMENT_INGEST = "document:ingest"
    DOCUMENT_DELETE = "document:delete"
    DATA_ERASE = "data:erase"
    RUN_CANCEL = "run:cancel"
    EVAL_RUN = "eval:run"

    # ── privileged ───────────────────────────────────────────────────────
    # Separated from ingestion because they are strictly more dangerous: they
    # spend budget per call and write schema. The Azure build ships the
    # equivalent routes with no auth dependency at all and has to retrofit a
    # middleware to cover them.
    SCHEMA_AUTHOR = "schema:author"
    SCHEMA_APPROVE = "schema:approve"
    BUDGET_MANAGE = "budget:manage"
    MEMBER_MANAGE = "member:manage"
    RELEASE_PROMOTE = "release:promote"

    # ── tools ────────────────────────────────────────────────────────────
    TOOL_INVOKE_READONLY = "tool:invoke:readonly"
    TOOL_INVOKE_WRITE = "tool:invoke:write"
    TOOL_APPROVE = "tool:approve"


# The policy. One table, so "who can do what" is answerable by reading rather
# than by grepping for role checks.
ROLE_CAPABILITIES: dict[Role, frozenset[Capability]] = {
    Role.OWNER: frozenset(Capability),  # everything, within their own tenant
    Role.OPERATOR: frozenset({
        Capability.SESSION_READ,
        Capability.QUERY_READ,
        Capability.DOCUMENT_READ,
        Capability.DOCUMENT_INGEST,
        Capability.RUN_READ,
        Capability.RUN_CANCEL,
        Capability.EVAL_READ,
        Capability.EVAL_RUN,
        Capability.USAGE_READ,
        Capability.SCHEMA_READ,
        Capability.TOOL_INVOKE_READONLY,
    }),
    Role.REVIEWER: frozenset({
        Capability.SESSION_READ,
        Capability.QUERY_READ,
        Capability.DOCUMENT_READ,
        Capability.RUN_READ,
        Capability.EVAL_READ,
        Capability.SCHEMA_READ,
        Capability.SCHEMA_APPROVE,
        Capability.TOOL_APPROVE,
        Capability.AUDIT_READ,
    }),
    Role.VIEWER: frozenset({
        Capability.SESSION_READ,
        Capability.QUERY_READ,
        Capability.DOCUMENT_READ,
        Capability.RUN_READ,
        Capability.EVAL_READ,
    }),
    # A service principal gets nothing implicitly. Workers receive exactly the
    # capabilities their workload declares, granted for the run — so a
    # compromised worker cannot do more than the job it was given.
    Role.SERVICE: frozenset(),
}

# A reviewer must not approve their own request. Maker-cannot-be-checker is
# structural here rather than advisory: `require` refuses when the actor is the
# subject, regardless of capability.
SELF_APPROVAL_FORBIDDEN: frozenset[Capability] = frozenset({
    Capability.SCHEMA_APPROVE,
    Capability.TOOL_APPROVE,
})


class NotAuthorized(Exception):
    """The actor may not perform this action. Carries why, for the audit log."""

    def __init__(self, capability: Capability, principal: Principal,
                 *, resource: str | None = None, reason: str = "no grant") -> None:
        self.capability = capability
        self.principal = principal
        self.resource = resource
        self.reason = reason
        super().__init__(
            f"{principal.subject} lacks {capability} "
            f"{f'on {resource} ' if resource else ''}({reason})"
        )


def capabilities_of(principal: Principal) -> frozenset[Capability]:
    """Capabilities from roles alone, ignoring resource-scoped grants."""
    out: set[Capability] = set()
    for role in principal.roles:
        out |= ROLE_CAPABILITIES.get(role, frozenset())
    return frozenset(out)


def has_grant(principal: Principal, capability: Capability, resource: str) -> bool:
    """Whether a direct, resource-scoped grant exists and is unexpired.

    Denies on a failed lookup. Treating an unreachable grants table as "allow"
    would open privileged actions to every authenticated user at exactly the
    moment the database is misbehaving — the Azure build reaches the same
    conclusion for its reviewer lookup, and it is worth stating as a rule:
    an authorisation check that cannot complete has not passed.
    """
    try:
        with tenant_session(principal.tenant) as s:
            found = s.execute(
                text(
                    "SELECT 1 FROM capability_grant "
                    "WHERE principal_id = :pid AND capability = :cap "
                    "AND (resource = :res OR resource = '*') "
                    "AND (expires_at IS NULL OR expires_at > now()) "
                    "AND revoked_at IS NULL LIMIT 1"
                ),
                {"pid": principal.id, "cap": str(capability), "res": resource},
            ).scalar_one_or_none()
        return found is not None
    except Exception:
        return False


def allows(
    principal: Principal,
    capability: Capability,
    *,
    resource: str | None = None,
    subject_principal_id: uuid.UUID | None = None,
) -> tuple[bool, str]:
    """The decision, and the reason for it. Returns ``(allowed, reason)``.

    Split from :func:`require` so the same logic serves a check that raises and
    a check that reports — the Azure build's two entry points share one decision
    function for exactly this reason, and it is the right pattern: two
    implementations drift, and the softer one becomes the way in.
    """
    if capability in SELF_APPROVAL_FORBIDDEN and subject_principal_id == principal.id:
        return False, "self-approval forbidden"

    if capability in capabilities_of(principal):
        return True, "role grant"

    if resource and has_grant(principal, capability, resource):
        return True, "resource grant"

    return False, "no grant"


def require(
    principal: Principal,
    capability: Capability,
    *,
    resource: str | None = None,
    subject_principal_id: uuid.UUID | None = None,
) -> None:
    """Raise :class:`NotAuthorized` unless the action is permitted."""
    allowed, reason = allows(
        principal, capability, resource=resource, subject_principal_id=subject_principal_id
    )
    if not allowed:
        raise NotAuthorized(capability, principal, resource=resource, reason=reason)


def grant(
    granter: Principal,
    to_principal_id: uuid.UUID,
    capability: Capability,
    *,
    resource: str = "*",
    expires_at: datetime | None = None,
    audit_context: RequestContext | None = None,
) -> uuid.UUID:
    """Grant a resource-scoped capability. Requires MEMBER_MANAGE.

    A granter can only grant capabilities it holds itself. Without that rule any
    member-manager could escalate to owner by granting themselves the
    capabilities they lack, which makes MEMBER_MANAGE equivalent to OWNER.
    """
    require(granter, Capability.MEMBER_MANAGE)
    if capability not in capabilities_of(granter):
        raise NotAuthorized(
            capability, granter, resource=resource,
            reason="cannot grant a capability the granter does not hold",
        )

    with tenant_session(granter.tenant) as s:
        grant_id = s.execute(
            text(
                "INSERT INTO capability_grant "
                "(tenant_id, principal_id, capability, resource, granted_by, expires_at) "
                "VALUES (:t, :p, :c, :r, :g, :e) "
                "ON CONFLICT (tenant_id, principal_id, capability, resource) "
                "DO UPDATE SET revoked_at = NULL, expires_at = EXCLUDED.expires_at, "
                "granted_by = EXCLUDED.granted_by, granted_at = now() "
                "RETURNING id"
            ),
            {
                "t": granter.tenant.id, "p": to_principal_id, "c": str(capability),
                "r": resource, "g": granter.id,
                "e": expires_at.astimezone(UTC) if expires_at else None,
            },
        ).scalar_one()
        if audit_context is not None:
            from platform_core.observability import audit

            audit.append_in_session(
                s,
                audit_context,
                action="member.capability.granted",
                outcome=audit.Outcome.SUCCEEDED,
                resource_type="principal",
                resource_id=str(to_principal_id),
                detail={
                    "grant_id": str(grant_id),
                    "capability": str(capability),
                    "resource": resource,
                    "expires_at": expires_at.isoformat() if expires_at else None,
                },
            )
        return grant_id


def revoke(
    revoker: Principal,
    grant_id: uuid.UUID,
    *,
    audit_context: RequestContext | None = None,
) -> bool:
    """Soft-revoke, preserving the row. The audit trail needs the history."""
    require(revoker, Capability.MEMBER_MANAGE)
    with tenant_session(revoker.tenant) as s:
        revoked = s.execute(
            text(
                "UPDATE capability_grant SET revoked_at = now(), revoked_by = :r "
                "WHERE id = :id AND revoked_at IS NULL RETURNING principal_id, "
                "capability, resource"
            ),
            {"id": grant_id, "r": revoker.id},
        ).one_or_none()
        if revoked is not None and audit_context is not None:
            from platform_core.observability import audit

            audit.append_in_session(
                s,
                audit_context,
                action="member.capability.revoked",
                outcome=audit.Outcome.SUCCEEDED,
                resource_type="principal",
                resource_id=str(revoked.principal_id),
                detail={
                    "grant_id": str(grant_id),
                    "capability": revoked.capability,
                    "resource": revoked.resource,
                },
            )
        return revoked is not None
