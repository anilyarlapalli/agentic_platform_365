"""Route authorisation policy, declared in one table.

## Why the polarity matters

The Azure build protects `/api/ingest/*` and `/api/onboard/*` and leaves
`GET /api/sessions/{id}`, `POST /api/reset`, `GET /api/audit` and
`POST /api/domains/reload` with no auth dependency at all. That is not
carelessness — it is what an *enumerate-what-to-protect* model produces over
time. Every new route defaults to open, and staying secure requires remembering.

Its own onboarding middleware gets this right and says so: it allowlists
wrapper-owned prefixes and admin-gates everything else under `/api/onboard`,
specifically so that a route the parent adds later "is covered instead of
arriving unguarded". This module applies that polarity to the whole surface.

## The rules

1. A path matching :data:`PUBLIC_PATHS` is open. That list is short, explicit,
   and reviewed.
2. Every other path requires authentication.
3. Every other path additionally requires a **declared capability**. A route
   with no entry in :data:`ROUTE_CAPABILITIES` is *refused*, not allowed.

Rule 3 is the one that makes this hold up. Adding a route without deciding its
authority produces a 403 on first call — annoying in development, and exactly
what you want, because the alternative is a route that silently inherits
whatever the framework's default was.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from platform_core.identity.capabilities import Capability

# Open by design. Each entry is a decision, not an oversight.
PUBLIC_PATHS: frozenset[str] = frozenset({
    "/health",           # liveness; no tenant data
    "/health/ready",     # readiness; reports dependency state only
    "/auth/login",       # must be reachable without a token, definitionally
    "/docs",             # OpenAPI UI — see note in app.py about production
    "/openapi.json",
    "/redoc",
})


@dataclass(frozen=True, slots=True)
class RoutePolicy:
    capability: Capability
    # Where in the path the resource identifier sits, when the capability is
    # resource-scoped. Lets "reviewer for collection X" be expressed without a
    # bespoke check in the handler.
    resource_param: str | None = None
    # Resource-scoped grants must be evaluated against the value the handler
    # will actually use. Body means body only: falling back to a query string
    # would let `?collection=allowed` authorise a JSON body naming `forbidden`.
    resource_location: Literal["path_or_query", "body"] = "path_or_query"


# method + path template → required capability.
#
# Path templates use FastAPI's `{param}` syntax and are matched against the
# *route* rather than the raw URL, so `/api/runs/{run_id}` matches regardless of
# the id — an attacker cannot dodge the policy by varying the identifier.
ROUTE_CAPABILITIES: dict[tuple[str, str], RoutePolicy] = {
    # Guarded, not public: reading back an identity requires a valid token, and
    # the middleware is the only thing that decodes one.
    ("GET", "/auth/me"): RoutePolicy(Capability.SESSION_READ),
    ("POST", "/auth/logout"): RoutePolicy(Capability.SESSION_READ),

    ("GET", "/api/runs"): RoutePolicy(Capability.RUN_READ),
    ("GET", "/api/runs/{run_id}"): RoutePolicy(Capability.RUN_READ),
    ("POST", "/api/runs/{run_id}/cancel"): RoutePolicy(Capability.RUN_CANCEL),

    ("GET", "/api/documents"): RoutePolicy(Capability.DOCUMENT_READ),
    ("GET", "/api/documents/{document_id}"): RoutePolicy(Capability.DOCUMENT_READ),
    ("POST", "/api/documents"): RoutePolicy(
        Capability.DOCUMENT_INGEST,
        resource_param="collection",
        resource_location="body",
    ),
    ("DELETE", "/api/documents/{document_id}"): RoutePolicy(Capability.DOCUMENT_DELETE),
    ("DELETE", "/api/documents/{document_id}/purge"): RoutePolicy(Capability.DATA_ERASE),

    ("POST", "/api/query"): RoutePolicy(Capability.QUERY_READ),

    ("GET", "/api/usage"): RoutePolicy(Capability.USAGE_READ),
    ("PUT", "/api/usage/caps"): RoutePolicy(Capability.BUDGET_MANAGE),

    ("GET", "/api/audit"): RoutePolicy(Capability.AUDIT_READ),

    # Authentication is the route-level floor; the registry selects read or
    # write capability from the actual tool spec and checks resource grants.
    ("GET", "/api/tools"): RoutePolicy(Capability.SESSION_READ),
    ("POST", "/api/tools/{tool_name}/invoke"): RoutePolicy(Capability.SESSION_READ),
    ("GET", "/api/approvals"): RoutePolicy(Capability.TOOL_APPROVE),
    ("POST", "/api/approvals/{approval_id}/decide"): RoutePolicy(Capability.TOOL_APPROVE),

    ("GET", "/api/members"): RoutePolicy(Capability.MEMBER_MANAGE),
    ("POST", "/api/members/grants"): RoutePolicy(Capability.MEMBER_MANAGE),
    ("DELETE", "/api/members/grants/{grant_id}"): RoutePolicy(Capability.MEMBER_MANAGE),

    # Reading a score, producing one, and deciding what it means are three
    # authorities. Measuring is not deciding — an operator may run an eval,
    # because making people ask permission to measure is how measurement stops
    # happening — but only `release:promote` moves the baseline.
    ("GET", "/api/eval"): RoutePolicy(Capability.EVAL_READ),
    ("GET", "/api/eval/datasets/{name}"): RoutePolicy(Capability.EVAL_READ),
    ("GET", "/api/eval/runs/{run_id}"): RoutePolicy(Capability.EVAL_READ),
    ("GET", "/api/eval/continuous"): RoutePolicy(Capability.EVAL_READ),
    ("PUT", "/api/eval/continuous/{name}"): RoutePolicy(Capability.RELEASE_PROMOTE),
    ("POST", "/api/eval/run"): RoutePolicy(Capability.EVAL_RUN),
    # Writing the golden set is the same authority as moving the baseline, not a
    # lesser one: whoever can rewrite the questions can make any regression pass.
    # Gating it any lower would leave `release:promote` guarding a door with no
    # wall attached.
    ("PUT", "/api/eval/datasets/{name}"): RoutePolicy(Capability.RELEASE_PROMOTE),
    ("POST", "/api/eval/runs/{run_id}/promote"): RoutePolicy(
        Capability.RELEASE_PROMOTE
    ),
    # Drafting rewrites the expected answers, and reviewing decides which items
    # are scored and whether the set counts as ground truth. Both change what
    # "passing" means, so both carry the same authority as moving the baseline.
    # The corpus-gap backlog. Reading it is `eval:read` — it is a list of
    # things the corpus does not cover, which is operational information rather
    # than a transcript. Turning one into an eval item is a dataset write.
    ("GET", "/api/eval/gaps"): RoutePolicy(Capability.EVAL_READ),
    ("POST", "/api/eval/datasets/{name}/mine"): RoutePolicy(
        Capability.RELEASE_PROMOTE
    ),
    ("POST", "/api/eval/datasets/{name}/draft"): RoutePolicy(
        Capability.RELEASE_PROMOTE
    ),
    ("PUT", "/api/eval/datasets/{name}/items/{item_id}"): RoutePolicy(
        Capability.RELEASE_PROMOTE
    ),

    # Onboarding. Three authorities, deliberately not one: drafting spends real
    # budget and writes a proposal, approving is a different act by a different
    # person, and reading is neither. The Azure build gates all of onboarding on
    # a single `admin` role and then needs a side-channel reviewer table the
    # moment one action needs finer authority than that.
    ("POST", "/api/onboard/sessions"): RoutePolicy(Capability.SCHEMA_AUTHOR),
    ("GET", "/api/onboard/sessions"): RoutePolicy(Capability.SCHEMA_READ),
    ("GET", "/api/onboard/sessions/{session_id}"): RoutePolicy(Capability.SCHEMA_READ),
    # Editing the taxonomy is authoring it, so it takes the author capability
    # rather than the approver's — and `approve` then refuses the editor exactly
    # as it refuses the drafter. Gating this on `schema:approve` would have let
    # one principal write a taxonomy and sign it off alone.
    ("POST", "/api/onboard/sessions/{session_id}/schema"): RoutePolicy(
        Capability.SCHEMA_AUTHOR
    ),
    ("POST", "/api/onboard/sessions/{session_id}/approve"): RoutePolicy(
        Capability.SCHEMA_APPROVE
    ),
    ("POST", "/api/onboard/sessions/{session_id}/publish"): RoutePolicy(
        Capability.SCHEMA_APPROVE
    ),
    ("POST", "/api/onboard/sessions/{session_id}/cancel"): RoutePolicy(
        Capability.SCHEMA_AUTHOR
    ),
    ("GET", "/api/onboard/domains"): RoutePolicy(Capability.SCHEMA_READ),

    # Candidate queries. Curating them is authoring the questions a domain must
    # answer, so it takes `schema:author` — but seeding an eval set *creates the
    # thing the gate measures against*, which is the same authority as writing
    # any dataset or moving a baseline.
    ("GET", "/api/onboard/sessions/{session_id}/queries"): RoutePolicy(
        Capability.SCHEMA_READ
    ),
    ("POST", "/api/onboard/sessions/{session_id}/queries/{query_id}"): RoutePolicy(
        Capability.SCHEMA_AUTHOR
    ),
    ("POST", "/api/onboard/sessions/{session_id}/seed-eval"): RoutePolicy(
        Capability.RELEASE_PROMOTE
    ),
}


_PARAM = re.compile(r"\{[^/}]+\}")


def is_public(path: str) -> bool:
    return path in PUBLIC_PATHS


def policy_for(method: str, route_template: str) -> RoutePolicy | None:
    """The declared policy for a route, or None when undeclared.

    None means **deny**. The caller must not treat it as "no restriction" — the
    absence of a decision is not permission.
    """
    return ROUTE_CAPABILITIES.get((method.upper(), route_template))


def undeclared_routes(registered: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Registered routes with neither a policy nor a public entry.

    Called at startup and asserted in ``tests/properties/test_authorization.py``.
    A route that reaches production undeclared is the failure this whole module
    exists to prevent, so it fails the build rather than merely logging.
    """
    missing = []
    for method, template in registered:
        if is_public(template):
            continue
        if policy_for(method, template) is None:
            missing.append((method, template))
    return sorted(missing)
