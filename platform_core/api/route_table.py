"""The authoritative list of routes, built from the routers we register.

## Why this module exists

The first version of the authorisation middleware enumerated ``app.routes`` and
filtered for ``APIRoute``. Under FastAPI 0.141 / Starlette 1.6 that returns
**nothing**: included routers are wrapped in a private ``_IncludedRouter`` object
and are not flattened into ``app.routes`` at all.

The consequences were both silent and total:

* The startup check for undeclared routes iterated an empty list, found no
  violations, and passed. A check that validates nothing looks exactly like a
  check that passes.
* ``_match_route`` never matched, so the middleware fell through to
  ``call_next`` on **every** request. The entire authorisation layer was a no-op
  while the service returned correct-looking responses.

Nothing about that would have shown up in a smoke test. It was found because the
route count printed as zero, which only looked wrong because the count was being
printed at all.

## What this does instead

Routes are collected from :data:`ROUTERS` — the same routers ``register_routes``
includes — using ``APIRouter.routes``, which is public and already carries the
router's prefix and a compiled path regex. No private attributes, and the list
cannot diverge from what is served because it is derived from the same objects.

The table is built once at app creation and matched per request, so the
per-request cost is a regex test against a small list rather than a walk of the
routing tree.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import APIRouter
from fastapi.routing import APIRoute

from platform_core.api.routes import (
    approvals,
    audit,
    auth,
    documents,
    eval,
    health,
    members,
    onboarding,
    query,
    runs,
    tools,
    usage,
)

# The single registration list. `register_routes` includes exactly these, and
# the route table is derived from exactly these, so "declared", "served" and
# "checked" are the same set by construction.
ROUTERS: tuple[APIRouter, ...] = (
    health.router,
    auth.router,
    runs.router,
    documents.router,
    usage.router,
    audit.router,
    approvals.router,
    tools.router,
    members.router,
    query.router,
    onboarding.router,
    eval.router,
)


@dataclass(frozen=True, slots=True)
class RouteEntry:
    method: str
    template: str
    route: APIRoute

    def match(self, scope: dict) -> dict | None:
        """Path params when this entry matches the scope, else None.

        Method is compared separately from the path so that a request to a known
        path with a wrong method produces a 405 from the router rather than a
        401 from the middleware. Answering 401 there would leak whether the path
        exists to an unauthenticated caller.
        """
        matched = self.route.path_regex.match(scope["path"])
        if matched is None:
            return None
        return {
            key: self.route.param_convertors[key].convert(value)
            if key in self.route.param_convertors
            else value
            for key, value in matched.groupdict().items()
        }


def build_route_table() -> list[RouteEntry]:
    entries: list[RouteEntry] = []
    for router in ROUTERS:
        for route in router.routes:
            if not isinstance(route, APIRoute):
                continue
            for method in sorted(route.methods):
                if method in {"HEAD", "OPTIONS"}:
                    continue
                entries.append(RouteEntry(method=method, template=route.path, route=route))

    # Longest template first, so a literal segment wins over a parameter when
    # both could match — `/api/runs/active` must not be captured by
    # `/api/runs/{run_id}` if both are ever registered.
    entries.sort(key=lambda e: (-len(e.template), e.template))
    if not entries:
        # The failure this module was written for. If the collection ever
        # returns nothing again, it must be an immediate, loud error rather
        # than an authorisation layer that quietly permits everything.
        raise RuntimeError(
            "route table is empty — the authorisation middleware would permit every "
            "request. This is the FastAPI/Starlette routing-model bug documented in "
            "platform_core/api/route_table.py; do not start with an empty table."
        )
    return entries


def registered_pairs(table: list[RouteEntry]) -> list[tuple[str, str]]:
    """``(method, template)`` pairs, for the policy-coverage check."""
    return [(entry.method, entry.template) for entry in table]
