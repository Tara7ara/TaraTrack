# Arquitectura

Detalle técnico completo — el README se queda con la vista de 30 segundos, esto es para cuando quieras profundizar.

## Estructura

```
app/
  main.py       raíz de composición FastAPI: crea la app, monta routers, auth, sync de fondo
  routers/      rutas HTTP por dominio (pendientes, titulo, listas, calendario...), un APIRouter cada uno
  web.py        templates Jinja2 + filtros/globales compartidos entre routers
  matching.py   heurística de emparejamiento TMDB/AniList (usada por buscar y calendario)
  config.py     única lectura de variables de entorno del proyecto
  repo/         todas las queries SQL y las reglas de negocio, por subsistema — sin ORM
  db.py         conexión sqlite3, SCHEMA + MIGRATIONS + INDEXES, WAL
  tmdb.py       cliente de TMDB (es-ES): búsqueda, detalles, episodios, créditos, recomendaciones
  anime.py      Jikan con fallback a AniList: metadatos y personajes de anime
  templates/    Jinja2; partials/ son los fragmentos que devuelven las rutas htmx
  static/       app.css (a mano), htmx, swipe.js, y las imágenes descargadas
scripts/        imports desde un export de Trakt, resync de personajes, backup
```

Regla de capas: `app/routers/*` → `app/repo/*` → `db.py`. **Nunca SQL fuera de `app/repo/`.** Todo el código sigue usando `from app import repo; repo.funcion(...)` — `app/repo/__init__.py` reexporta cada nombre del paquete, así que da igual en qué submódulo viva de verdad.

## Modelo de datos

`users`, `titles` (catálogo compartido entre cuentas), `entries` (una por *título+usuario*, es tu estado de seguimiento — no 1:1 con títulos desde el multiusuario), `episodes` (también compartida), `episode_watches`/`episode_user_state` (visto/comentario privado/favorito, por usuario), `episode_comments` (la única tabla pensada para verse entre usuarios: el debate por episodio), `lists`/`list_items`, `watch_sessions` (rewatches), `characters`/`favorite_characters`, `duels`/`elo_snapshots` (ranking por duelos), `rating_history`, `season_ratings`, `rejected_recommendations`, `recommendations_cache`, `app_settings`.

Detalles que importan al tocar código:

- **Multiusuario**: cualquier función de `app/repo/*` que reciba un id que venga de la URL (`entry_id`, `list_id`, `episode_id`...) tiene que comprobar que pertenece al usuario de la sesión antes de tocarlo — el patrón es un helper `get_owned_*`/`_owned_*_or_404` por dominio (ver `app/repo/lists.py:get_owned_list`, `app/repo/titles.py:get_owned_entry_with_title`). `titles`/`episodes`/`characters` son catálogo compartido a propósito (evita re-descargar los mismos metadatos por cada cuenta) — todo lo demás es privado por defecto salvo `episode_comments`, que es la excepción deliberada.

- Los títulos sin match en TMDB (altas manuales) usan un **`tmdb_id` negativo sintético**. Cualquier código que vaya a llamar a la API real debe filtrarlos.
- `runtime_minutes` en series son **minutos por episodio**; la duración total es `runtime_minutes × episode_count`.
- Las **columnas nuevas** sobre tablas ya desplegadas van en la lista `MIGRATIONS` de `db.py` (`ALTER TABLE` idempotente), **no** editando `SCHEMA`.
- La nota final es o la media ponderada de las cinco categorías (`CATEGORY_WEIGHTS`) o el número directo — nunca las dos.
- "Es anime" (`repo._IS_ANIME_SQL`) se decide por `titles.original_language == 'ja'`, no por el género "Animation" de TMDB (ese también incluye dibujos occidentales).
- El ranking por duelos (listas, waifus, y toda la biblioteca vista en `/duelo`) vive en una columna `elo` por tabla comparable + `duels` (historial) + `elo_snapshots` (foto para calcular "cuánto has ganado desde la última vez").

## Tests y calidad

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt ruff
.venv/bin/pytest -q
.venv/bin/ruff check .        # solo revisión, sin --fix
```

Si tu checkout vive en un montaje de red sin soporte de symlinks (p. ej. CIFS/SMB), `venv` falla al crear `lib64` — crea el venv fuera de esa ruta (p. ej. en tu `$HOME` local) en su lugar. Reglas de Ruff en `pyproject.toml`, deliberadamente pocas (imports/variables sin usar, sintaxis, orden de imports) — nada de estilo/opinión que choque con patrones ya asumidos del proyecto (p. ej. `except Exception` ancho a propósito alrededor de llamadas a TMDB/AniList).

## Con Docker

```bash
echo "TMDB_API_KEY=..." > .env
docker-compose up -d --build
curl -s localhost:8420/healthz     # {"status":"ok"}
```

Publica en `127.0.0.1:8420` a propósito (detrás de un reverse proxy). Los datos persistentes van en volúmenes con nombre: `taratrack-data` (BBDD), `taratrack-posters`, `taratrack-uploads`, `taratrack-profiles`, `taratrack-avatars` (fotos de perfil de cuenta). Reconstruir la imagen no los toca; `docker-compose down -v` **sí los borra**. Si añades un volumen nuevo al `docker-compose.yml`, recuerda que tu propio script de despliegue tiene que sincronizar ese fichero al servidor además de `app/` — un volumen definido solo en tu repo local, sin llegar nunca a desplegarse, falla en silencio.

## Sincronización

Una tarea de fondo (`sync_loop`) ejecuta `repo.sync_library()` cada 12 horas: refresca metadatos de todo lo seguido y los episodios de las series en emisión. Abre **una conexión por título** a propósito, para no dejar la base de datos bloqueada durante minutos.

Se traga las excepciones para sobrevivir a fallos puntuales de TMDB: si deja de traer episodios, ejecútala a mano para ver el error.

```bash
docker exec taratrack python3 -c "from app import repo; repo.sync_library()"
```

## Scripts

Todos son idempotentes: repetirlos no duplica ni pisa datos tuyos.

```bash
TRAKT_EXPORT_DIR=/ruta/json python3 scripts/seed_from_trakt.py            # titulos
TRAKT_EXPORT_DIR=/ruta/json python3 scripts/import_history_from_trakt.py  # episodios
TRAKT_EXPORT_DIR=/ruta/json python3 scripts/import_extra_from_trakt.py    # favoritos, notas, fechas
python3 scripts/resync_anime_characters.py                                # actores TMDB -> personajes AniList
```

Ese es el orden correcto. `scripts/backup.sh` se ejecuta en el host, no dentro del contenedor.

## Notas

- La interfaz no usa Tailwind ni ningún framework JS; los colores salen de las variables de `:root` en `app.css`.
