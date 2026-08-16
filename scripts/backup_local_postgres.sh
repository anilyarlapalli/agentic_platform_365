#!/usr/bin/env bash
set -euo pipefail

# Local verification aid. Production uses managed encrypted PITR plus scheduled
# snapshots; see docs/runbooks/operations/backup-restore.md.
backup_dir=${1:-backups}
mkdir -p "$backup_dir"
chmod 700 "$backup_dir"
stamp=$(date -u +%Y%m%dT%H%M%SZ)
final_path="$backup_dir/platform-$stamp.dump"
temporary_path=$(mktemp "$backup_dir/.platform-$stamp.XXXXXX")
cleanup() { rm -f "$temporary_path"; }
trap cleanup EXIT
umask 077

docker compose -f deploy/compose.yml exec -T postgres \
  pg_dump --username=platform_owner --dbname=platform --format=custom \
  --no-password > "$temporary_path"
docker compose -f deploy/compose.yml exec -T postgres \
  pg_restore --list < "$temporary_path" > /dev/null
mv "$temporary_path" "$final_path"
sha256sum "$final_path" > "$final_path.sha256"
trap - EXIT
printf '%s\n' "$final_path"

