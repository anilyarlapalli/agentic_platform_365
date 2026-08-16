"""Conversation state in Postgres, so any replica can serve any turn.

## The defect this removes

`_Singleton.sessions` in the Azure build is a plain dict on the replica. The API
runs `max-replicas 3` with no `--session-affinity` in `deploy.sh`, so a user's
second turn lands on a different replica roughly two thirds of the time and finds
an empty `ChatState`. It is the platform defect its users would actually notice,
and it exists purely because the state is in the process.

Sticky affinity would paper over it and introduce two new problems: a replica
restart still drops every conversation pinned to it, and load stops being evenly
distributable. Moving the state out solves both, and makes a rolling deploy
transparent instead of conversation-dropping.

## Binding, not just keying

A session belongs to `(tenant, principal)`, not to a session id. In the Azure
build sessions are keyed `(domain, session_id)` with no user binding at all, and
`GET /api/sessions/{session_id}` carries no auth dependency — so anyone who can
reach the ingress and guess an id reads someone else's conversation, including
content retrieved under *their* document grants.

Here the tenant is enforced by row-level security and the principal is checked on
every load, so possessing an id is not sufficient.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text

from platform_core.db.engine import relay_session, tenant_session
from platform_core.identity.principal import RequestContext

logger = logging.getLogger("platform.scaling.sessions")

DEFAULT_TTL = timedelta(hours=12)


class SessionNotFound(LookupError):
    """No such session for this principal.

    One error for "absent" and "belongs to someone else", for the same reason
    the ports layer refuses to distinguish them: telling them apart confirms that
    an id exists, which is an existence oracle across the boundary.
    """


@dataclass(frozen=True, slots=True)
class Session:
    id: str
    tenant_id: uuid.UUID
    principal_id: uuid.UUID
    workload: str
    state: dict[str, Any]
    turn_count: int
    updated_at: datetime
    expires_at: datetime


def create(ctx: RequestContext, *, workload: str, ttl: timedelta = DEFAULT_TTL,
           session_id: str | None = None) -> Session:
    sid = session_id or uuid.uuid4().hex
    expires = datetime.now(UTC) + ttl

    with tenant_session(ctx.tenant) as s:
        row = s.execute(
            text(
                "INSERT INTO session (id, tenant_id, principal_id, workload, state, "
                "  expires_at) VALUES (:id, :t, :p, :w, '{}'::jsonb, :exp) "
                "RETURNING id, tenant_id, principal_id, workload, state, turn_count, "
                "  updated_at, expires_at"
            ),
            {"id": sid, "t": ctx.tenant.id, "p": ctx.principal.id, "w": workload,
             "exp": expires},
        ).one()
    return _to_session(row)


def load(ctx: RequestContext, session_id: str) -> Session:
    """Fetch a session. Raises unless it belongs to this principal and is live."""
    with tenant_session(ctx.tenant) as s:
        row = s.execute(
            text(
                "SELECT id, tenant_id, principal_id, workload, state, turn_count, "
                "  updated_at, expires_at FROM session "
                "WHERE id = :id AND principal_id = :p AND expires_at > now()"
            ),
            {"id": session_id, "p": ctx.principal.id},
        ).one_or_none()
    if row is None:
        raise SessionNotFound(session_id)
    return _to_session(row)


def append_turn(ctx: RequestContext, session_id: str, turn: dict[str, Any],
                *, max_turns: int = 50) -> Session:
    """Append a turn atomically, in the database.

    Read-modify-write in the application would lose a turn whenever a user sends
    two messages quickly and they land on different replicas — the exact
    concurrency the out-of-process design is meant to make safe. Doing the append
    in SQL makes the update atomic without a lock the caller has to remember.

    History is trimmed to the most recent ``max_turns`` so a long conversation
    cannot grow a row without bound.
    """
    with tenant_session(ctx.tenant) as s:
        row = s.execute(
            text(
                "UPDATE session SET "
                # CAST(:turn AS jsonb), never `:turn::jsonb` — SQLAlchemy's bind
                # syntax claims the first colon of a `::` cast and the parameter
                # name comes out as `turn:`. Third occurrence of this collision
                # in the codebase; `tests/properties/test_sql_hygiene.py` now
                # fails the build on the pattern rather than waiting for the
                # next one.
                "  state = jsonb_set(state, '{turns}', "
                "    (SELECT to_jsonb(array_agg(t)) FROM ("
                "       SELECT t FROM jsonb_array_elements("
                "         coalesce(state->'turns', '[]'::jsonb) || CAST(:turn AS jsonb)) AS t "
                "       OFFSET greatest(0, "
                "         jsonb_array_length(coalesce(state->'turns', '[]'::jsonb)) + 1 - :max)"
                "     ) trimmed), true), "
                "  turn_count = turn_count + 1, updated_at = now() "
                "WHERE id = :id AND principal_id = :p AND expires_at > now() "
                "RETURNING id, tenant_id, principal_id, workload, state, turn_count, "
                "  updated_at, expires_at"
            ),
            {"id": session_id, "p": ctx.principal.id,
             "turn": json.dumps([turn]), "max": max_turns},
        ).one_or_none()
    if row is None:
        raise SessionNotFound(session_id)
    return _to_session(row)


def delete(ctx: RequestContext, session_id: str) -> bool:
    with tenant_session(ctx.tenant) as s:
        deleted = s.execute(
            text(
                "DELETE FROM session WHERE id = :id AND principal_id = :p RETURNING id"
            ),
            {"id": session_id, "p": ctx.principal.id},
        ).scalar_one_or_none()
    return deleted is not None


def purge_expired() -> int:
    """Remove lapsed sessions. Cross-tenant housekeeping, run by the relay."""
    with relay_session(reason="retention: purge expired sessions") as session:
        return int(
            session.execute(
                text("SELECT platform_purge_expired_sessions()")
            ).scalar_one()
        )


def _to_session(row) -> Session:
    return Session(
        id=row.id, tenant_id=row.tenant_id, principal_id=row.principal_id,
        workload=row.workload, state=row.state, turn_count=row.turn_count,
        updated_at=row.updated_at, expires_at=row.expires_at,
    )
