"""The grant matrix is asserted, not inferred from what a migration says it did.

A migration writing `GRANT SELECT, INSERT ON x TO platform_app` reads like a
restriction and is not one. `ALTER DEFAULT PRIVILEGES` in the container's init
script already granted all four privileges on every table the owner creates, so
a narrower grant later is purely additive — it adds nothing and removes nothing.

That is how `audit_event` came to carry UPDATE and DELETE for the app role while
`0006`'s docstring stated the opposite. The immutability guarantee still held,
because the trigger enforced it; the *grant* did not, and nobody would have
known until someone dumped `role_table_grants`.

So these tests read the live catalog. An explicit REVOKE is the only thing that
narrows a default, and the only way to be sure is to look.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from platform_core.db.engine import owner_session
from platform_core.db.models import PLATFORM_TABLES, TENANT_SCOPED_TABLES

pytestmark = pytest.mark.property

APP_ROLE = "platform_app"
RELAY_ROLE = "platform_relay"

# Tables the application may append to but never alter or remove from.
APPEND_ONLY = {"audit_event", "llm_usage", "release_observation"}
# Tables the application may read but never write at all.
READ_ONLY = {"tenant", "release"}
# The relay's entire reach: cross-tenant delivery and recovery, nothing else.
RELAY_TABLES = {"outbox", "side_effect", "run"}


def _grants(role: str) -> dict[str, set[str]]:
    with owner_session() as s:
        rows = s.execute(
            text(
                "SELECT table_name, privilege_type FROM information_schema.role_table_grants "
                "WHERE table_schema = 'public' AND grantee = :role"
            ),
            {"role": role},
        ).all()
    out: dict[str, set[str]] = {}
    for row in rows:
        out.setdefault(row.table_name, set()).add(row.privilege_type)
    return out


def test_append_only_tables_cannot_be_modified_by_the_app(record_evidence):
    """The ledger and the audit log accept inserts and nothing else."""
    grants = _grants(APP_ROLE)
    offenders = {
        table: sorted(grants.get(table, set()) & {"UPDATE", "DELETE", "TRUNCATE"})
        for table in APPEND_ONLY
        if grants.get(table, set()) & {"UPDATE", "DELETE", "TRUNCATE"}
    }
    assert not offenders, (
        f"append-only tables are modifiable by {APP_ROLE}: {offenders}. "
        f"A narrower GRANT does not undo ALTER DEFAULT PRIVILEGES — only REVOKE does."
    )
    for table in APPEND_ONLY:
        assert "INSERT" in grants.get(table, set()), f"{table} is not insertable"

    record_evidence(
        "privilege_append_only_enforced", holds=True, tables=sorted(APPEND_ONLY),
        detail="INSERT and SELECT only; UPDATE, DELETE and TRUNCATE revoked",
    )


def test_read_only_tables_cannot_be_written_by_the_app(record_evidence):
    """Minting a tenant or shifting traffic is not a request-path action."""
    grants = _grants(APP_ROLE)
    for table in READ_ONLY:
        writes = grants.get(table, set()) & {"INSERT", "UPDATE", "DELETE"}
        assert not writes, f"{APP_ROLE} can write {table}: {sorted(writes)}"
        assert "SELECT" in grants.get(table, set())

    record_evidence(
        "privilege_read_only_enforced", holds=True, tables=sorted(READ_ONLY),
        detail="tenant and release are readable but not writable by the app role",
    )


def test_the_relay_reaches_only_delivery_tables(record_evidence):
    """The cross-tenant credential is bounded by grants, not by good behaviour."""
    grants = _grants(RELAY_ROLE)
    writable = {t for t, p in grants.items() if p & {"INSERT", "UPDATE", "DELETE"}}
    assert writable == RELAY_TABLES, (
        f"relay can write {sorted(writable)}, expected exactly {sorted(RELAY_TABLES)}"
    )
    # It may read tenant identity — it must resolve a tenant_id before it can
    # open a scoped session — but never write it.
    assert "SELECT" in grants.get("tenant", set())
    assert not grants.get("tenant", set()) & {"INSERT", "UPDATE", "DELETE"}

    record_evidence(
        "privilege_relay_bounded", holds=True,
        writable=sorted(writable), reads_tenant=True,
        detail="relay writes three delivery tables and reads tenant identity only",
    )


def test_every_table_is_either_tenant_scoped_or_declared_platform_wide(record_evidence):
    """No table escapes classification.

    A table that is neither RLS-protected nor on the platform-wide list is one
    nobody decided about — which is how a boundary narrows silently over time.
    """
    with owner_session() as s:
        # 'p' as well as 'r': since 0014 a partitioned parent is relkind 'p',
        # and filtering to 'r' alone would drop `chunk` from the classification
        # check entirely while admitting its sixteen partitions.
        tables = {
            row.relname
            for row in s.execute(
                text(
                    "SELECT relname FROM pg_class "
                    "WHERE relnamespace = 'public'::regnamespace AND relkind IN ('r', 'p') "
                    "AND relname NOT LIKE 'pg_%' AND relname <> 'alembic_version'"
                )
            )
        }
        # A partition is not independently classified — it is part of its
        # parent, which is classified. Resolving them keeps the declared lists
        # stable across a repartitioning.
        partitions = set(
            s.execute(
                text(
                    "SELECT c.relname FROM pg_inherits i "
                    "JOIN pg_class c ON c.oid = i.inhrelid "
                    "WHERE c.relnamespace = 'public'::regnamespace"
                )
            ).scalars()
        )

    unclassified = tables - partitions - set(TENANT_SCOPED_TABLES) - PLATFORM_TABLES
    assert not unclassified, (
        f"tables classified as neither tenant-scoped nor platform-wide: "
        f"{sorted(unclassified)}"
    )

    record_evidence(
        "privilege_all_tables_classified", holds=True,
        tenant_scoped=len(TENANT_SCOPED_TABLES), platform_wide=len(PLATFORM_TABLES),
        detail="every table is explicitly one or the other",
    )
