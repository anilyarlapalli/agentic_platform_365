"""Tenant isolation is enforced by Postgres, not by application code.

This is the Phase 0 acceptance test. Everything else in the platform assumes the
boundary holds; these cases are what make that an observation rather than an
assumption.

The cases are ordered by how badly each failure would matter:

1. **Cross-tenant read** — the obvious one, and the least dangerous, because a
   leak that only reads is at least detectable after the fact.
2. **Cross-tenant write** — worse. A tenant that can INSERT rows carrying
   another tenant's id has corrupted data it cannot even see. This is what
   ``WITH CHECK`` prevents, and it is the clause most implementations omit.
3. **Vector search** — the one most likely to be broken in practice, because
   similarity search usually takes a different code path from a normal SELECT
   and it is easy to build it against a connection that has no tenant set.
4. **Fail-closed default** — no tenant context must return zero rows, never all.
5. **Pooled-connection carry-over** — the trap that makes the other four pass in
   testing and fail in production.
6. **Role capability** — the app role must not be able to bypass any of it.
7. **Catalog completeness** — a new table without a policy must break the build.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError

from platform_core.db.engine import (
    _session_factory,
    current_tenant,
    owner_session,
    system_session,
    tenant_session,
)
from platform_core.db.models import TENANT_SCOPED_TABLES

pytestmark = pytest.mark.property


def test_cross_tenant_read_returns_nothing(seeded_corpus, tenant_a, tenant_b, record_evidence):
    """A tenant sees its own rows and only its own, even when they look alike."""
    with tenant_session(tenant_a) as s:
        rows = s.execute(text("SELECT text FROM chunk")).scalars().all()

    assert len(rows) == 1, f"tenant A saw {len(rows)} chunks; exactly one is its own"
    assert "[acme]" in rows[0]
    assert "[globex]" not in rows[0]

    # And the reciprocal, because a policy that filters one direction only is a
    # policy that was written against the wrong column.
    with tenant_session(tenant_b) as s:
        rows_b = s.execute(text("SELECT text FROM chunk")).scalars().all()
    assert len(rows_b) == 1 and "[globex]" in rows_b[0]

    # Addressing another tenant's row by its primary key must also fail. Filters
    # that only apply to unqualified scans are a common half-measure.
    foreign_id = seeded_corpus["globex"]["chunk_id"]
    with tenant_session(tenant_a) as s:
        direct = s.execute(
            text("SELECT text FROM chunk WHERE id = :id"), {"id": foreign_id}
        ).scalar_one_or_none()
    assert direct is None, "tenant A read tenant B's chunk by primary key"

    record_evidence(
        "tenant_isolation_read",
        holds=True,
        detail="scan and primary-key access both scoped; verified in both directions",
    )


def test_cross_tenant_write_is_rejected(seeded_corpus, tenant_a, tenant_b, record_evidence):
    """WITH CHECK: a tenant cannot write rows it would not be allowed to read.

    Without this clause a tenant can INSERT under another tenant's id, or UPDATE
    its own row to transfer ownership. Neither is visible to the attacker
    afterwards, which is exactly why it goes unnoticed.
    """
    doc_a = seeded_corpus["acme"]["document_id"]

    # INSERT carrying the foreign tenant id.
    with pytest.raises(ProgrammingError) as insert_err, tenant_session(tenant_a) as s:
        s.execute(
            text(
                "INSERT INTO chunk (tenant_id, document_id, collection, canonical_id, "
                "ordinal, text) VALUES (:foreign, :d, 'maintenance', 'c_injected0000000', "
                "99, 'injected')"
            ),
            {"foreign": tenant_b.id, "d": doc_a},
        )
    assert "row-level security" in str(insert_err.value).lower()

    # UPDATE handing a row to the other tenant. Without WITH CHECK this
    # succeeds silently: the pre-image passes USING, and there is nothing left
    # to validate the post-image.
    with pytest.raises(ProgrammingError) as update_err, tenant_session(tenant_a) as s:
        s.execute(
            text("UPDATE chunk SET tenant_id = :foreign"), {"foreign": tenant_b.id}
        )
    assert "row-level security" in str(update_err.value).lower()

    # Tenant B's data is unchanged and still exactly one row.
    with tenant_session(tenant_b) as s:
        assert s.execute(text("SELECT count(*) FROM chunk")).scalar_one() == 1

    record_evidence(
        "tenant_isolation_write",
        holds=True,
        detail="WITH CHECK rejects both foreign-tenant INSERT and ownership-transfer UPDATE",
    )


def test_vector_search_is_tenant_scoped(seeded_corpus, tenant_a, record_evidence):
    """Similarity search obeys the same boundary as a plain SELECT.

    The seeded embeddings are near-identical across tenants, so an unscoped
    search would return two rows with almost equal distance. One row is the
    only correct answer.
    """
    probe = str([0.9] + [0.1] * 1535)

    with tenant_session(tenant_a) as s:
        hits = s.execute(
            text(
                "SELECT text, embedding <=> :probe AS distance FROM chunk "
                "ORDER BY embedding <=> :probe LIMIT 10"
            ),
            {"probe": probe},
        ).all()

    assert len(hits) == 1, (
        f"vector search returned {len(hits)} rows across the boundary; "
        f"got {[h.text for h in hits]}"
    )
    assert "[acme]" in hits[0].text

    record_evidence(
        "tenant_isolation_vector_search",
        holds=True,
        detail="HNSW cosine search returns only in-tenant rows despite near-identical vectors",
        neighbours_returned=len(hits),
    )


def test_missing_tenant_context_returns_zero_rows(populated_every_table, record_evidence):
    """Forgetting the context is an empty result, never an unfiltered one.

    ``current_setting('app.tenant_id', true)`` is NULL when unset, and
    ``tenant_id = NULL`` is never true. So the failure mode of forgetting to set
    a tenant is a visibly wrong empty answer, not a silent breach.

    The fixture seeds **every** tenant-scoped table first. An earlier version of
    this test relied on whatever the corpus fixture happened to create, so
    ``run``, ``outbox`` and ``side_effect`` were empty and the assertion held
    for them regardless of policy. It passed while a genuine cross-tenant hole
    was open in exactly those three tables — a test that cannot distinguish
    "blocked" from "nothing there" is not testing anything.
    """
    with system_session(reason="property test: unscoped read must see nothing") as s:
        assert current_tenant(s) is None
        for table in TENANT_SCOPED_TABLES:
            count = s.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()
            assert count == 0, f"{table} returned {count} rows with no tenant context"

    record_evidence(
        "tenant_isolation_fail_closed",
        holds=True,
        detail=(
            "unscoped session sees zero rows in every tenant-scoped table, with "
            "every table verified non-empty first"
        ),
        tables_checked=list(TENANT_SCOPED_TABLES),
        rows_seeded_per_table=populated_every_table,
    )


def test_relay_role_reaches_only_its_three_tables(populated_every_table, record_evidence):
    """The cross-tenant credential is narrow, and narrow is asserted.

    ``platform_relay`` exists so one relay can drain every tenant's outbox. That
    is a real privilege, so its blast radius is bounded by grants rather than by
    the relay's own good behaviour: it must see across tenants in ``outbox``,
    ``side_effect`` and ``run``, and be refused everywhere else.
    """
    from sqlalchemy.exc import ProgrammingError

    from platform_core.db.engine import relay_session
    from platform_core.db.models import RELAY_ACCESSIBLE_TABLES

    reachable, refused = {}, []
    with relay_session(reason="property test: relay reach") as s:
        for table in TENANT_SCOPED_TABLES:
            try:
                reachable[table] = s.execute(
                    text(f"SELECT count(*) FROM {table}")
                ).scalar_one()
            except ProgrammingError:
                refused.append(table)
                s.rollback()

    assert set(reachable) == RELAY_ACCESSIBLE_TABLES, (
        f"relay reached {sorted(reachable)}, expected exactly "
        f"{sorted(RELAY_ACCESSIBLE_TABLES)}"
    )
    # Cross-tenant within those three: two tenants were seeded, so a correctly
    # privileged relay sees both.
    for table in RELAY_ACCESSIBLE_TABLES:
        assert reachable[table] >= 2, (
            f"relay saw {reachable[table]} rows in {table}; it must see across tenants"
        )

    # The relay may READ tenant identity — it must turn a tenant_id into a
    # (id, slug) before it can open a scoped session at all — but must never
    # write it. Creating a tenant mints an isolation scope; updating one moves
    # budget ceilings. Neither belongs to a delivery process.
    with relay_session(reason="property test: relay tenant read/write asymmetry") as s:
        assert s.execute(text("SELECT count(*) FROM tenant")).scalar_one() >= 2

    with (
        pytest.raises(ProgrammingError) as write_err,
        relay_session(reason="property test: relay must not write tenant") as s,
    ):
        s.execute(text("INSERT INTO tenant (slug, name) VALUES ('relay-owned', 'x')"))
    assert "permission denied" in str(write_err.value).lower()

    record_evidence(
        "relay_credential_is_narrow",
        holds=True,
        reachable=sorted(reachable),
        refused=sorted(refused),
        detail=(
            "cross-tenant in outbox/side_effect/run only; denied on every other "
            "tenant-scoped table; tenant is readable but not writable"
        ),
    )


def test_app_role_cannot_authenticate_as_relay(record_evidence):
    """The API cannot become the relay, whatever it calls.

    This is what makes the privilege a credential rather than a flag. The
    previous design keyed cross-tenant access on a session variable, so the
    authentication path's own system session could read every tenant's runs.
    """
    from sqlalchemy import text as sa_text

    from platform_core.db.engine import system_session as sysdb

    with sysdb(reason="property test: app role identity") as s:
        role = s.execute(sa_text("SELECT current_user")).scalar_one()
        can_switch = s.execute(
            sa_text(
                "SELECT pg_has_role(current_user, 'platform_relay', 'MEMBER')"
            )
        ).scalar_one()

    assert role == "platform_app"
    assert can_switch is False, (
        "platform_app is a member of platform_relay and could SET ROLE to it, "
        "which collapses the credential boundary back into a flag"
    )

    record_evidence(
        "relay_privilege_is_a_credential",
        holds=True,
        detail="platform_app is not a member of platform_relay and cannot assume it",
    )


def test_tenant_context_does_not_survive_into_the_pool(tenant_a, tenant_b, seeded_corpus,
                                                       record_evidence):
    """The pooled-connection trap, exercised directly.

    ``SET`` would leave ``app.tenant_id`` on the connection for the next
    checkout; ``SET LOCAL`` is reverted at COMMIT. This drives a pool of exactly
    one connection so that every session is guaranteed to reuse the same
    physical socket, alternating tenants — the arrangement in which a
    session-scoped GUC leaks immediately.
    """
    factory = _session_factory("app")
    engine = factory.kw["bind"]

    observed: list[tuple[str, int]] = []
    for tenant, marker in ((tenant_a, "acme"), (tenant_b, "globex")) * 3:
        with tenant_session(tenant) as s:
            texts = s.execute(text("SELECT text FROM chunk")).scalars().all()
            observed.append((marker, len(texts)))
            assert len(texts) == 1
            assert f"[{marker}]" in texts[0], (
                f"session for {marker} saw {texts[0]!r} — the previous tenant's context "
                f"survived into this connection"
            )

        # After the transaction closes, the setting must be gone.
        bare = factory()
        try:
            leaked = bare.execute(
                text("SELECT current_setting('app.tenant_id', true)")
            ).scalar_one_or_none()
            assert not leaked, f"app.tenant_id survived as {leaked!r} on a pooled connection"
        finally:
            bare.close()

    record_evidence(
        "tenant_isolation_pool_carryover",
        holds=True,
        detail="SET LOCAL reverted at commit; alternating tenants over a shared pool stay scoped",
        alternations=len(observed),
        pool_size=engine.pool.size(),
    )


def test_application_role_cannot_bypass_rls(record_evidence):
    """The runtime role must not be a superuser and must not hold BYPASSRLS.

    Both are silent bypasses: policies still exist, ``pg_policies`` still lists
    them, and none of them apply. Asserting the role's capabilities is the only
    way to know the control is reachable at all.
    """
    with system_session(reason="property test: verify app role capabilities") as s:
        role = s.execute(text("SELECT current_user")).scalar_one()
        is_super = s.execute(text("SELECT current_setting('is_superuser')")).scalar_one()
        bypass = s.execute(
            text("SELECT rolbypassrls FROM pg_roles WHERE rolname = current_user")
        ).scalar_one()

    assert role == "platform_app", f"runtime connected as {role!r}, expected platform_app"
    assert is_super == "off", "the runtime role is a superuser; every policy is decorative"
    assert bypass is False, "the runtime role holds BYPASSRLS"

    record_evidence(
        "tenant_isolation_role_capability",
        holds=True,
        detail="runtime role is non-superuser without BYPASSRLS",
        role=role,
    )


def test_every_tenant_scoped_table_is_protected(record_evidence):
    """A table with a tenant_id but no policy must fail the build.

    This is the guard against the boundary quietly narrowing over time: the
    check is derived from the catalog, so a table added in a later migration
    without RLS fails here rather than in an incident.
    """
    with owner_session() as s:
        with_tenant_column = set(
            s.execute(
                text(
                    "SELECT table_name FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND column_name = 'tenant_id'"
                )
            ).scalars()
        )
        # `relkind = 'p'` matters since 0014: a partitioned parent is 'p', not
        # 'r', so filtering to 'r' alone silently drops `chunk` — the check
        # would then pass while ignoring the largest tenant-scoped table.
        protected = {
            row.relname
            for row in s.execute(
                text(
                    "SELECT relname FROM pg_class "
                    "WHERE relnamespace = 'public'::regnamespace "
                    "AND relkind IN ('r', 'p') AND relrowsecurity AND relforcerowsecurity"
                )
            )
        }
        # Partition name → parent name. A partition is not a table anyone
        # declares; it inherits its parent's classification. It does *not*
        # inherit the parent's RLS, though, which is asserted separately below.
        partition_parent = dict(
            s.execute(
                text(
                    "SELECT c.relname, p.relname FROM pg_inherits i "
                    "JOIN pg_class c ON c.oid = i.inhrelid "
                    "JOIN pg_class p ON p.oid = i.inhparent "
                    "WHERE c.relnamespace = 'public'::regnamespace"
                )
            ).all()
        )
        with_policy = set(
            s.execute(
                text("SELECT DISTINCT tablename FROM pg_policies WHERE schemaname = 'public'")
            ).scalars()
        )

    # Every relation carrying tenant_id must be protected — partitions
    # included. RLS is **not** inherited: a policy on the parent governs rows
    # reached through the parent, while `SELECT * FROM chunk_p3` is subject only
    # to chunk_p3's own policies, and platform_app can name it. An unprotected
    # partition is a direct-access hole in the control the parent enforces.
    unprotected = with_tenant_column - protected
    assert not unprotected, (
        f"tables carry tenant_id but lack ENABLE+FORCE row level security: {sorted(unprotected)}"
    )

    policyless = with_tenant_column - with_policy
    assert not policyless, f"tables have RLS enabled but no policy: {sorted(policyless)}"

    # Collapse partitions onto their parent before comparing with the declared
    # list. Declaring each partition would make the constant churn on every
    # repartitioning while proving nothing extra — the protection assertions
    # above already cover them individually.
    declared_equivalent = {
        partition_parent.get(name, name) for name in with_tenant_column
    }
    assert declared_equivalent == set(TENANT_SCOPED_TABLES), (
        f"TENANT_SCOPED_TABLES={sorted(TENANT_SCOPED_TABLES)} disagrees with the catalog "
        f"{sorted(declared_equivalent)}"
    )

    record_evidence(
        "tenant_isolation_catalog_complete",
        holds=True,
        detail="every table carrying tenant_id has ENABLE+FORCE RLS and a policy",
        tables=sorted(with_tenant_column),
    )


def test_app_role_cannot_create_a_tenant(tenant_a, record_evidence):
    """Minting an isolation scope is a platform operation, not a tenant one.

    If the application credential could INSERT into ``tenant``, a compromise
    would let an attacker create a scope, place itself in it, and operate
    entirely inside the rules.
    """
    with pytest.raises(ProgrammingError) as err, tenant_session(tenant_a) as s:
        s.execute(
            text("INSERT INTO tenant (slug, name) VALUES ('attacker-owned', 'x')")
        )
    assert "permission denied" in str(err.value).lower()

    record_evidence(
        "tenant_creation_is_privileged",
        holds=True,
        detail="app role lacks INSERT on tenant; scope creation requires the owner role",
    )


def test_unknown_tenant_id_sees_nothing(seeded_corpus, record_evidence):
    """A well-formed but unknown tenant id is not a skeleton key."""
    with tenant_session(uuid.uuid4()) as s:
        for table in TENANT_SCOPED_TABLES:
            assert s.execute(text(f"SELECT count(*) FROM {table}")).scalar_one() == 0

    record_evidence(
        "tenant_isolation_unknown_id",
        holds=True,
        detail="an unrecognised tenant uuid returns zero rows rather than all rows",
    )
