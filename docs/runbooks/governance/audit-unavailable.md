# Mandatory audit unavailable

Impact: privileged and mutating operations fail closed with 503. Reads may
continue. Never set `AUDIT_FAIL_CLOSED=false` in staging or production.

1. Check Postgres availability, app-role INSERT privilege, sequence access and
   audit trigger errors. Confirm ordinary tenant reads still obey RLS.
2. Roll back the application or migration that changed the audit append path.
   Do not grant UPDATE/DELETE/TRUNCATE on audit tables.
3. After recovery, run per-tenant chain verification including the retention
   anchor. Any hash mismatch is a security incident; preserve database and log
   snapshots before further writes.
4. Replay the refused business operation from the client only after audit is
   healthy; the failed request did not enter the handler.
5. Resolve after required audit appends succeed and chain verification passes.

