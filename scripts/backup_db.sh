#!/usr/bin/env bash
# Nightly pg_dump with 7-day retention. Run from cron on the DB host.
# Usage: POSTGRES_PASSWORD=... ./scripts/backup_db.sh [/backup/dir]
set -euo pipefail

BACKUP_DIR="${1:-/var/backups/downvedio}"
RETENTION_DAYS=7
STAMP="$(date +%Y%m%d_%H%M%S)"
mkdir -p "$BACKUP_DIR"

export PGPASSWORD="${POSTGRES_PASSWORD:?POSTGRES_PASSWORD required}"
pg_dump -h "${PGHOST:-localhost}" -U "${PGUSER:-postgres}" -d "${PGDATABASE:-downvedio}" \
  -F c -f "$BACKUP_DIR/downvedio_$STAMP.dump"

find "$BACKUP_DIR" -name 'downvedio_*.dump' -mtime +"$RETENTION_DAYS" -delete
echo "backup ok: $BACKUP_DIR/downvedio_$STAMP.dump"
