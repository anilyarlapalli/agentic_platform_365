# Admission control unavailable

Impact: production APIs fail closed with 503 because the distributed limiter
cannot establish a trustworthy quota decision. Never set
`RATE_LIMIT_FAIL_CLOSED=false` to restore traffic.

1. Check Redis TLS, credentials, latency, memory pressure, connection limits and
   the API-to-Redis network policy/private endpoint.
2. Confirm the failure is shared dependency loss rather than a single bad API
   replica. Roll back a release-specific client or configuration regression.
3. Restore Redis from the managed service path; do not point production at an
   in-process limiter because replica-local counters do not enforce a global
   limit.
4. Confirm login and authenticated admission decisions resume and 503s clear.
5. Review the outage interval for attempted abuse and capacity saturation.
