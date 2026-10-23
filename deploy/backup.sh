#!/bin/sh
# Back up the Docker Compose stack: a database dump and a copy of every photo.
#
#   sh deploy/backup.sh
#
# Run it from anywhere; it works from the repository folder. Writes to
# BACKUP_DIR (default: ./backups in the repository):
#
#   db/apl_db_<date>.dump   one file per run; older than KEEP_DAYS are deleted
#   photos/                 a mirror of the photo bucket, updated each run
#
# To run it every night at 3 am, see docs/deploy-on-your-own-server.md,
# "Backups". Restoring is described there too.
set -eu

cd "$(dirname "$0")/.."
BACKUP_DIR="${BACKUP_DIR:-$(pwd)/backups}"
KEEP_DAYS="${KEEP_DAYS:-14}"
export BACKUP_DIR
mkdir -p "$BACKUP_DIR/db" "$BACKUP_DIR/photos"
stamp="$(date +%Y-%m-%d_%H%M)"
dump="$BACKUP_DIR/db/apl_db_$stamp.dump"

# Written to a temporary name first, so a failed dump never looks like a good one.
docker compose exec -T postgres pg_dump -U postgres --format=custom apl_db > "$dump.part"
mv "$dump.part" "$dump"
echo "database: $dump ($(du -h "$dump" | cut -f1))"

docker compose run --rm photo-backup
echo "photos:   $BACKUP_DIR/photos ($(du -sh "$BACKUP_DIR/photos" | cut -f1))"

find "$BACKUP_DIR/db" -name 'apl_db_*.dump' -mtime +"$KEEP_DAYS" -delete
echo "backup finished $(date)"
