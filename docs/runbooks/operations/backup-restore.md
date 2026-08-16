# Backup and restore

Postgres and object storage form one recovery point. Redis does not: it carries
cache entries and delivery pointers, while durable intent and leases remain in
Postgres.

Production policy

- Enable encrypted PostgreSQL point-in-time recovery with continuous WAL,
  daily snapshots, cross-region/account copies, and deletion protection.
- Enable object versioning, immutability/soft delete, lifecycle policy and
  cross-region replication for the artifact bucket.
- Keep database and object-store recovery timestamps aligned. Back up secret
  manager configuration and infrastructure definitions separately.
- Target RPO is 15 minutes and RTO is 4 hours unless the product SLO is stricter.
  Run a restore drill at least quarterly and after storage/topology changes.

Restore drill

1. Restore into a new isolated account/cluster. Never restore over production.
2. Block all user/model-provider egress, use new credentials, and record the
   backup ids, timestamps, checksums and operator in the incident/change ticket.
3. Restore Postgres and the object-store version at the same recovery point.
4. Confirm the Alembic revision, table/tenant counts, RLS/forced-RLS flags and
   role grants. Run the full property suite against the restored database.
5. Verify every tenant audit chain including its retention anchor. Hash mismatch
   is a security incident, not a warning to waive.
6. Verify every live document references an object with matching tenant and
   SHA-256 metadata. Verify current collection builds and eval baselines.
7. Start relay, maintenance and workers with outbound model/tool traffic still
   disabled. Confirm pending/outbox runs recover without duplicate effects.
8. Record achieved RPO/RTO and destroy the isolated restore only after evidence
   is retained.

For local database-only mechanics, run `scripts/backup_local_postgres.sh`, then
`scripts/restore_drill_local_postgres.sh <dump>`. This does not back up MinIO and
therefore is not a complete production backup.

