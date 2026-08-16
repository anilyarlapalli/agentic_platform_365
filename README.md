# local-platform

`local-platform` is a runnable reference implementation of the controls needed
around production agentic workloads. GraphRAG is one workload under test; the
platform boundary—identity, durable execution, governed tools, cost control,
evaluation, audit, telemetry, and operations—is the product.

The repository contains a local Docker substrate, a role-separated runtime,
hardened Kubernetes base manifests, release workflows, and executable property,
chaos, and load checks. It is not a claim that an arbitrary cluster is ready:
managed HA dependencies, real secrets, private network endpoints, alert routing,
and recovery/load drills must still be supplied and proven per environment.

## Enforced guarantees

| Concern | Mechanism | Principal evidence |
| --- | --- | --- |
| Authentication and tenant isolation | Tenant-bound JWTs, principal-state checks, default-deny route policy, Postgres RLS | `tests/properties/test_authorization.py`, `test_tenant_isolation.py`, `test_privilege_model.py` |
| Durable agent execution | Postgres checkpoints, leases/heartbeats, transactional outbox, step/effect idempotency | `test_agent_runtime.py`, `test_idempotency.py`, `tests/chaos/` |
| Governed tools | Bounded registry, capability checks, exact-argument approvals, timeouts, durable receipts, reconciliation state | `test_execution_controls.py`, `tests/unit/test_agent_runtime_unit.py` |
| Spend and fairness | Pre-dispatch reservations, tenant caps, attribution, bounded retry/jitter, cancellation, fair queue ordering and distributed rate limits | `test_cost_attribution.py`, `test_execution_controls.py`, `tests/unit/test_security_controls.py` |
| Evaluation and release safety | Versioned golden sets, independent judge/annotator, mandatory continuous schedules and promotion gates | `test_eval_gates.py`, `test_eval_exposure.py`, `test_governance_controls.py` |
| Audit and data lifecycle | Fail-closed privileged audit, tamper-evident tenant chains with retention anchors, bounded erasure/retention | `test_cost_attribution.py`, `test_governance_controls.py`, `test_document_ingest.py` |
| Observability | OTLP traces, metrics and privacy-scrubbed logs; readiness checks; Prometheus alerts, Grafana dashboard and runbooks | `platform_core/observability/`, `deploy/prometheus/`, `deploy/grafana/`, `docs/runbooks/` |
| Delivery | Frozen Python/npm locks, dependency audits, pinned actions/base images, non-root images, SBOM/provenance, manifest policy | `.github/workflows/`, `scripts/check_deployment_policy.py`, `deploy/k8s/base/` |

Telemetry and continuous evaluation are mandatory in staging and production.
Startup validation rejects disabled telemetry, insecure OTLP, missing telemetry
pseudonymisation keys, fail-open audit/budget/rate controls, development secrets,
mutable release identity, and unsafe proxy/origin configuration.

## Runtime shape

| Process | Authority |
| --- | --- |
| API | Tenant-scoped app database, cache/rate limiter, object store, model provider and JWT signing key |
| Run worker | Tenant-scoped app database, run queue, object store and model provider; no JWT, relay or owner credential |
| Relay | Narrow cross-tenant outbox function and broker; no tenant content, model, object-store or JWT credential |
| Maintenance worker | Narrow cross-tenant lease/eval/retention functions and maintenance queue |
| Scheduler | Broker only; duplicate sweep scheduling is safe and avoids a singleton |
| Migrator | Owner database credential only, in a one-shot release-specific job |
| Web | Server-side API proxy only; bearer credentials stay in HttpOnly cookies |

Postgres is the source of truth for runs and outbox intent. Redis/Celery carries
pointers and may deliver them more than once; leases, checkpoints and effect
receipts decide what is allowed to execute.

## Local quickstart

Requirements: Docker with Compose, Python 3.12, `uv`, and Node only if running
the web console outside its container.

```bash
uv sync --extra dev
cp .env.example .env.local
make up
make migrate
make init-store
make verify
```

The local substrate uses deliberately offset ports: Postgres `5442`, Redis
`6389`, MinIO `9100/9101`, Jaeger `16687`, Prometheus `9190`, Grafana `3100`,
and OTLP `4317/4318`.

Run the complete local process split with:

```bash
make runtime-up
# API http://127.0.0.1:8100, console http://127.0.0.1:3000
make runtime-down
```

If those host ports are occupied, set `API_PORT` and `WEB_PORT`; the browser
origin policy follows `WEB_PORT` so the alternate binding remains usable
without weakening origin checks.

```bash
API_PORT=18100 WEB_PORT=13000 make runtime-up
```

The runtime defaults to a local Ollama-compatible endpoint. Set
`OPENAI_API_KEY` only for explicitly requested live calls; never commit it.

## Verification

```bash
make lock-check     # dependency declarations match uv.lock
make lint           # Ruff, no source rewriting
make policy         # manifests, privilege split, runbooks, secret patterns
make audit          # Python and npm vulnerability audits
make verify         # platform property suite
make chaos          # worker crash recovery at side-effect boundaries
make load           # throughput/contention evidence
make web-check      # locked install, audit, generated types, typecheck, build
make images         # non-root runtime and web images
make image-audit    # final image OS/library high/critical CVE gate
```

CI runs the fast release gates on every change and the load/mutation suites on a
schedule. Tagged releases build and push digest-addressed images with SBOM and
provenance attestations; deployment still requires an environment overlay and
the release policy gate.

## Production deployment

Start with `deploy/k8s/base`, then follow
[`docs/production-readiness.md`](docs/production-readiness.md). The base is
intentionally non-deployable as a release: invalid registry names,
`unreleased` labels and default-deny external egress force each environment to
provide immutable image digests, real endpoints, private egress CIDRs and
secret-manager-backed credentials.

Before admission, render the overlay and run:

```bash
.venv/bin/python -m scripts.check_deployment_policy \
  --release --rendered /path/to/rendered-release.yaml
```

Operations procedures live in `docs/runbooks/operations/`; every Prometheus
alert also resolves to a checked-in runbook. A launch is incomplete until alert
routing, restore, credential rotation, erasure, rollback and capacity drills
have produced environment-specific evidence.

## Workloads and external source trees

`workloads/echo/` is deliberately trivial so platform properties never depend
on GraphRAG. `workloads/graphrag/` can read a separately configured canonical
engine tree when `GRAPHRAG_ENABLED=true`; startup refuses the setting when that
tree is absent, and this repository does not write to it.
