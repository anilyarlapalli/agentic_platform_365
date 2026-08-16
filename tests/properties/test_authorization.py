"""Authorisation holds across every route, every tenant, and every role.

Three families of case, in increasing order of what a failure would cost:

1. **Coverage** — no route is served without a declared policy, and the route
   table is not empty. The second half of that sounds trivial and is not: an
   empty table made this entire middleware a silent no-op under FastAPI 0.141
   (see ``route_table.py``), and every functional test still passed.

2. **Capability** — a role without a capability is refused, whatever the route.

3. **Cross-tenant** — tenant B's fully-authorised owner, addressing tenant A's
   resource identifiers directly, gets nothing. This is the case that matters:
   the caller is authenticated, authorised for the *action*, and simply
   addressing someone else's object. Role checks alone never catch it.
"""

from __future__ import annotations

import base64
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from platform_core.api.app import app
from platform_core.api.policy import PUBLIC_PATHS, ROUTE_CAPABILITIES, is_public
from platform_core.api.route_table import registered_pairs
from platform_core.db.engine import tenant_session
from platform_core.identity.auth import issue_token
from platform_core.identity.capabilities import Capability, grant
from platform_core.identity.principal import ActorType, Principal, RequestContext, Role, Tenant
from platform_core.observability import audit

pytestmark = pytest.mark.property


@pytest.fixture(scope="module")
def client() -> TestClient:
    with TestClient(app) as c:
        yield c


def _principal(tenant: Tenant, subject: str, *roles: Role) -> Principal:
    with tenant_session(tenant) as s:
        pid = s.execute(
            text(
                "INSERT INTO principal (tenant_id, subject, actor_type, roles) "
                "VALUES (:t, :s, 'human', :r) "
                "ON CONFLICT (tenant_id, subject) DO UPDATE SET roles = EXCLUDED.roles "
                "RETURNING id"
            ),
            {"t": tenant.id, "s": subject, "r": [str(r) for r in roles]},
        ).scalar_one()
    return Principal(
        id=pid, tenant=tenant, subject=subject, roles=frozenset(roles),
        actor_type=ActorType.HUMAN,
    )


def _auth(principal: Principal) -> dict[str, str]:
    return {"Authorization": f"Bearer {issue_token(principal)}"}


@pytest.fixture
def owner_a(tenant_a) -> Principal:
    return _principal(tenant_a, "owner@acme.example", Role.OWNER)


@pytest.fixture
def viewer_a(tenant_a) -> Principal:
    return _principal(tenant_a, "viewer@acme.example", Role.VIEWER)


@pytest.fixture
def operator_a(tenant_a) -> Principal:
    return _principal(tenant_a, "operator@acme.example", Role.OPERATOR)


@pytest.fixture
def owner_b(tenant_b) -> Principal:
    return _principal(tenant_b, "owner@globex.example", Role.OWNER)


@pytest.fixture
def tenant_a_resources(tenant_a, owner_a) -> dict[str, uuid.UUID]:
    """Real rows in tenant A, for tenant B to fail to reach."""
    with tenant_session(tenant_a) as s:
        run_id = s.execute(
            text(
                "INSERT INTO run (tenant_id, workload, status, requested_by) "
                "VALUES (:t, 'echo', 'pending', :p) RETURNING id"
            ),
            {"t": tenant_a.id, "p": owner_a.id},
        ).scalar_one()
        doc_id = s.execute(
            text(
                "INSERT INTO document (tenant_id, workload, collection, filename, "
                "content_sha256, byte_size, storage_key) "
                "VALUES (:t, 'echo', 'maintenance', 'secret.pdf', :sha, 10, 'k') "
                "RETURNING id"
            ),
            {"t": tenant_a.id, "sha": "a" * 64},
        ).scalar_one()
        approval_id = s.execute(
            text(
                "INSERT INTO tool_approval (tenant_id, run_id, tool_name, side_effect, "
                "arguments, arguments_sha256, requested_by, expires_at) "
                "VALUES (:t, :r, 'transfer_funds', 'write', '{}'::jsonb, :sha, :p, :exp) "
                "RETURNING id"
            ),
            {
                "t": tenant_a.id, "r": run_id, "sha": "b" * 64, "p": owner_a.id,
                "exp": datetime.now(UTC) + timedelta(hours=1),
            },
        ).scalar_one()
    return {"run": run_id, "document": doc_id, "approval": approval_id}


# ── 1. coverage ──────────────────────────────────────────────────────────


