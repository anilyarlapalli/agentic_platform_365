# Durable run failure spike

1. Group failures by workload, release, terminal/retryable outcome and provider
   error. Inspect checkpoints and side-effect receipts before retrying.
2. Pause the regressing release or workload admission if failures can create
   external effects. Never replay a `needs_reconciliation` run blindly.
3. For retryable provider failure, verify bounded backoff and `Retry-After` are
   applied. For lease loss, check worker termination grace and heartbeat gaps.
4. Reconcile unknown tool effects using the stored idempotency receipt and
   external provider record, then explicitly mark the reconciliation result.
5. Resolve after the failure ratio is normal and all affected runs are either
   succeeded, terminal with an operator-visible cause, or reconciled.

