"""The HTTP surface. Default-deny, enforced before any handler runs.

Authorisation happens in one place — :func:`_authorize` — and it matches against
the **route table** (see :mod:`platform_core.api.route_table`), never against the
raw URL string. Matching raw paths means an attacker controls the string being
compared, and prefix rules written that way have a long history of being bypassed
by encoding, traversal or a trailing slash. Matching a compiled route regex means
`/api/runs/{run_id}` is checked as a route, whatever the id happens to be.

Read `route_table.py` before changing anything here: enumerating routes the
obvious way silently produced an empty list under this FastAPI version, which
turned this entire middleware into a no-op.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from platform_core.api.policy import is_public, policy_for, undeclared_routes
from platform_core.api.route_table import ROUTERS, build_route_table, registered_pairs
from platform_core.identity.auth import (
    AuthenticationBackendUnavailable,
    AuthenticationError,
    decode_token,
    token_from_header,
)
from platform_core.identity.capabilities import Capability, allows
from platform_core.identity.principal import RequestContext
from platform_core.observability import audit
from platform_core.observability.telemetry import (
    bind_request_context,
    configure_telemetry,
    instrument_fastapi,
    record_admission_decision,
    record_http,
    shutdown_telemetry,
)
from platform_core.security.rate_limit import RateLimitUnavailable, get_rate_limiter
from platform_core.settings import get_settings, require_coherent_settings

logger = logging.getLogger("platform.api")

# Reads are visible in access telemetry. Mutating and privileged decisions also
# enter the tenant's tamper-evident audit chain before the handler is admitted.
_AUDITED_CAPABILITIES = frozenset({
    Capability.DOCUMENT_INGEST,
    Capability.DOCUMENT_DELETE,
    Capability.DATA_ERASE,
    Capability.RUN_CANCEL,
    Capability.EVAL_RUN,
    Capability.SCHEMA_AUTHOR,
    Capability.SCHEMA_APPROVE,
    Capability.BUDGET_MANAGE,
    Capability.MEMBER_MANAGE,
    Capability.RELEASE_PROMOTE,
    Capability.TOOL_INVOKE_WRITE,
    Capability.TOOL_APPROVE,
})


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = require_coherent_settings()
    configure_telemetry(settings)
    table = app.state.route_table

    # Refuse to start with an undeclared route. Checking at startup rather than
    # on first request means an unguarded route cannot reach a running service:
    # a check that only fires when someone happens to call the route is a check
    # that fires after the incident.
    missing = undeclared_routes(registered_pairs(table))
    if missing:
        raise RuntimeError(
            "routes registered without an authorisation policy — every route must "
            "either be listed in PUBLIC_PATHS or have a declared capability:\n"
            + "\n".join(f"  {m} {p}" for m, p in missing)
        )

    public = sum(1 for entry in table if is_public(entry.template))
    logger.info(
        "%s ready: %d routes (%d public, %d guarded), release=%s",
        settings.service_name, len(table), public, len(table) - public, settings.release,
    )
    try:
        yield
    finally:
        shutdown_telemetry()


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="local-platform",
        version=settings.release,
        lifespan=lifespan,
        # The interactive docs enumerate every route and schema to an
        # unauthenticated caller. Fine locally; disabled elsewhere here rather
        # than left to a deployment checklist.
        docs_url="/docs" if settings.environment == "local" else None,
        redoc_url="/redoc" if settings.environment == "local" else None,
        openapi_url="/openapi.json" if settings.environment == "local" else None,
    )

    for router in ROUTERS:
        app.include_router(router)

    # Built from the same routers that were just included, so the table cannot
    # describe a different surface from the one being served.
    app.state.route_table = build_route_table()

    @app.middleware("http")
    async def authorize(request: Request, call_next: Callable[[Request], Awaitable]):
        started = time.monotonic()
        try:
            response = await _authorize(request, call_next)
        except Exception:
            route = getattr(request.state, "route_template", "unmatched")
            record_http(request.method, route, 500, (time.monotonic() - started) * 1000)
            raise
        route = getattr(request.state, "route_template", "unmatched")
        record_http(
            request.method,
            route,
            response.status_code,
            (time.monotonic() - started) * 1000,
        )
        _add_security_headers(response)
        return response

    instrument_fastapi(app)

    return app


async def _authorize(request: Request, call_next):
    settings = get_settings()
    method = request.method

    content_length = request.headers.get("content-length")
    if content_length:
        try:
            declared_size = int(content_length)
        except ValueError:
            return JSONResponse(
                status_code=400,
                content={"detail": "Invalid Content-Length.", "code": "invalid_request"},
            )
        if declared_size < 0 or declared_size > settings.max_request_body_bytes:
            return JSONResponse(
                status_code=413,
                content={"detail": "Request body is too large.", "code": "body_too_large"},
            )

    if method == "OPTIONS":
        return await call_next(request)

    table = request.app.state.route_table
    entry = None
    path_params: dict = {}
    path_exists = False

    for candidate in table:
        params = candidate.match(request.scope)
        if params is None:
            continue
        path_exists = True
        if candidate.method == method:
            entry, path_params = candidate, params
            break

    if entry is None:
        # Either no such path, or the path exists with a different method. Let
        # the router answer (404 or 405). Returning 401/403 here would make the
        # authorisation layer a route-existence oracle for anonymous callers.
        del path_exists
        return await call_next(request)

    request.state.route_template = entry.template

    if is_public(entry.template):
        return await call_next(request)

    principal = None
    token = token_from_header(request.headers.get("authorization"))
    cookie_authenticated = False
    if token is None:
        token = request.cookies.get(settings.auth_cookie_name)
        cookie_authenticated = token is not None
    if token:
        try:
            principal = decode_token(token)
        except AuthenticationError:
            principal = None
        except AuthenticationBackendUnavailable:
            return JSONResponse(
                status_code=503,
                content={
                    "detail": "Identity verification is temporarily unavailable.",
                    "code": "identity_unavailable",
                },
                headers={"Retry-After": "5"},
            )

    if principal is None:
        return JSONResponse(
            status_code=401,
            content={"detail": "Authentication required.", "code": "unauthenticated"},
            headers={"WWW-Authenticate": "Bearer"},
        )

    request_id = uuid.uuid4()
    decision_ctx = RequestContext(
        principal=principal,
        request_id=request_id,
        idempotency_key=request.headers.get("idempotency-key"),
        labels={"route": entry.template, "method": method},
    )

    # HttpOnly cookies remove bearer tokens from JavaScript storage, but cookie
    # authentication must reject cross-origin state changes. Bearer clients are
    # not subject to CSRF because another origin cannot manufacture the header.
    if cookie_authenticated and method not in {"GET", "HEAD", "OPTIONS"}:
        origin = request.headers.get("origin")
        if origin not in settings.browser_allowed_origins:
            audit.record(
                decision_ctx,
                action="authorization.csrf",
                outcome=audit.Outcome.DENIED,
                resource_type="route",
                resource_id=entry.template,
                detail={"method": method, "reason": "origin not allowed"},
            )
            return JSONResponse(
                status_code=403,
                content={"detail": "Cross-origin request refused.", "code": "csrf_refused"},
                headers={"X-Request-ID": str(request_id)},
            )

    if settings.rate_limit_enabled:
        subject = f"{principal.tenant.id}:{principal.id}"
        try:
            decision = get_rate_limiter().check(
                "authenticated",
                subject,
                limit=settings.authenticated_requests_per_minute,
                window_seconds=60,
            )
        except RateLimitUnavailable:
            record_admission_decision("authenticated", "unavailable")
            return JSONResponse(
                status_code=503,
                content={
                    "detail": "Admission control is temporarily unavailable.",
                    "code": "admission_control_unavailable",
                },
                headers={"Retry-After": "5"},
            )
        if not decision.allowed:
            record_admission_decision("authenticated", "denied")
            audit.record(
                decision_ctx,
                action="admission.rate_limit",
                outcome=audit.Outcome.DENIED,
                resource_type="route",
                resource_id=entry.template,
                detail={"method": method},
            )
            return JSONResponse(
                status_code=429,
                content={"detail": "Request rate exceeded.", "code": "rate_limited"},
                headers={
                    "Retry-After": str(decision.retry_after_seconds),
                    "RateLimit-Limit": str(decision.limit),
                    "RateLimit-Remaining": "0",
                    "X-Request-ID": str(request_id),
                },
            )
        record_admission_decision("authenticated", "allowed")

    policy = policy_for(method, entry.template)
    if policy is None:
        # Startup should have caught this. Refusing here too means a route added
        # after boot still cannot slip through with no decision behind it.
        logger.error("undeclared route reached the middleware: %s %s", method, entry.template)
        return JSONResponse(
            status_code=403,
            content={
                "detail": "This route has no authorisation policy and is refused.",
                "code": "undeclared_route",
            },
        )

    resource = None
    if policy.resource_param:
        if policy.resource_location == "body":
            try:
                body = await request.body()
                if len(body) > settings.max_request_body_bytes:
                    return JSONResponse(
                        status_code=413,
                        content={
                            "detail": "Request body is too large.",
                            "code": "body_too_large",
                        },
                    )
                payload = await request.json()
                resource = payload.get(policy.resource_param) if isinstance(payload, dict) else None
            except (UnicodeDecodeError, ValueError):
                resource = None
        else:
            resource = path_params.get(policy.resource_param) or request.query_params.get(
                policy.resource_param
            )
        resource = str(resource) if resource is not None else None

    permitted, reason = allows(principal, policy.capability, resource=resource)
    if not permitted:
        # A non-privileged caller reaching a privileged route is either a
        # misconfigured client or someone probing. Both are worth seeing.
        logger.warning(
            "denied %s %s for %s (tenant=%s, capability=%s, reason=%s)",
            method, entry.template, principal.subject, principal.tenant.slug,
            policy.capability, reason,
        )
        audit.record(
            decision_ctx,
            action="authorization.decision",
            outcome=audit.Outcome.DENIED,
            resource_type="route",
            resource_id=entry.template,
            detail={
                "method": method,
                "capability": str(policy.capability),
                "reason": reason,
                "resource": resource,
            },
        )
        return JSONResponse(
            status_code=403,
            content={"detail": f"This action requires {policy.capability}.", "code": "forbidden"},
            headers={"X-Request-ID": str(request_id)},
        )

    if policy.capability in _AUDITED_CAPABILITIES:
        try:
            audit.record(
                decision_ctx,
                action="authorization.decision",
                outcome=audit.Outcome.ALLOWED,
                resource_type="route",
                resource_id=entry.template,
                detail={
                    "method": method,
                    "capability": str(policy.capability),
                    "reason": reason,
                    "resource": resource,
                },
                required=settings.audit_fail_closed,
            )
        except audit.AuditUnavailable:
            return JSONResponse(
                status_code=503,
                content={
                    "detail": "Mandatory audit logging is temporarily unavailable.",
                    "code": "audit_unavailable",
                },
                headers={"Retry-After": "5", "X-Request-ID": str(request_id)},
            )

    # Handlers never construct a context, so a handler cannot invent a tenant or
    # omit an identity. The tenant comes from the signed token and nowhere else.
    request.state.context = decision_ctx
    bind_request_context(request.state.context)
    response = await call_next(request)
    response.headers["X-Request-ID"] = str(request_id)
    if settings.rate_limit_enabled:
        response.headers["RateLimit-Limit"] = str(decision.limit)
        response.headers["RateLimit-Remaining"] = str(decision.remaining)
    return response


def _add_security_headers(response: Response) -> None:
    settings = get_settings()
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault(
        "Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()"
    )
    response.headers.setdefault("Cache-Control", "no-store")
    if settings.environment in {"staging", "production"}:
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=63072000; includeSubDomains; preload"
        )
        response.headers.setdefault(
            "Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'"
        )


app = create_app()
