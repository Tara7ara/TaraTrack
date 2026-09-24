#!/bin/bash
# Backup nocturno: BBDD (copia en caliente con la API de sqlite3) + pósters/uploads.
# Se ejecuta en el host del servidor, no dentro del contenedor.
set -euo pipefail

STAMP=$(date +%F)
BACKUP_DIR="$HOME/taratrack/backups"
KEEP_DAYS=14

mkdir -p "$BACKUP_DIR"

docker exec taratrack mkdir -p /data/backups
docker exec taratrack python3 -c "
import sqlite3
src = sqlite3.connect('/data/taratrack.db')
dst = sqlite3.connect('/data/backups/taratrack-${STAMP}.db')
with dst:
    src.backup(dst)
dst.close()
src.close()
"
docker cp "taratrack:/data/backups/taratrack-${STAMP}.db" "$BACKUP_DIR/"
docker exec taratrack rm -f "/data/backups/taratrack-${STAMP}.db"

mkdir -p "$BACKUP_DIR/posters-${STAMP}" "$BACKUP_DIR/uploads-${STAMP}" "$BACKUP_DIR/profiles-${STAMP}" "$BACKUP_DIR/avatars-${STAMP}"
docker cp "taratrack:/srv/app/static/posters/." "$BACKUP_DIR/posters-${STAMP}/"
docker cp "taratrack:/srv/app/static/uploads/." "$BACKUP_DIR/uploads-${STAMP}/"
docker cp "taratrack:/srv/app/static/profiles/." "$BACKUP_DIR/profiles-${STAMP}/"
# avatars/ se crea con la primera subida; mkdir -p evita que el docker cp de abajo
# falle en una instalación nueva.
docker exec taratrack mkdir -p /srv/app/static/avatars
docker cp "taratrack:/srv/app/static/avatars/." "$BACKUP_DIR/avatars-${STAMP}/"

find "$BACKUP_DIR" -maxdepth 1 -mtime +${KEEP_DAYS} \( -name 'taratrack-*.db' -o -name 'posters-*' -o -name 'uploads-*' -o -name 'profiles-*' -o -name 'avatars-*' \) -exec rm -rf {} +

# Este directorio debe quedar cubierto por la copia de seguridad del propio servidor.

echo "[$(date -Iseconds)] backup OK: taratrack-${STAMP}.db"
