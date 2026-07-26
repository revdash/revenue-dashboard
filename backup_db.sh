#!/bin/bash
# Daily backup of the Revdash SQLite database.
# Keeps 90 days of dated backups, deletes older ones automatically.
#
# Run this ON THE HOST (not inside the container) -- it just needs
# read access to the same /data volume path the dashboard container
# uses.

set -e

DATA_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/data"
BACKUP_DIR="$DATA_DIR/backups"
DB_FILE="$DATA_DIR/income.db"
DATE=$(date '+%Y-%m-%d')

if [ ! -f "$DB_FILE" ]; then
  echo "[backup] $DB_FILE not found, nothing to back up yet."
  exit 0
fi

mkdir -p "$BACKUP_DIR"

# sqlite3's own backup command is safer than a plain file copy --
# it won't produce a corrupt snapshot if a write happens mid-copy.
if command -v sqlite3 &>/dev/null; then
  sqlite3 "$DB_FILE" ".backup '$BACKUP_DIR/income_$DATE.db'"
else
  cp "$DB_FILE" "$BACKUP_DIR/income_$DATE.db"
fi

echo "[backup] Saved $BACKUP_DIR/income_$DATE.db"

# Keep 90 days, delete anything older.
find "$BACKUP_DIR" -name "income_*.db" -mtime +90 -delete

echo "[backup] Done. $(ls "$BACKUP_DIR" | wc -l) backups retained."
