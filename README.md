# TaraTrack

Tracker personal de series y películas, autoalojado y de un solo usuario — un sustituto propio de Trakt/TV Time. FastAPI + Jinja2 + htmx + SQLite, server-rendered, sin build step de frontend.

Los metadatos vienen de TMDB (y de Jikan/AniList para anime), pero en la base de datos solo se guarda lo que realmente sigues: lo pendiente, lo visto, tus notas, tus listas y tus personajes favoritos.

## Arrancar en local

```bash
pip install -r requirements.txt
TMDB_API_KEY=... TARATRACK_SECRET_KEY=... TARATRACK_PASSWORD=... TARATRACK_DB_PATH=./taratrack.db uvicorn app.main:app --reload
```

Abre http://127.0.0.1:8000. El esquema se crea solo al arrancar (`init_db()`): no hay paso de migración manual.

La única credencial externa necesaria es una API key v3 de [TMDB](https://www.themoviedb.org/settings/api) (gratis). Jikan y AniList no piden key. `TARATRACK_SECRET_KEY`/`TARATRACK_PASSWORD` son del login propio (ver `CLAUDE.md` § Seguridad) — sin `TARATRACK_SECRET_KEY` el arranque falla a propósito, no hay valor por defecto.

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

Publica en `127.0.0.1:8420` a propósito (detrás de un reverse proxy). Los datos persistentes van en cuatro volúmenes con nombre: `taratrack-data` (BBDD), `taratrack-posters`, `taratrack-uploads`, `taratrack-profiles`. Reconstruir la imagen no los toca; `docker-compose down -v` **sí los borra**.

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

`titles`, `entries` (1:1 con títulos, es tu estado de seguimiento), `episodes`, `lists`/`list_items`, `watch_sessions` (rewatches), `characters`/`favorite_characters`, `duels`/`elo_snapshots` (ranking por duelos), `rating_history`, `season_ratings`, `rejected_recommendations`, `recommendations_cache`, `app_settings`.

Detalles que importan al tocar código:

- Los títulos sin match en TMDB (altas manuales) usan un **`tmdb_id` negativo sintético**. Cualquier código que vaya a llamar a la API real debe filtrarlos.
- `runtime_minutes` en series son **minutos por episodio**; la duración total es `runtime_minutes × episode_count`.
- Las **columnas nuevas** sobre tablas ya desplegadas van en la lista `MIGRATIONS` de `db.py` (`ALTER TABLE` idempotente), **no** editando `SCHEMA`.
- La nota final es o la media ponderada de las cinco categorías (`CATEGORY_WEIGHTS`) o el número directo — nunca las dos.
- "Es anime" (`repo._IS_ANIME_SQL`) se decide por `titles.original_language == 'ja'`, no por el género "Animation" de TMDB (ese también incluye dibujos occidentales).
- El ranking por duelos (listas, waifus, y toda la biblioteca vista en `/duelo`) vive en una columna `elo` por tabla comparable + `duels` (historial) + `elo_snapshots` (foto para calcular "cuánto has ganado desde la última vez").

## Índice de afinidad

La app no se queda en "nota media por género" — eso castiga justo lo que más consumes (a más volumen visto de un género, más obras mediocres de ese género entran en la media, y la media baja). En vez de una sola métrica, `app/repo/affinity.py` (~1100 líneas) separa dos ejes por atributo (género/tag/estudio):

- **Apetito**: cuánto lo persigues de verdad — volumen (episodios, agrupados por franquicia vía union-find sobre precuelas), qué cuota de tu consumo se lleva frente al resto de tu biblioteca, ritmo al salir episodios, si lo ves el día 1, rewatches, si lo has curado en listas propias.
- **Calidad**: cuánto te gusta cuando lo ves — percentil 85 de tus notas en ese atributo (no la media, que se hunde con el volumen), percentil 25, disfrute, corrección por tu ranking de duelos (Elo/Glicko).

Ambos se normalizan a percentil dentro de tu propio perfil y se combinan con **media geométrica ponderada + castigo si te alejas de la diagonal**: `base = apetito^0.6 · calidad^0.4`, y si apetito supera a calidad por más de 25 puntos, la afinidad se recorta hasta un 50% — es la señal de "esto lo ves por costumbre, no porque te guste tanto". Esa diferencia (apetito − calidad) es la **inercia**: positiva = lo persigues más de lo que te gusta, negativa = te gusta más de lo que lo persigues (candidato a que le des más bola).

El score final no se enseña en crudo — se calibra contra la distribución real de tus propias notas (percentil: "95%" = mejor que el 95% de lo que has visto en tu vida) y se valida con **backtesting leave-one-out**: recalcula tu perfil sacando cada título puntuado uno a uno, predice su nota sin haberlo visto, y compara contra la real (`scripts/tune_affinity_weights.py` hace un grid search sobre los pesos usando este mismo mecanismo).

**Marcado de hábito** (`entries.is_habit`): series de fondo tipo sitcom/kids que ves por costumbre, no porque las persigas de verdad — se excluyen del eje apetito (no del de calidad, tu nota sigue contando) para que no infle esa métrica solo por volumen. Sin este flag, un estudio como el de una serie con miles de episodios vistos salía con "inercia +47" solo por volumen bruto.

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

- Hay tests en `tests/` (`pytest -q`, ver § Tests y calidad) y Ruff configurado en modo solo-revisión (`pyproject.toml`).
- La interfaz no usa Tailwind ni ningún framework JS; los colores salen de las variables de `:root` en `app.css`.
- `CLAUDE.md` tiene el detalle fino de arquitectura y quirks para trabajar con Claude Code en este repo.