def test_route_table_is_not_empty(client, record_evidence):
    """The regression guard for the no-op middleware bug.

    Under FastAPI 0.141 / Starlette 1.6, included routers are not flattened into
    ``app.routes``, so the obvious enumeration returns an empty list. An empty
    route table means the middleware matches nothing and permits everything,
    while every functional test continues to pass.
    """
    table = app.state.route_table
    assert len(table) >= 10, (
        f"route table has {len(table)} entries — an under-populated table silently "
        f"disables authorisation for the routes it omits"
    )
    guarded = [e for e in table if not is_public(e.template)]
    assert guarded, "no guarded routes in the table; the middleware would be a no-op"

    record_evidence(
        "authorization_route_table_populated",
        holds=True,
        total_routes=len(table),
        guarded_routes=len(guarded),
        public_routes=len(table) - len(guarded),
    )


def test_every_route_has_a_declared_policy(client, record_evidence):
    """No route is served without an authorisation decision behind it."""
    pairs = registered_pairs(app.state.route_table)
    undeclared = [
        (m, p) for m, p in pairs if not is_public(p) and (m, p) not in ROUTE_CAPABILITIES
    ]
    assert not undeclared, f"routes served with no policy: {undeclared}"

    record_evidence(
        "authorization_policy_coverage",
        holds=True,
        routes_checked=len(pairs),
        public_paths=sorted(PUBLIC_PATHS),
    )


def test_public_readiness_never_exposes_configuration_or_credentials(
    client, record_evidence
):
    """A public probe reports control state, never resolved configuration."""
    response = client.get("/health/ready")
    assert response.status_code in {200, 503}
    body = response.json()
    assert "config" not in body

    serialized = response.text.lower()
    forbidden = (
        "postgresql+psycopg://",
        "redis://",
        "platform_dev_only",
        "jwt_secret",
        "openai_api_key",
        "s3_secret_key",
        "telemetry_hmac_key",
    )
    leaked = [token for token in forbidden if token in serialized]
    assert not leaked, f"public readiness leaked configuration tokens: {leaked}"

    record_evidence(
        "readiness_contains_no_credentials",
        holds=True,
        status=response.status_code,
        checks=sorted(body.get("checks", {})),
    )


def test_every_guarded_route_rejects_anonymous(client, record_evidence):
    """401 on every guarded route, with no exceptions and no partial responses."""
    checked = []
    for entry in app.state.route_table:
        if is_public(entry.template):
            continue
        # Substitute a syntactically valid id so the request reaches
        # authorisation rather than failing path validation first.
        url = entry.template
        for param in entry.route.param_convertors:
            url = url.replace(f"{{{param}}}", str(uuid.uuid4()))

        response = client.request(entry.method, url, json={})
        assert response.status_code == 401, (
            f"{entry.method} {entry.template} returned {response.status_code} to an "
            f"anonymous caller, expected 401"
        )
        checked.append(f"{entry.method} {entry.template}")

    record_evidence(
        "authorization_anonymous_denied", holds=True, routes_denied=len(checked),
        routes=sorted(checked),
    )


# ── 2. capability ────────────────────────────────────────────────────────


def test_viewer_cannot_write(client, viewer_a, record_evidence):
    """A read-only role is refused every write, by capability rather than by route."""
    headers = _auth(viewer_a)
    denials = []

    for method, url, body in (
        ("POST", "/api/documents",
         {"collection": "maintenance", "filename": "x.txt", "content_base64": "eA=="}),
        ("PUT", "/api/usage/caps", {"daily_token_cap": 999_999_999}),
        ("GET", "/api/members", None),
        ("POST", "/api/members/grants",
         {"principal_id": str(uuid.uuid4()), "capability": "budget:manage"}),
    ):
        response = client.request(method, url, json=body, headers=headers)
        assert response.status_code == 403, (
            f"viewer got {response.status_code} on {method} {url}, expected 403"
        )
        assert response.json()["code"] == "forbidden"
        denials.append(f"{method} {url}")

    # And the reads it *is* entitled to still work, so the test is measuring
    # authorisation rather than a broken token.
    assert client.get("/api/runs", headers=headers).status_code == 200

    record_evidence(
        "authorization_capability_enforced", holds=True, denied=denials,
        role="viewer", reads_still_permitted=True,
    )


def test_operator_cannot_manage_budget(client, operator_a, record_evidence):
    """Reading spend and changing the ceiling are different authorities.

    In the Azure build both sit behind the same ``admin`` gate, so anyone who
    can read spend can also raise the cap.
    """
    headers = _auth(operator_a)
    assert client.get("/api/usage", headers=headers).status_code == 200
    assert client.put(
        "/api/usage/caps", json={"daily_token_cap": 10**9}, headers=headers
    ).status_code == 403

    record_evidence(
        "authorization_budget_separated", holds=True,
        detail="USAGE_READ granted, BUDGET_MANAGE refused for the same principal",
    )


