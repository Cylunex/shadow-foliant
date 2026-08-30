#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
migration_dir="$repo_dir/scripts/migrations"

if ! command -v psql >/dev/null 2>&1; then
  echo "psql is required for schema migration" >&2
  exit 8
fi

# Application config uses PG_* while libpq uses PG*. Map in-process without
# printing any value. The script is safe to call independently from deploy.sh.
export PGHOST="${PGHOST:-${PG_HOST:-}}"
export PGPORT="${PGPORT:-${PG_PORT:-}}"
export PGUSER="${PGUSER:-${PG_USER:-}}"
export PGPASSWORD="${PGPASSWORD:-${PG_PASSWORD:-}}"
export PGDATABASE="${PGDATABASE:-${PG_DATABASE:-}}"

has_ledger="$(psql -X -Atqc \
  "SELECT CASE WHEN to_regclass('public.research_schema_migrations') IS NULL THEN 'no' ELSE 'yes' END")"
if [[ "$has_ledger" != "yes" ]]; then
  echo "schema ledger absent; applying idempotent bootstrap"
  psql -X -v ON_ERROR_STOP=1 --single-transaction \
    -c "SELECT pg_advisory_xact_lock(1936482714)" \
    -f "$repo_dir/scripts/init_postgres.sql"
fi

for migration in "$migration_dir"/*.sql; do
  [[ -e "$migration" ]] || continue
  version="$(basename "$migration" .sql)"
  applied="$(psql -X -Atq -v version="$version" \
    -c "SELECT 1 FROM research_schema_migrations WHERE version=:'version'")"
  if [[ "$applied" == "1" ]]; then
    continue
  fi
  echo "applying schema migration $version"
  psql -X -v ON_ERROR_STOP=1 --single-transaction \
    -c "SELECT pg_advisory_xact_lock(1936482714)" \
    -f "$migration"
done

echo "schema migrations are current"
