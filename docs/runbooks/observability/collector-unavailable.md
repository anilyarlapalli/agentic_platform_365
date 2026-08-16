# OpenTelemetry collector unavailable

Impact: API readiness fails and production processes refuse unsafe telemetry
configuration. Do not disable telemetry to restore traffic.

1. Confirm scope from collector pod health, restart count, receiver errors and
   backend exporter queues. Check certificate expiry and DNS before restarting.
2. If the change began with a collector/config/backend rollout, roll that
   component back. Preserve the failed pod logs first.
3. If an exporter backend is slow, restore the last known-good bounded
   queue/batch configuration; do not remove redaction.
4. Verify OTLP TLS from each workload namespace, then verify `/health/ready`
   reports `telemetry.ok=true` on all API replicas.
5. Resolve only after traces, metrics and logs from the current release are
   queryable and the alert has remained clear for 15 minutes.

Escalate immediately if data may have bypassed the redaction processor or if
collector buffers were lost; treat either as a security/forensics incident.

