# Model provider failures

Impact: agent turns, ingestion, evaluation, or onboarding may be failing after
budget reservation. Do not disable budget enforcement or bypass the governed
LLM client.

1. Group failures by provider operation, model, release and error type. Confirm
   whether the provider is throttling, unavailable, or rejecting a request.
2. Verify configured endpoints, identity, quota and network egress without
   logging prompts, responses, tool arguments, or credentials.
3. For throttling, preserve bounded retry with jitter and provider
   `Retry-After`; reduce admission or worker concurrency below quota.
4. Roll back a release-specific regression. Do not retry terminal requests or
   any run whose external effect requires reconciliation.
5. Resolve when the error ratio and latency return to baseline, reservations
   are released or settled, and affected durable runs are visibly recovered.
