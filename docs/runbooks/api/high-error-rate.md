# API high error rate

1. Break down 5xx by route, release and exception type. Correlate the first bad
   trace with deploy, dependency, budget, and database events.
2. If one release regressed, stop promotion and roll back using the release
   runbook. Do not retry non-idempotent requests manually.
3. For dependency failure, keep failed readiness replicas out of service and
   restore the dependency; do not bypass RLS, admission control, audit, or
   telemetry to regain capacity.
4. Check Postgres pool saturation, Redis admission failures, object-store
   errors, outbox age and model-provider throttle responses.
5. Verify error rate is below 1%, readiness is green, and a representative
   authenticated request succeeds for two tenants before resolving.