def test_resource_grant_cannot_be_spoofed_with_a_query_parameter(
    client, tenant_a, owner_a, record_evidence
):
    """The authorised collection is the JSON value consumed by the handler.

    A query parameter with an allowed collection must not authorise a body that
    names a different collection.
    """
    scoped = _principal(tenant_a, "scoped-ingestor@acme.example")
    grant(owner_a, scoped.id, Capability.DOCUMENT_INGEST, resource="allowed")

    response = client.post(
        "/api/documents?collection=allowed",
        json={
            "collection": "forbidden",
            "filename": "scope-check.txt",
            "content_base64": base64.b64encode(b"must never be stored").decode(),
        },
        headers=_auth(scoped),
    )
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "forbidden"

    record_evidence(
        "authorization_body_resource_is_authoritative",
        holds=True,
        detail="allowed query parameter could not authorise a forbidden JSON collection",
    )


def test_role_removal_takes_effect_before_token_expiry(client, tenant_a, record_evidence):
    principal = _principal(tenant_a, "role-change@acme.example", Role.OWNER)
    token = issue_token(principal)
    with tenant_session(tenant_a) as session:
        session.execute(
            text("UPDATE principal SET roles = ARRAY['viewer'] WHERE id = :id"),
            {"id": principal.id},
        )

    response = client.put(
        "/api/usage/caps",
        json={"daily_token_cap": 1000},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 403, response.text

    record_evidence(
        "authorization_current_roles_override_token_roles",
        holds=True,
        detail="an owner token lost budget authority immediately after the database role removal",
    )


def test_self_approval_is_refused(client, tenant_a, owner_a, tenant_a_resources,
                                  record_evidence):
    """Maker cannot be checker, even for an owner holding every capability.

    Enforced twice on purpose: a capability rule that refuses when the actor is
    the subject, and a CHECK constraint in migration 0002 that no code path can
    bypass.
    """
    response = client.post(
        f"/api/approvals/{tenant_a_resources['approval']}/decide",
        json={"approved": True},
        headers=_auth(owner_a),
    )
    assert response.status_code == 403, (
        f"owner approved their own request ({response.status_code}); "
        f"maker-cannot-be-checker was not enforced"
    )
    assert "self-approval" in response.json()["detail"].lower()

    # A different reviewer in the same tenant succeeds, proving the refusal is
    # about self-approval rather than about the route being broken.
    reviewer = _principal(tenant_a, "reviewer@acme.example", Role.REVIEWER)
    ok = client.post(
        f"/api/approvals/{tenant_a_resources['approval']}/decide",
        json={"approved": True},
        headers=_auth(reviewer),
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["status"] == "approved"

    record_evidence(
        "authorization_no_self_approval", holds=True,
        detail="requester refused with 403; independent reviewer accepted",
    )


# ── 3. cross-tenant ──────────────────────────────────────────────────────


def test_cross_tenant_resource_access_is_denied(
    client, owner_b, tenant_a_resources, record_evidence
):
    """The case role checks never catch.

    Tenant B's owner holds every capability in its own tenant and is addressing
    tenant A's identifiers directly. Every response must be 404 — not 403, which
    would confirm the identifier exists.
    """
    headers = _auth(owner_b)
    probes = [
        ("GET", f"/api/runs/{tenant_a_resources['run']}", None),
        ("POST", f"/api/runs/{tenant_a_resources['run']}/cancel", {}),
        ("GET", f"/api/documents/{tenant_a_resources['document']}", None),
        ("DELETE", f"/api/documents/{tenant_a_resources['document']}", None),
        ("DELETE", f"/api/documents/{tenant_a_resources['document']}/purge", None),
        ("POST", f"/api/approvals/{tenant_a_resources['approval']}/decide",
         {"approved": True}),
    ]

    results = []
    for method, url, body in probes:
        response = client.request(method, url, json=body, headers=headers)
        assert response.status_code == 404, (
            f"tenant B got {response.status_code} on {method} {url} — expected 404. "
            f"Body: {response.text[:200]}"
        )
        results.append(f"{method} {url.rsplit('/', 1)[0]}/<tenant-a-id>")

    record_evidence(
        "authorization_cross_tenant_denied", holds=True, probes=results,
        detail="404 rather than 403 throughout, so no identifier is confirmed to exist",
    )


def test_cross_tenant_listing_returns_only_own_rows(
    client, owner_b, tenant_a, tenant_a_resources, record_evidence
):
    """Collection endpoints are scoped, not merely the by-id ones."""
    headers = _auth(owner_b)

    runs = client.get("/api/runs", headers=headers).json()
    documents = client.get("/api/documents", headers=headers).json()

    assert runs["runs"] == [], f"tenant B listed tenant A's runs: {runs}"
    assert documents["documents"] == [], f"tenant B listed tenant A's documents: {documents}"
    assert runs["tenant"] == "globex-motors"

    record_evidence(
        "authorization_cross_tenant_listing", holds=True,
        detail="list endpoints return empty for a tenant with no rows of its own",
    )


def test_token_tenant_cannot_be_overridden_by_request(
    client, owner_b, tenant_a, tenant_a_resources, record_evidence
):
    """The tenant comes from the signed token and nothing else.

    Headers and query parameters naming another tenant must be inert. If the
    tenant were caller-supplied, every route would have to verify entitlement to
    it and the first one that forgets is a full breach.
    """
    spoofs = [
        {"X-Tenant-Id": str(tenant_a.id)},
        {"X-Tenant": "acme-industrial"},
    ]
    for spoof in spoofs:
        response = client.get(
            f"/api/runs/{tenant_a_resources['run']}", headers={**_auth(owner_b), **spoof}
        )
        assert response.status_code == 404, f"spoof {spoof} changed the outcome"

    query = client.get(
        f"/api/runs?tenant_id={tenant_a.id}", headers=_auth(owner_b)
    ).json()
    assert query["runs"] == []
    assert query["tenant"] == "globex-motors"

    record_evidence(
        "authorization_tenant_not_caller_supplied", holds=True,
        detail="tenant headers and query parameters are inert; the signed claim governs",
    )


def test_expired_and_forged_tokens_are_rejected(client, owner_a, record_evidence):
    """Signature and expiry are both enforced, and `alg` is pinned."""
    import jwt

    from platform_core.settings import get_settings

    settings = get_settings()

    expired = issue_token(owner_a, ttl=timedelta(seconds=-1))
    assert client.get(
        "/api/runs", headers={"Authorization": f"Bearer {expired}"}
    ).status_code == 401

    wrong_key = jwt.encode(
        {
            "sub": str(owner_a.id), "tid": str(owner_a.tenant.id),
            "tsl": owner_a.tenant.slug, "sbj": owner_a.subject, "rol": ["owner"],
            "iat": int(datetime.now(UTC).timestamp()),
            "exp": int((datetime.now(UTC) + timedelta(hours=1)).timestamp()),
        },
        "not-the-real-secret-but-at-least-32-bytes",
        algorithm="HS256",
    )
    assert client.get(
        "/api/runs", headers={"Authorization": f"Bearer {wrong_key}"}
    ).status_code == 401

    # `alg: none` — rejected because decode_token pins algorithms rather than
    # trusting the token's own header.
    unsigned = jwt.encode(
        {
            "sub": str(owner_a.id), "tid": str(owner_a.tenant.id),
            "tsl": owner_a.tenant.slug, "sbj": owner_a.subject, "rol": ["owner"],
            "iat": int(datetime.now(UTC).timestamp()),
            "exp": int((datetime.now(UTC) + timedelta(hours=1)).timestamp()),
        },
        key="",
        algorithm="none",
    )
    assert client.get(
        "/api/runs", headers={"Authorization": f"Bearer {unsigned}"}
    ).status_code == 401

    assert settings.jwt_algorithm == "HS256"

    record_evidence(
        "authorization_token_integrity", holds=True,
        detail="expired, wrong-key and alg:none tokens all rejected",
    )


def test_audit_api_returns_only_the_verified_tenant_chain(
    client, owner_a, owner_b, record_evidence
):
    audit.record(
        RequestContext(principal=owner_a),
        action="test.audit.api.acme",
        outcome=audit.Outcome.SUCCEEDED,
        required=True,
    )
    audit.record(
        RequestContext(principal=owner_b),
        action="test.audit.api.globex",
        outcome=audit.Outcome.SUCCEEDED,
        required=True,
    )

    response = client.get("/api/audit?limit=50", headers=_auth(owner_a))
    assert response.status_code == 200
    payload = response.json()
    assert payload["verification"]["intact"] is True
    assert payload["verification"]["events_checked"] >= 1
    actions = {event["action"] for event in payload["events"]}
    assert "test.audit.api.acme" in actions
    assert "test.audit.api.globex" not in actions
    assert all(event["hash"] for event in payload["events"])

    record_evidence(
        "audit_api_is_tenant_scoped_and_verified",
        holds=True,
        events_checked=payload["verification"]["events_checked"],
    )
