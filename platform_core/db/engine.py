"""Database sessions that carry a tenant, and cannot forget to.

Row-level security is only as good as the thing that sets the discriminator.
This module is that thing, and it has one job that is easy to get subtly wrong.

## The pooled-connection trap

Postgres policies here read ``current_setting('app.tenant_id', true)``. The
obvious way to populate it is::

    SET app.tenant_id = '<uuid>'

which is **session**-scoped: it survives the transaction, stays on the pooled
connection, and is inherited by whoever checks that connection out next. Under
a connection pool that is a cross-tenant read, and it is invisible in testing
because a single-tenant test never checks out a dirtied connection.

``SET LOCAL`` is transaction-scoped and is reverted on COMMIT or ROLLBACK. So
:func:`tenant_session` opens a transaction first and sets the GUC inside it,
and there is a property test that hammers a one-connection pool alternating
tenants specifically to prove a leak cannot happen.

## Fail-closed by construction

``current_setting('app.tenant_id', true)`` returns NULL when unset, and NULL is
never equal to a tenant id, so a query issued outside :func:`tenant_session`
matches **zero** rows rather than all of them. Forgetting the context is a
visible empty result, not a silent breach.

## The cross-tenant escape hatch is a credential, not a flag

Some work is legitimately cross-tenant: the outbox relay and the lease reaper.
:func:`relay_session` serves that, and it connects as **a different Postgres
role** — ``platform_relay`` — whose policies permit every tenant's rows in
exactly three tables and nothing else.

The shape matters more than the mechanism. The first attempt expressed the same
privilege as a policy keyed on a session variable: any connection that set
``app.system_reason`` and no tenant could read across the boundary. That is a
privilege *any caller can grant itself by passing a string*, and the consequence
was immediate — :func:`system_session` is also used by the readiness probe and
by the authentication path's tenant lookup, so a session opened by **login**
gained cross-tenant read on every run. Measured at 2 rows across 2 tenants
before it was reverted.

A privilege boundary has to be something a process either holds or does not
hold. The API is configured only with the ``platform_app`` DSN, so no code path
in it can reach across the boundary however it is called.

:func:`system_session` still exists, still logs its reason, and is now what its
name always implied: an app-role session with **no** tenant set, which therefore
sees nothing in any tenant-scoped table. It is for work that legitimately needs
no tenant at all, such as resolving a tenant slug during login.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from platform_core.identity.principal import Tenant
from platform_core.settings import get_settings

logger = logging.getLogger("platform.db")

# The GUC name policies read. Kept here rather than inlined at call sites so a
# rename is one edit and a mismatch is impossible.
TENANT_GUC = "app.tenant_id"


@lru_cache(maxsize=4)
def _engine(role: str = "app") -> Engine:
    settings = get_settings()
    url = str({
        "app": settings.database_url,
        "owner": settings.database_owner_url,
        "relay": settings.database_relay_url,
    }[role])

    engine = create_engine(
        url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_pre_ping=True,
        # Recycle below any server-side idle timeout so a stale socket surfaces
        # as a clean reconnect rather than as a mid-transaction failure.
        pool_recycle=1800,
        echo=False,
    )

    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_conn, _record) -> None:
        # A statement that outruns the latency budget is killed by the database
        # rather than left to hold a lease while the caller waits. Set at
        # connect time so it applies to every transaction on the connection.
        with dbapi_conn.cursor() as cur:
            cur.execute(f"SET statement_timeout = {settings.db_statement_timeout_ms}")
            cur.execute("SET idle_in_transaction_session_timeout = 30000")
    return engine


@lru_cache(maxsize=4)
def _session_factory(role: str = "app") -> sessionmaker[Session]:
    return sessionmaker(bind=_engine(role), expire_on_commit=False, future=True)


@contextmanager
def tenant_session(tenant: Tenant | uuid.UUID) -> Iterator[Session]:
    """A transaction scoped to one tenant, with RLS active.

    Commits on clean exit, rolls back on exception. The GUC is set with
    ``SET LOCAL`` **inside** the transaction, so it cannot outlive it and cannot
    follow the connection back into the pool.
    """
    tenant_id = tenant.id if isinstance(tenant, Tenant) else tenant
    session = _session_factory("app")()
    try:
        # `set_config(name, value, is_local => true)` rather than `SET LOCAL`.
        #
        # SET is a utility statement: Postgres does not accept bind parameters
        # in it, so `SET LOCAL app.tenant_id = :x` is a syntax error. The only
        # way to make the statement form work is to interpolate the value into
        # the SQL string — which puts a caller-supplied value into a statement
        # body, i.e. an injection point on the one control that everything else
        # here depends on.
        #
        # set_config() is an ordinary function call, so the value binds
        # normally, and `is_local => true` gives exactly SET LOCAL's semantics:
        # reverted at COMMIT or ROLLBACK, never carried back into the pool.
        session.execute(
            text("SELECT set_config(:guc, :tenant_id, true)"),
            {"guc": TENANT_GUC, "tenant_id": str(tenant_id)},
        )
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@contextmanager
def system_session(*, reason: str) -> Iterator[Session]:
    """A cross-tenant transaction. Narrow, explicit, and logged.

    For the outbox relay, the stale-claim reaper and platform-wide metrics —
    work that is genuinely not on behalf of one tenant. ``reason`` is mandatory
    and recorded, so every use of the escape hatch is greppable and reviewable.

    This still connects as ``platform_app``. It does not turn RLS off; it simply
    does not set a tenant, so it sees only what the migrations explicitly expose
    to the system role. Anything that needs to bypass a policy has to say so in
    a migration, where it can be reviewed.
    """
    logger.info("system_session opened: %s", reason)
    session = _session_factory("app")()
    try:
        session.execute(
            text("SELECT set_config('app.system_reason', :reason, true)"),
            {"reason": reason[:200]},
        )
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@contextmanager
def owner_session() -> Iterator[Session]:
    """Migration-only session as the table owner. Bypasses RLS — never at runtime.

    Guarded rather than merely documented: a process that is not running
    migrations has no business holding this, and the check makes an accidental
    import-and-use fail immediately instead of quietly running unscoped.
    """
    settings = get_settings()
    if settings.service_role != "test" and settings.environment != "local":
        raise RuntimeError(
            f"owner_session() requested by service_role={settings.service_role!r} in "
            f"{settings.environment!r}. The owner role bypasses row-level security; "
            f"runtime code must use tenant_session() or system_session()."
        )
    session = _session_factory("owner")()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@contextmanager
def relay_session(*, reason: str) -> Iterator[Session]:
    """Cross-tenant transaction for the outbox relay and the lease reaper.

    Connects as ``platform_relay``, a role whose policies permit every tenant's
    rows in exactly three tables — ``outbox``, ``side_effect`` and ``run`` — and
    nothing else. It is not a superuser and does not hold BYPASSRLS, so the
    boundary on every other table still applies to it.

    The privilege is the credential. The API process is configured only with the
    ``platform_app`` DSN, so no amount of calling this from the wrong place can
    grant it cross-tenant access — it simply cannot authenticate as this role.
    """
    logger.info("relay_session opened: %s", reason)
    session = _session_factory("relay")()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def current_tenant(session: Session) -> uuid.UUID | None:
    """What the database thinks the tenant is. Used by tests and health checks."""
    raw = session.execute(
        text(f"SELECT current_setting('{TENANT_GUC}', true)")
    ).scalar_one_or_none()
    return uuid.UUID(raw) if raw else None


def reset_engines() -> None:
    """Drop cached engines. For tests that change settings between cases."""
    for cached in (_engine, _session_factory):
        cached.cache_clear()
