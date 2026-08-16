"""FastAPI dependencies.

Separate from ``app.py`` on purpose. When ``get_context`` lived beside the app
factory, every route module imported ``platform_core.api.app`` and the factory
imported the route modules — a cycle whose symptom depended on which side was
imported first: an ``ImportError`` from one direction, and an app with **zero
routes and no error at all** from the other. The silent variant is the dangerous
one, because a service with no routes still starts, still passes a liveness
probe, and answers 404 to everything.

Dependencies belong in a leaf module that imports neither the app nor the
routes. That makes the cycle impossible rather than merely currently-absent.
"""

from __future__ import annotations

from fastapi import HTTPException, Request

from platform_core.identity.principal import Principal, RequestContext


def get_context(request: Request) -> RequestContext:
    """The context the authorisation middleware attached.

    Never absent on a guarded route: the middleware either builds it or returns
    401/403 before the handler runs. The 500 below is therefore a real invariant
    violation — a route reachable without passing the middleware — and it should
    be loud rather than degrade into an unauthenticated request.
    """
    context = getattr(request.state, "context", None)
    if context is None:
        raise HTTPException(
            status_code=500,
            detail="Request context missing — this route bypassed the authorisation "
                   "middleware, which is a platform bug, not a client error.",
        )
    return context


def get_principal(request: Request) -> Principal:
    return get_context(request).principal
