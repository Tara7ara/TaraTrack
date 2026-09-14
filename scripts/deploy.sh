#!/bin/bash
# Despliegue de TaraTrack: valida en local, saca un snapshot de seguridad del
# servidor, sincroniza, reconstruye el contenedor y verifica que responde -
# todo en un solo comando en vez del ritual manual documentado en CLAUDE.md
# (rsync + build + docker rm -f + up -d), que ya ha fallado una vez de verdad
# por un rsync mal apuntado que aplano rutas dentro de app/ (ronda 2026-08-21).
#
# Sin git en el proyecto (decision consciente de Tara), este script es la unica
# red de seguridad real que existe hoy: antes de tocar nada en el servidor,
# empaqueta el app/ actual del servidor en un .tar.gz fechado. Si el despliegue
# sale mal, `scripts/deploy.sh --rollback` restaura ese snapshot y reconstruye.
#
# Uso:
#   scripts/deploy.sh                despliegue normal, con todas las comprobaciones
#   scripts/deploy.sh --skip-tests   salta pytest/ruff (solo para iterar rapido - NUNCA para el despliegue real)
#   scripts/deploy.sh --rollback     restaura el snapshot server-side mas reciente y reconstruye
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="/home/tara/.venvs/taratrack/bin"
SERVER="servidor"
SERVER_APP_DIR="/home/tara/taratrack"
SNAPSHOT_DIR="/home/tara/taratrack/deploy-snapshots"
KEEP_SNAPSHOTS=5

cd "$REPO_DIR"

SKIP_TESTS=0
ROLLBACK=0
for arg in "$@"; do
  case "$arg" in
    --skip-tests) SKIP_TESTS=1 ;;
    --rollback) ROLLBACK=1 ;;
    *) echo "Argumento desconocido: $arg (usa --skip-tests o --rollback)" >&2; exit 1 ;;
  esac
done

# --- Rollback: restaura el ultimo snapshot server-side y reconstruye ---
if [ "$ROLLBACK" = "1" ]; then
  echo "== Restaurando el snapshot server-side mas reciente =="
  ssh "$SERVER" "
    set -euo pipefail
    LATEST=\$(ls -t '$SNAPSHOT_DIR'/app-*.tar.gz 2>/dev/null | head -1)
    if [ -z \"\$LATEST\" ]; then
      echo 'No hay ningun snapshot que restaurar en $SNAPSHOT_DIR.' >&2
      exit 1
    fi
    echo \"Restaurando \$LATEST\"
    rm -rf '$SERVER_APP_DIR/app'
    tar -xzf \"\$LATEST\" -C '$SERVER_APP_DIR'
    cd '$SERVER_APP_DIR'
    docker-compose build taratrack
    docker rm -f taratrack
    docker-compose up -d
  "
  echo "== Esperando a que el contenedor responda tras el rollback =="
  sleep 3
  CODE=$(ssh "$SERVER" "curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8420/login" || echo "000")
  echo "Codigo HTTP de /login tras rollback: $CODE"
  [ "$CODE" = "200" ] || { echo "El rollback no dejo el contenedor sano - revisa 'ssh servidor docker logs taratrack'." >&2; exit 1; }
  echo "== Rollback OK =="
  exit 0
fi

# --- Comprobaciones locales, antes de tocar el servidor ---
echo "== 1/5 Sintaxis Python (py_compile) =="
find app scripts -name "*.py" -not -path "*/__pycache__/*" -print0 | xargs -0 "$VENV/python" -m py_compile

echo "== 2/5 Ruff (informativo - pyproject.toml lo marca 'solo revision', no bloquea el deploy) =="
"$VENV/ruff" check . || echo "(avisos de Ruff arriba - revisalos si quieres, no detienen el despliegue)"

if [ "$SKIP_TESTS" = "0" ]; then
  echo "== 3/5 pytest =="
  "$VENV/pytest" -q
else
  echo "== 3/5 pytest (SALTADO con --skip-tests) =="
fi

echo "== 4/5 Plantillas Jinja2 (compilacion en seco) =="
"$VENV/python" scripts/check_templates.py

echo "== 5/5 CSS: llaves balanceadas =="
"$VENV/python" -c "
import sys
css = open('app/static/css/app.css').read()
o, c = css.count('{'), css.count('}')
if o != c:
    print(f'Llaves desbalanceadas en app.css: {o} abren, {c} cierran', file=sys.stderr)
    sys.exit(1)
print(f'OK: {o} bloques')
"

echo "== Todo verde. Desplegando =="

echo "-- Snapshot de seguridad del app/ actual en el servidor --"
ssh "$SERVER" "
  set -euo pipefail
  mkdir -p '$SNAPSHOT_DIR'
  STAMP=\$(date +%Y%m%d-%H%M%S)
  tar -czf '$SNAPSHOT_DIR/app-'\$STAMP'.tar.gz' -C '$SERVER_APP_DIR' app
  ls -t '$SNAPSHOT_DIR'/app-*.tar.gz | tail -n +$((KEEP_SNAPSHOTS + 1)) | xargs -r rm -f
  echo \"Snapshot guardado: app-\$STAMP.tar.gz (se conservan los ultimos $KEEP_SNAPSHOTS)\"
"

echo "-- rsync app/ -> servidor (con --delete: la estructura ya ha cambiado de raiz alguna vez) --"
rsync -av --delete --exclude "__pycache__/" "$REPO_DIR/app/" "$SERVER:$SERVER_APP_DIR/app/"

echo "-- Build + recreate del contenedor --"
# docker-compose v1.29.2 (EOL, version instalada en el servidor) revienta con
# KeyError: 'ContainerConfig' si se recrea in-place - de ahi el rm -f antes de up -d.
ssh "$SERVER" "
  set -euo pipefail
  cd '$SERVER_APP_DIR'
  docker-compose build taratrack
  docker rm -f taratrack
  docker-compose up -d
"

echo "-- Esperando a que el contenedor responda (hasta 30s) --"
OK=0
for _ in $(seq 1 15); do
  sleep 2
  CODE=$(ssh "$SERVER" "curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8420/login" || echo "000")
  if [ "$CODE" = "200" ]; then
    OK=1
    break
  fi
done

if [ "$OK" != "1" ]; then
  echo "El contenedor no respondio 200 en /login tras 30s (ultimo codigo: $CODE). Logs recientes:" >&2
  ssh "$SERVER" "docker logs --tail 60 taratrack" >&2
  echo >&2
  echo "Despliegue con problemas. Para revertir al snapshot anterior: scripts/deploy.sh --rollback" >&2
  exit 1
fi

echo "-- Buscando trazas de error en los logs del ultimo minuto --"
ERRORS=$(ssh "$SERVER" "docker logs --since 1m taratrack 2>&1" | grep -iE "traceback|error|exception" || true)
if [ -n "$ERRORS" ]; then
  echo "AVISO: hay lineas sospechosas en los logs recientes (puede ser ruido de arranque, revisar a mano):"
  echo "$ERRORS"
fi

echo "== Desplegado y respondiendo 200 en /login =="
