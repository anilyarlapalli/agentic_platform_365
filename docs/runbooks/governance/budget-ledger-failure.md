# Budget ledger failure

Impact: production model calls fail closed before dispatch, or a call that
already completed may not have a settled usage row. Never disable budget
enforcement to clear this alert.

1. Identify the operation (`reservation`, `release`, `settlement`, or `usage`)
   and check Postgres availability, locks, privileges and the current schema.
2. If settlement failed after provider dispatch, reconcile provider usage with
   the reservation and durable run before reopening tenant headroom.
3. Roll back a release or migration that changed ledger behavior. Retain
   reservations until their bounded TTL when outcome is uncertain.
4. Treat any `unmetered_fail_open` in a deployed environment as a control
   incident; verify `BUDGET_FAIL_CLOSED=true` on every API and worker replica.
5. Resolve when writes succeed, spend is attributable, reservations converge,
   and tenant ceilings again refuse projected overspend before dispatch.
