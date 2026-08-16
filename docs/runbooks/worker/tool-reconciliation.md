# Tool execution failure or reconciliation required

Impact: a write tool may have completed externally without a committed success
receipt. Automatic replay is intentionally refused.

1. Find the execution and run by trace/run id. Compare its argument hash,
   approval, checkpoint and idempotency key; do not log raw tool arguments.
2. Query the external provider using its idempotency/reference record to decide
   whether the effect happened. Never infer failure from a timeout alone.
3. If the effect happened, record the result through the reconciliation path.
   If it did not, explicitly authorize a retry with the original arguments and
   idempotency contract.
4. For read-only failure spikes, inspect dependency health and bounded timeout
   behavior. Roll back a release-specific handler regression.
5. Resolve only when every uncertain write is explicitly reconciled and normal
   tool success/error rates have returned to baseline.
