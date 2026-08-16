"""Login, and reading back the identity a token carries.

``/auth/login`` is the only route that must work without a token. ``/auth/me``
is its counterpart: guarded like everything else, and the route a console calls
on load to find out whether a persisted token is still valid and what its holder
may do.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from platform_core.api.deps import get_context
from platform_core.identity.auth import AuthenticationError, authenticate, issue_token
from platform_core.identity.capabilities import capabilities_of
from platform_core.identity.principal import RequestContext
from platform_core.observability import audit
from platform_core.observability.telemetry import record_admission_decision, record_auth_attempt
from platform_core.security.rate_limit import RateLimitUnavailable, get_rate_limiter
from platform_core.settings import get_settings

logger = logging.getLogger("platform.api.auth")
router = APIRouter(tags=["auth"])


class LoginRequest(BaseModel):
    # The tenant is part of the credential, not something chosen after login.
    # Principals are unique within a tenant, so identity is the pair.
    tenant: str = Field(min_length=1, max_length=64)
    subject: str = Field(min_length=1, max_length=320)
    password: str = Field(min_length=1, max_length=1024)
    # Browser clients request an HttpOnly session cookie and do not receive a
    # JavaScript-readable bearer token. Programmatic clients retain the normal
    # token response by leaving this false.
    browser_session: bool = False


class LoginResponse(BaseModel):
    access_token: str | None
    token_type: str = "bearer"
    tenant: str
    subject: str
    roles: list[str]


@router.post("/auth/login", response_model=LoginResponse)
def login(payload: LoginRequest, request: Request, response: Response) -> LoginResponse:
    """Exchange credentials for a token that carries the tenant.

    Every failure returns the same 401 with the same body. Distinguishing
    "no such tenant" from "no such user" from "wrong password" hands an attacker
    a working enumeration oracle, and the timing is equalised in
    ``authenticate`` for the same reason.
    """
    settings = get_settings()
    client_ip = request.client.host if request.client else "unknown"
    try:
        decision = get_rate_limiter().check(
            "login",
            f"{client_ip}:{payload.tenant.casefold()}:{payload.subject.casefold()}",
            limit=settings.login_attempts_per_window,
            window_seconds=settings.login_window_seconds,
        )
    except RateLimitUnavailable:
        record_admission_decision("login", "unavailable")
        record_auth_attempt("unavailable")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication admission control is temporarily unavailable.",
            headers={"Retry-After": "5"},
        ) from None
    if not decision.allowed:
        record_admission_decision("login", "denied")
        record_auth_attempt("rate_limited")
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many authentication attempts.",
            headers={
                "Retry-After": str(decision.retry_after_seconds),
                "RateLimit-Limit": str(decision.limit),
                "RateLimit-Remaining": "0",
            },
        )
    record_admission_decision("login", "allowed")

    try:
        principal = authenticate(payload.tenant, payload.subject, payload.password)
    except AuthenticationError:
        # Do not put an email address and tenant name into the unauthenticated
        # log stream. The limiter has a keyed pseudonymous correlation key.
        logger.info("failed login from client=%s", client_ip)
        record_auth_attempt("failed")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication failed.",
            headers={"WWW-Authenticate": "Bearer"},
        ) from None

    try:
        audit.record(
            None,
            principal=principal,
            action="authentication.login",
            outcome=audit.Outcome.SUCCEEDED,
            resource_type="principal",
            resource_id=str(principal.id),
            required=settings.audit_fail_closed,
        )
    except audit.AuditUnavailable:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Mandatory audit logging is temporarily unavailable.",
            headers={"Retry-After": "5"},
        ) from None
    record_auth_attempt("succeeded")
    token = issue_token(principal)
    if payload.browser_session:
        response.set_cookie(
            key=settings.auth_cookie_name,
            value=token,
            max_age=settings.access_token_ttl_seconds,
            httponly=True,
            secure=settings.environment in {"staging", "production"},
            samesite="strict",
            path="/",
        )

    return LoginResponse(
        access_token=None if payload.browser_session else token,
        tenant=principal.tenant.slug,
        subject=principal.subject,
        roles=sorted(str(r) for r in principal.roles),
    )


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(response: Response, ctx: Annotated[RequestContext, Depends(get_context)]) -> None:
    """End the browser session by removing its HttpOnly cookie."""
    settings = get_settings()
    response.delete_cookie(
        key=settings.auth_cookie_name,
        httponly=True,
        secure=settings.environment in {"staging", "production"},
        samesite="strict",
        path="/",
    )
    audit.record(
        ctx,
        action="authentication.logout",
        outcome=audit.Outcome.SUCCEEDED,
        resource_type="principal",
        resource_id=str(ctx.principal.id),
    )


class MeResponse(BaseModel):
    principal_id: str
    tenant: str
    subject: str
    roles: list[str]
    actor_type: str
    # The console gates its own controls on this. Sending it means the UI does
    # not have to re-derive authority from role names — a second copy of the
    # policy that would drift from ROLE_CAPABILITIES the first time it changed.
    #
    # This is a convenience for rendering, never the enforcement point: every
    # action is still checked server-side by the middleware. A client that
    # ignores this list gets 403s, not access.
    capabilities: list[str]


@router.get("/auth/me", response_model=MeResponse)
def me(ctx: Annotated[RequestContext, Depends(get_context)]) -> MeResponse:
    """Who this token belongs to, and what its roles permit.

    Guarded by ``SESSION_READ`` rather than left public. A public variant would
    have to parse the bearer token itself, which means a second token-decoding
    path beside the middleware's — and the softer of two auth paths is the one
    that eventually gets used.

    Only role-derived capabilities are listed. Resource-scoped grants are
    deliberately excluded: they answer "may this actor do X *to that
    collection*", which is a per-object question, and flattening them into one
    global list would let the console show an action as universally available
    when the grant covers a single resource.
    """
    principal = ctx.principal
    return MeResponse(
        principal_id=str(principal.id),
        tenant=principal.tenant.slug,
        subject=principal.subject,
        roles=sorted(str(r) for r in principal.roles),
        actor_type=str(principal.actor_type),
        capabilities=sorted(str(c) for c in capabilities_of(principal)),
    )
