# Production readiness contract

This repository now supplies the application controls and a hardened deployment
baseline. A production launch is permitted only when every item below is
satisfied; none of the observability, evaluation, recovery or security controls
is optional.

## Required managed dependencies

- PostgreSQL 16 with pgvector, multi-zone HA, TLS, PITR and deletion protection.
- TLS Redis with persistence for Celery delivery and rate limiting. Postgres
  remains the run/outbox source of truth.
- Versioned, encrypted S3-compatible object storage with soft delete and
  cross-region replication.
- An OpenTelemetry collector tier with durable/bounded queues and production
  trace, metric and log backends. All workloads export through OTLP/TLS.
- Kubernetes with enforced Restricted Pod Security, NetworkPolicy, metrics
  server, KEDA 2.20+, a TLS ingress/gateway, external secret synchronization,
  and workload identity for registry/secret access.

The base under `deploy/k8s/base` deliberately contains invalid registry hosts,
`unreleased` labels and default-deny external egress. An environment overlay
must replace them. This makes an incomplete production configuration fail to
start instead of quietly shipping.

## Secret contract

Create these Secrets through the external-secret controller; do not generate
them in Kustomize or commit examples with values.

| Secret | Required keys |
| --- | --- |
| `platform-api-secrets` | `database-url`, `redis-url`, `s3-access-key`, `s3-secret-key`, `openai-api-key`, `jwt-secret`, `telemetry-hmac-key` |
| `platform-worker-secrets` | `database-url`, `celery-broker-url`, `celery-result-backend`, `s3-access-key`, `s3-secret-key`, `openai-api-key`, `telemetry-hmac-key`, `redis-address`, `redis-username`, `redis-password` |
| `platform-relay-secrets` | `database-relay-url`, `celery-broker-url`, `celery-result-backend`, `telemetry-hmac-key` |
| `platform-maintenance-secrets` | `database-relay-url`, `celery-broker-url`, `celery-result-backend`, `telemetry-hmac-key` |
| `platform-scheduler-secrets` | `celery-broker-url`, `celery-result-backend`, `telemetry-hmac-key` |
| `platform-migrator-secrets` | `database-owner-url`, `telemetry-hmac-key` |

The API and ordinary run workers must never receive owner or relay credentials.
Only the one-shot migration Job receives the owner DSN. Relay and maintenance
share the narrowly granted cross-tenant role, but consume different queues.

## Release sequence

1. CI passes lock validation, lint, unit/property tests, dependency audits,
   manifest policy, web build, and both image builds.
2. Build the same source once. Publish API/runtime and web images by digest with
   SBOM and provenance attestations; sign them using workload identity/OIDC.
3. Produce an environment overlay that sets both images to `@sha256` references,
   replaces every `unreleased` label/name with the git SHA, configures the real
   HTTPS origins/proxy CIDRs/endpoints, and adds only approved private endpoint
   CIDRs based on `deploy/k8s/external-egress.example.yaml`.
4. Run `.venv/bin/python -m scripts.check_deployment_policy --release --rendered <release.yaml>`
   against the rendered release (or enforce the same checks with admission
   policy) before cluster admission.
5. Apply foundation/config/network resources, then run the release-specific
   migration Job. A failed Job blocks rollout.
6. Roll out relay, maintenance, scheduler and workers; verify scheduler and
   outbox metrics. Roll out API and web as a canary.
7. Execute the pinned evaluation dataset. Promotion requires an independent
   judge pass and healthy latency/error/cost signals. Forced promotion remains
   audited and is reserved for an incident commander.
8. Increase traffic gradually. Roll back on SLO burn, audit failure, telemetry
   loss or eval regression.

## Launch evidence

- `/health/ready` is green on every API replica, including telemetry,
  checkpoint durability, dependencies and continuous-evaluation schedule.
- Cross-tenant, privilege-catalog, idempotency, crash-recovery, budget and
  governance property suites pass against the release schema.
- Alerts reach the on-call route and every alert's linked runbook is available.
- A restore drill has met the declared RPO/RTO and verified audit chains plus
  object/database integrity.
- Load tests establish API saturation, queue drain rate, relay throughput and
  database pool limits; HPA/KEDA maxima are below downstream quota ceilings.
- Credential rotation, data erasure and rollback drills have been executed by
  someone other than the author of the procedure.
