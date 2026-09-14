#!/bin/bash
# Backup nocturno de TaraTrack: BBDD (online-safe via API de sqlite3) + posters/uploads.
# Se ejecuta en el servidor (host), no dentro del contenedor. Ver plan Fase 0 / punto 3 de riesgos.
set -euo pipefail

STAMP=$(date +%F)
BACKUP_DIR="/home/tara/taratrack/backups"
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

mkdir -p "$BACKUP_DIR/posters-${STAMP}" "$BACKUP_DIR/uploads-${STAMP}" "$BACKUP_DIR/profiles-${STAMP}"
docker cp "taratrack:/srv/app/static/posters/." "$BACKUP_DIR/posters-${STAMP}/"
docker cp "taratrack:/srv/app/static/uploads/." "$BACKUP_DIR/uploads-${STAMP}/"
docker cp "taratrack:/srv/app/static/profiles/." "$BACKUP_DIR/profiles-${STAMP}/"

find "$BACKUP_DIR" -maxdepth 1 -mtime +${KEEP_DAYS} \( -name 'taratrack-*.db' -o -name 'posters-*' -o -name 'uploads-*' -o -name 'profiles-*' \) -exec rm -rf {} +

# NOTA: el servidor no tiene montado el NAS (solo el PC de Tara lo tiene).
# Este directorio debe estar cubierto por la rutina de Synology Active Backup
# for Business del servidor (ver Apuntes/Cosas_de_casa/NAS/Copias de Seguridad.md) —
# si esa rutina hace backup bare-metal completo del servidor, ya incluye esta carpeta
# sin configuracion adicional. Si se quiere una copia secundaria explicita en el NAS,
# habria que montar el CIFS en el propio servidor (pendiente, no bloqueante).

echo "[$(date -Iseconds)] backup OK: taratrack-${STAMP}.db"
