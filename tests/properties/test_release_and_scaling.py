"""A bad canary rolls itself back; sessions survive replicas; the cache is scoped.

The acceptance case is the first test: ship a deliberately bad revision to 10% of
traffic, let it accumulate observations, and require the supervisor to detect the
breach and restore traffic **without a human**. A canary that needs someone to
notice has the same mean time to recovery as no canary — it just fails on a
smaller fraction of traffic while somebody reads a dashboard.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import text

from platform_core.db.engine import owner_session, tenant_session
from platform_core.identity.principal import RequestContext
from platform_core.ports.llm import ChatResponse, TokenUsage
from platform_core.release import canary
from platform_core.scaling import sessions
from platform_core.scaling.cache import RedisSemanticCache, response_from_cached

pytestmark = pytest.mark.property

GOOD = "rev-0001-good"
BAD = "rev-0002-bad"


@pytest.fixture(autouse=True)
def _clean_releases():
    yield
    with owner_session() as s:
        s.execute(text("DELETE FROM release_observation"))
        s.execute(text("DELETE FROM release"))


@pytest.fixture
def active_release():
    with owner_session() as s:
        s.execute(
            text(
                "INSERT INTO release (revision, image_tag, schema_version, status, "
                "traffic_weight) VALUES (:r, 'v1', '0011', 'active', 100)"
            ),
            {"r": GOOD},
        )
    return GOOD


def _observe(revision: str, *, n: int, error_rate: float, latency_ms: float = 100.0):
    errors = int(n * error_rate)
    with owner_session() as s:
        for i in range(n):
            s.execute(
                text(
                    "INSERT INTO release_observation (revision, route, outcome, "
                    "latency_ms) VALUES (:r, '/api/query', :o, :ms)"
                ),
                {"r": revision, "o": "error" if i < errors else "success",
                 "ms": latency_ms},
            )


# ── the acceptance case ──────────────────────────────────────────────────


def test_a_bad_canary_rolls_itself_back(active_release, record_evidence):
    """Ship at 10%, breach the SLO, recover automatically."""
    canary.register(BAD, image_tag="v2", schema_version="0011")
    canary.start_canary(BAD, weight=10)

    split = canary.traffic_split()
    assert split == {GOOD: 90, BAD: 10}, split

    # Baseline is healthy; the canary errors on a third of its requests.
    _observe(GOOD, n=200, error_rate=0.01, latency_ms=100)
    _observe(BAD, n=40, error_rate=0.33, latency_ms=120)

    verdict = canary.supervise(BAD, GOOD)

    assert verdict.action == "rollback", verdict.explain()
    assert any("error rate rose" in r for r in verdict.reasons)

    # Traffic is fully restored, with no redeploy and no human.
    restored = canary.traffic_split()
    assert restored == {GOOD: 100}, restored

    with owner_session() as s:
        row = s.execute(
            text("SELECT status, rolled_back_at, rollback_reason FROM release "
                 "WHERE revision = :r"),
            {"r": BAD},
        ).one()
    assert row.status == "rolled_back"
    assert row.rolled_back_at is not None
    assert "error rate" in row.rollback_reason

    record_evidence(
        "release_bad_canary_auto_rollback", holds=True,
        weight_at_breach=10,
        baseline_error_rate=verdict.baseline.error_rate,
        candidate_error_rate=verdict.candidate.error_rate,
        traffic_after=restored,
        detail="SLO breach detected against the baseline and traffic restored automatically",
    )


def test_a_latency_regression_also_trips(active_release, record_evidence):
    """Not every bad release errors. A slow one is still a bad one."""
    canary.register(BAD, image_tag="v2", schema_version="0011")
    canary.start_canary(BAD, weight=10)

    _observe(GOOD, n=200, error_rate=0.0, latency_ms=100)
    _observe(BAD, n=40, error_rate=0.0, latency_ms=400)

    verdict = canary.supervise(BAD, GOOD)
    assert verdict.action == "rollback", verdict.explain()
    assert any("p95 latency" in r for r in verdict.reasons)
    assert canary.traffic_split() == {GOOD: 100}

    record_evidence(
        "release_latency_regression_trips", holds=True,
        baseline_p95=verdict.baseline.p95_latency_ms,
        candidate_p95=verdict.candidate.p95_latency_ms,
    )


def test_a_healthy_canary_is_promoted(active_release, record_evidence):
    """The gate must also let good releases through, or it gets switched off."""
    canary.register(BAD, image_tag="v2", schema_version="0011")
    canary.start_canary(BAD, weight=10)

    _observe(GOOD, n=200, error_rate=0.02, latency_ms=100)
    _observe(BAD, n=50, error_rate=0.02, latency_ms=105)

    verdict = canary.supervise(BAD, GOOD)
    assert verdict.action == "promote", verdict.explain()
    assert canary.traffic_split() == {BAD: 100}

    with owner_session() as s:
        retired = s.execute(
            text("SELECT status FROM release WHERE revision = :r"), {"r": GOOD}
        ).scalar_one()
    assert retired == "retired"

    record_evidence(
        "release_healthy_canary_promoted", holds=True,
        detail="comparable error rate and latency promotes; the previous revision retires",
    )


def test_an_undersampled_canary_is_held_not_promoted(active_release, record_evidence):
    """"Not enough data to detect a breach" is not "no breach".

    The same vacuity rule as everywhere else here. Without it a canary promotes
    itself seconds after starting, before it has served enough traffic to fail.
    """
    canary.register(BAD, image_tag="v2", schema_version="0011")
    canary.start_canary(BAD, weight=10)

    _observe(GOOD, n=200, error_rate=0.01)
    _observe(BAD, n=3, error_rate=0.0)  # perfect, and meaningless

    verdict = canary.supervise(BAD, GOOD)
    assert verdict.action == "hold", verdict.explain()
    assert any("insufficient sample" in r for r in verdict.reasons)
    # Still a canary — neither promoted nor rolled back.
    assert canary.traffic_split() == {GOOD: 90, BAD: 10}

    record_evidence(
        "release_undersampled_canary_held", holds=True,
        observations=verdict.candidate.observations,
        detail="a perfect score over 3 requests holds rather than promoting",
    )


def test_the_gate_compares_against_the_baseline_not_a_fixed_threshold(
    active_release, record_evidence
):
    """A service with a 10% steady-state error rate must not trip on every deploy.

    A fixed threshold is wrong in both directions: it fires constantly on a noisy
    service and never fires on a quiet one that regresses a hundredfold.
    """
    canary.register(BAD, image_tag="v2", schema_version="0011")
    canary.start_canary(BAD, weight=10)

    # Both revisions are equally bad in absolute terms — and that is not a
    # regression.
    _observe(GOOD, n=200, error_rate=0.10, latency_ms=100)
    _observe(BAD, n=50, error_rate=0.10, latency_ms=100)

    verdict = canary.evaluate_canary(BAD, GOOD)
    assert verdict.action == "promote", verdict.explain()

    record_evidence(
        "release_gate_is_comparative", holds=True,
        error_rate_both=0.10,
        detail="a 10% error rate matching the baseline is not a regression",
    )


def test_rollback_reports_whether_the_schema_must_move(active_release, record_evidence):
    """Rolling back an image does not roll back the database.

    Expand/contract is what makes the answer normally "no". A candidate that ran
    a contracting migration cannot be recovered by restoring the image alone, so
    the rollback says so rather than reporting success.
    """
    canary.register(BAD, image_tag="v2", schema_version="0012")  # moved the schema
    canary.start_canary(BAD, weight=10)

    result = canary.rollback(BAD, reason="test")
    assert result["schema_rollback_required"] is True
    assert result["candidate_schema"] == "0012"
    assert result["active_schema"] == "0011"

    # And the expand-only case reports no schema work.
    with owner_session() as s:
        s.execute(text("DELETE FROM release WHERE revision = :r"), {"r": BAD})
    canary.register(BAD, image_tag="v3", schema_version="0011")
    canary.start_canary(BAD, weight=10)
    clean = canary.rollback(BAD, reason="test")
    assert clean["schema_rollback_required"] is False

    record_evidence(
        "release_rollback_reports_schema_drift", holds=True,
        detail="a candidate on a different schema version flags that the image alone "
               "does not restore service",
    )


def test_the_app_role_cannot_shift_traffic(tenant_a, record_evidence):
    """Promoting a revision is a deploy action, not a request action."""
    from sqlalchemy.exc import ProgrammingError

    with pytest.raises(ProgrammingError) as err, tenant_session(tenant_a) as s:
        s.execute(text("UPDATE release SET traffic_weight = 100"))
    assert "permission denied" in str(err.value).lower()

    record_evidence(
        "release_traffic_is_privileged", holds=True,
        detail="the app role holds SELECT on release and nothing else",
    )


# ── sessions ─────────────────────────────────────────────────────────────


def test_a_session_is_readable_from_any_replica(tenant_a, principal_a, record_evidence):
    """State lives in Postgres, so the second turn does not need the first replica.

    In the Azure build `_Singleton.sessions` is a per-process dict with three
    replicas and no affinity, so this is the case that fails ~2/3 of the time.
    """
    ctx = RequestContext(principal=principal_a)
    created = sessions.create(ctx, workload="echo")

    sessions.append_turn(ctx, created.id, {"role": "user", "content": "first"})
    # A different RequestContext stands in for a different replica: no shared
    # in-process state, only the same identity.
    other_replica = RequestContext(principal=principal_a)
    loaded = sessions.load(other_replica, created.id)

    assert loaded.turn_count == 1
    assert loaded.state["turns"][0]["content"] == "first"

    sessions.append_turn(other_replica, created.id, {"role": "user", "content": "second"})
    final = sessions.load(ctx, created.id)
    assert final.turn_count == 2
    assert [t["content"] for t in final.state["turns"]] == ["first", "second"]

    record_evidence(
        "session_replica_independent", holds=True, turns=final.turn_count,
        detail="turns appended from two contexts are both present and ordered",
    )


def test_a_session_id_alone_does_not_grant_access(tenant_a, tenant_b, principal_a,
                                                  principal_b, record_evidence):
    """Possessing an id is not sufficient — the session is bound to a principal.

    `GET /api/sessions/{session_id}` in the Azure build has no auth dependency at
    all and sessions carry no user binding, so an id is a complete credential.
    """
    owner_ctx = RequestContext(principal=principal_a)
    created = sessions.create(owner_ctx, workload="echo")
    sessions.append_turn(owner_ctx, created.id, {"role": "user", "content": "private"})

    # Another principal in the same tenant, holding the id.
    with tenant_session(tenant_a) as s:
        other_id = s.execute(
            text(
                "INSERT INTO principal (tenant_id, subject, roles) "
                "VALUES (:t, 'other@acme.example', ARRAY['viewer']) RETURNING id"
            ),
            {"t": tenant_a.id},
        ).scalar_one()
    from platform_core.identity.principal import Principal, Role

    other = RequestContext(
        principal=Principal(id=other_id, tenant=tenant_a,
                            subject="other@acme.example", roles=frozenset({Role.VIEWER}))
    )
    with pytest.raises(sessions.SessionNotFound):
        sessions.load(other, created.id)

    # And a principal in another tenant, also holding the id.
    with pytest.raises(sessions.SessionNotFound):
        sessions.load(RequestContext(principal=principal_b), created.id)

    record_evidence(
        "session_id_is_not_a_credential", holds=True,
        detail="a session id is refused for a different principal and a different tenant",
    )


def test_expired_sessions_are_not_loadable(tenant_a, principal_a, record_evidence):
    ctx = RequestContext(principal=principal_a)
    created = sessions.create(ctx, workload="echo", ttl=timedelta(seconds=-1))

    with pytest.raises(sessions.SessionNotFound):
        sessions.load(ctx, created.id)

    assert sessions.purge_expired() >= 1

    record_evidence("session_expiry_enforced", holds=True)


# ── cache ────────────────────────────────────────────────────────────────


def test_cache_keys_are_tenant_scoped(tenant_a, tenant_b, principal_a, principal_b,
                                      record_evidence):
    """An identical request in two tenants produces two different keys.

    The tenant is a prefix, so a cross-tenant hit is unreachable rather than
    merely rejected — there is no code path that could forget to check.
    """
    cache = RedisSemanticCache(client=_FakeRedis())
    payload = {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "same"}]}

    ctx_a = RequestContext(principal=principal_a)
    ctx_b = RequestContext(principal=principal_b)
    key_a = cache.key(ctx_a, payload)
    key_b = cache.key(ctx_b, payload)

    assert key_a != key_b
    assert str(tenant_a.id) in key_a and str(tenant_b.id) in key_b

    response = ChatResponse(
        content="tenant A answer", model="gpt-4o-mini",
        usage=TokenUsage(10, 5), cost_usd=0.001, finish_reason="stop",
    )
    cache.put(ctx_a, key_a, response)

    assert cache.get(ctx_a, key_a)["content"] == "tenant A answer"
    assert cache.get(ctx_b, key_b) is None, "tenant B read tenant A's cached answer"

    record_evidence(
        "cache_tenant_scoped", holds=True, hit_ratio=cache.stats.hit_ratio,
        detail="the tenant is part of the key, so a cross-tenant hit is unreachable",
    )


def test_a_cache_hit_costs_nothing(record_evidence):
    """A replayed answer must not be charged twice.

    The original call is already in the ledger. Charging the hit as well would
    double-count spend and make the cache appear to *increase* cost.
    """
    cached = response_from_cached(
        {"content": "x", "model": "gpt-4o-mini",
         "usage": {"input_tokens": 10, "output_tokens": 5, "reported": True},
         "cost_usd": 0.001, "finish_reason": "stop"}
    )
    assert cached.cost_usd == 0.0
    assert cached.cache_hit is True

    record_evidence("cache_hit_is_free", holds=True)


def test_an_unavailable_cache_degrades_to_a_miss(tenant_a, principal_a, record_evidence):
    """A cache that can take the request path down is a dependency, not an optimisation."""
    class Broken:
        def get(self, *a, **k):
            raise ConnectionError("redis down")

        def setex(self, *a, **k):
            raise ConnectionError("redis down")

    cache = RedisSemanticCache(client=Broken())
    ctx = RequestContext(principal=principal_a)
    key = cache.key(ctx, {"q": 1})

    assert cache.get(ctx, key) is None      # a miss, not an exception
    cache.put(ctx, key, ChatResponse(
        content="x", model="m", usage=TokenUsage(1, 1), cost_usd=0.0,
        finish_reason="stop",
    ))                                       # also does not raise
    assert cache.stats.errors == 2

    record_evidence(
        "cache_degrades_to_miss", holds=True,
        detail="read and write failures are absorbed; the request path is unaffected",
    )


class _FakeRedis:
    """Minimal in-memory stand-in. The cache's contract is get/setex/scan_iter."""

    def __init__(self) -> None:
        self._data: dict[str, str] = {}

    def get(self, key):
        return self._data.get(key)

    def setex(self, key, _ttl, value):
        self._data[key] = value

    def scan_iter(self, match: str, count: int = 500):
        prefix = match.rstrip("*")
        return [k for k in list(self._data) if k.startswith(prefix)]

    def delete(self, key):
        self._data.pop(key, None)
