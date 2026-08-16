#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 || ! -f "$1" ]]; then
  printf 'usage: %s backups/platform-TIMESTAMP.dump\n' "$0" >&2
  exit 64
fi

dump_path=$1
drill_database=platform_restore_drill

# The only destructive target is the fixed, isolated drill database. Production
# restores must always use a separate account/cluster and follow the runbook.
cleanup() {
  docker compose -f deploy/compose.yml exec -T postgres \
    dropdb --username=platform_owner --if-exists "$drill_database" > /dev/null
}
trap cleanup EXIT
cleanup
docker compose -f deploy/compose.yml exec -T postgres \
  createdb --username=platform_owner "$drill_database"
docker compose -f deploy/compose.yml exec -T postgres \
  pg_restore --username=platform_owner --dbname="$drill_database" \
  --exit-on-error --no-password < "$dump_path"
docker compose -f deploy/compose.yml exec -T postgres \
  psql --username=platform_owner --dbname="$drill_database" \
  --set=ON_ERROR_STOP=1 --tuples-only \
  --command="SELECT version_num FROM alembic_version;"
docker compose -f deploy/compose.yml exec -T postgres \
  psql --username=platform_owner --dbname="$drill_database" \
  --set=ON_ERROR_STOP=1 --tuples-only \
  --command="SELECT count(*) FROM tenant;"
printf 'isolated restore drill passed for %s\n' "$dump_path"

