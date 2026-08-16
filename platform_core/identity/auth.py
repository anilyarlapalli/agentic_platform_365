"""Authentication: proving who is calling, and for which tenant.

Tokens carry the tenant. Not as a convenience — as the fix for a specific class
of bug. If the tenant were supplied by the caller as a parameter or a header,
every route would have to check that the caller is entitled to the tenant it
named, and the first route that forgets is a full cross-tenant breach. Binding
the tenant into the signed token means there is nothing to forget: the claim is
either signed or the token is rejected.

Password hashing is Argon2id, which is memory-hard and therefore actually costly
to attack in parallel on a GPU. The parameters below are the argon2-cffi
defaults, which track the RFC 9106 recommendations; they are stated explicitly
rather than inherited so that a library default changing is a visible diff.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from platform_core.db.engine import system_session, tenant_session
from platform_core.identity.principal import ActorType, Principal, Role, Tenant
from platform_core.settings import get_settings

# time_cost=3, memory_cost=64MiB, parallelism=4 — argon2-cffi's defaults, which
# follow RFC 9106's second recommended configuration.
_hasher = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=4)


class AuthenticationError(Exception):
    """Credentials are absent, malformed, expired, or wrong.

    One error for all four on purpose. Distinguishing "no such user" from "wrong
    password" is a user-enumeration oracle, and distinguishing "expired" from
    "invalid signature" tells an attacker whether a forged token's structure was
    otherwise acceptable.
    """


class AuthenticationBackendUnavailable(RuntimeError):
    """Identity state could not be checked, so authentication cannot proceed."""


def hash_password(password: str) -> str:
    if len(password) < 12:
        raise ValueError("password must be at least 12 characters")
    return _hasher.hash(password)


def verify_password(stored_hash: str, password: str) -> tuple[bool, str | None]:
    """Verify, and report a rehash when the parameters have moved on.

    Returns ``(ok, new_hash_or_None)``. Rehashing on successful login is how a
    parameter upgrade reaches existing users at all — otherwise the cost factors
    are only ever applied to accounts created after the change.
    """
    try:
        _hasher.verify(stored_hash, password)
    except (VerifyMismatchError, InvalidHashError):
        return False, None
    if _hasher.check_needs_rehash(stored_hash):
        return True, _hasher.hash(password)
    return True, None


def issue_token(principal: Principal, *, ttl: timedelta | None = None) -> str:
    """Mint an access token binding the principal to its tenant."""
    settings = get_settings()
    now = datetime.now(UTC)
    expires = now + (ttl or timedelta(seconds=settings.access_token_ttl_seconds))

    claims: dict[str, Any] = {
        "sub": str(principal.id),
        "tid": str(principal.tenant.id),
        "tsl": principal.tenant.slug,
        "sbj": principal.subject,
        "rol": sorted(str(r) for r in principal.roles),
        "typ": str(principal.actor_type),
        "iat": int(now.timestamp()),
        "nbf": int(now.timestamp()),
        "exp": int(expires.timestamp()),
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        # Unique per token so an individual token can be revoked without
        # invalidating every token the principal holds.
        "jti": str(uuid.uuid4()),
    }
    return jwt.encode(claims, settings.jwt_secret.get_secret_value(),
                      algorithm=settings.jwt_algorithm)


def decode_token(token: str) -> Principal:
    """Verify a token and rebuild the principal from its claims.

    ``algorithms`` is pinned to a single value. Passing the token's own ``alg``
    header back to the verifier is the classic JWT confusion attack: a token
    forged with ``alg: none``, or an RS256 verifier tricked into treating the
    public key as an HMAC secret. The library will not do it if it is never
    given the option.
    """
    settings = get_settings()
    try:
        claims = jwt.decode(
            token,
            settings.jwt_secret.get_secret_value(),
            algorithms=[settings.jwt_algorithm],
            audience=settings.jwt_audience,
            issuer=settings.jwt_issuer,
            options={
                "require": [
                    "exp", "iat", "nbf", "iss", "aud", "jti",
                    "sub", "tid", "tsl", "sbj", "typ",
                ]
            },
        )
    except jwt.PyJWTError as exc:
        raise AuthenticationError("invalid or expired token") from exc

    try:
        tenant = Tenant(id=uuid.UUID(claims["tid"]), slug=claims["tsl"])
        principal_id = uuid.UUID(claims["sub"])
        token_actor_type = ActorType(claims["typ"])
    except (KeyError, ValueError) as exc:
        raise AuthenticationError("token claims are malformed") from exc

    if not settings.verify_principal_state:
        try:
            return Principal(
                id=principal_id,
                tenant=tenant,
                subject=claims["sbj"],
                roles=frozenset(Role(r) for r in claims.get("rol", [])),
                actor_type=token_actor_type,
            )
        except ValueError as exc:
            raise AuthenticationError("token claims are malformed") from exc

    # The signature proves who issued the token; current database state decides
    # whether that identity is still enabled and what it may do now. This makes
    # disables and role removals effective immediately rather than after TTL.
    try:
        with tenant_session(tenant) as session:
            row = session.execute(
                text(
                    "SELECT subject, roles, actor_type, disabled_at "
                    "FROM principal WHERE id = :id"
                ),
                {"id": principal_id},
            ).one_or_none()
    except SQLAlchemyError as exc:
        raise AuthenticationBackendUnavailable("identity state is unavailable") from exc

    if row is None or row.disabled_at is not None:
        raise AuthenticationError("invalid or expired token")
    if row.subject != claims["sbj"] or ActorType(row.actor_type) != token_actor_type:
        raise AuthenticationError("token identity is stale")

    try:
        roles = frozenset(Role(role) for role in (row.roles or []))
    except ValueError as exc:
        raise AuthenticationError("principal has an invalid role") from exc
    return Principal(
        id=principal_id,
        tenant=tenant,
        subject=row.subject,
        roles=roles,
        actor_type=ActorType(row.actor_type),
    )


def authenticate(tenant_slug: str, subject: str, password: str) -> Principal:
    """Resolve credentials to a principal, or raise.

    The tenant is looked up first because principals are unique *within* a
    tenant, not globally — two tenants may each have an ``admin@`` and they are
    different people. A globally unique subject would leak the existence of an
    account in another tenant through a signup collision.
    """
    with system_session(reason="authentication: resolve tenant by slug") as s:
        row = s.execute(
            text("SELECT id, slug FROM tenant WHERE slug = :slug"), {"slug": tenant_slug}
        ).one_or_none()
    if row is None:
        # Still pay the hashing cost, so a missing tenant and a wrong password
        # take the same time. Timing is an enumeration oracle too.
        _hasher.hash(password)
        raise AuthenticationError("authentication failed")

    tenant = Tenant(id=row.id, slug=row.slug)
    with tenant_session(tenant) as s:
        principal_row = s.execute(
            text(
                "SELECT id, subject, roles, password_hash, actor_type, disabled_at "
                "FROM principal WHERE subject = :subject"
            ),
            {"subject": subject},
        ).one_or_none()

    if principal_row is None or not principal_row.password_hash:
        _hasher.hash(password)
        raise AuthenticationError("authentication failed")

    ok, rehashed = verify_password(principal_row.password_hash, password)
    if not ok or principal_row.disabled_at is not None:
        raise AuthenticationError("authentication failed")

    if rehashed:
        with tenant_session(tenant) as s:
            s.execute(
                text("UPDATE principal SET password_hash = :h WHERE id = :id"),
                {"h": rehashed, "id": principal_row.id},
            )

    return Principal(
        id=principal_row.id,
        tenant=tenant,
        subject=principal_row.subject,
        roles=frozenset(Role(r) for r in (principal_row.roles or [])),
        actor_type=ActorType(principal_row.actor_type),
    )


def token_from_header(authorization: str | None) -> str | None:
    """Extract a bearer token. One parser, so no two paths disagree.

    The Azure build has this exact concern and solves it the same way — its
    request-level guard reuses ``api.auth``'s parsing rather than
    reimplementing it, precisely so a token-format change cannot leave one path
    accepting what the other rejects.
    """
    if not authorization:
        return None
    scheme, _, credentials = authorization.partition(" ")
    if scheme.lower() != "bearer" or not credentials.strip():
        return None
    return credentials.strip()
