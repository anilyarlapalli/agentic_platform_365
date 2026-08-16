# Retention enforcement failed or stopped

Impact: user content, run history, evaluation data, usage, or audit events may
be retained beyond policy. Retention and continuous evaluation share the
maintenance path but are independently measured and guarded.

1. Check both beat replicas, the `maintenance` queue and maintenance workers.
   Confirm `platform.sweep` is still delivered every 30 seconds.
2. Inspect the failing retention database function, statement timeout, locks,
   storage pressure and relay-role function privilege. Do not grant broad table
   access to the maintenance process.
3. Restore or roll back the responsible deployment/migration. Keep bounded
   batches; an unbounded catch-up delete can become an outage.
4. Confirm audit pruning advanced the signed chain anchor and verify every
   affected tenant chain after catch-up.
5. Resolve after successful pass metrics resume, backlog is cleared, and no
   category remains beyond its configured lifecycle window.
