import os
import sqlite3
from contextlib import contextmanager

from app import config

# Atributo de modulo normal (no una funcion) a proposito - conftest.py y los
# tests monkeypatchean db.DB_PATH directamente (monkeypatch.setattr(db, "DB_PATH", ...));
# la lectura real de la variable de entorno vive en config.py, esto solo la reexporta.
DB_PATH = config.DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS titles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tmdb_id INTEGER UNIQUE NOT NULL,
    type TEXT NOT NULL CHECK(type IN ('movie', 'show')),
    title TEXT NOT NULL,
    year INTEGER,
    poster_path TEXT,
    overview TEXT,
    genres TEXT,
    imdb_id TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title_id INTEGER NOT NULL UNIQUE REFERENCES titles(id),
    status TEXT NOT NULL CHECK(status IN ('pending', 'watched')) DEFAULT 'pending',
    rating REAL CHECK(rating IS NULL OR (rating >= 0 AND rating <= 10)),
    comment TEXT,
    added_at TEXT NOT NULL DEFAULT (datetime('now')),
    watched_at TEXT
);

CREATE TABLE IF NOT EXISTS episodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title_id INTEGER NOT NULL REFERENCES titles(id),
    season_number INTEGER NOT NULL,
    episode_number INTEGER NOT NULL,
    name TEXT,
    air_date TEXT,
    watched_at TEXT,
    UNIQUE(title_id, season_number, episode_number)
);

CREATE TABLE IF NOT EXISTS lists (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    is_default INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS list_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    list_id INTEGER NOT NULL REFERENCES lists(id),
    entry_id INTEGER NOT NULL REFERENCES entries(id),
    added_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(list_id, entry_id)
);

CREATE TABLE IF NOT EXISTS watch_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id INTEGER NOT NULL REFERENCES entries(id),
    watched_at TEXT NOT NULL,
    speed TEXT NOT NULL DEFAULT '1x',
    notes TEXT
);

CREATE TABLE IF NOT EXISTS characters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title_id INTEGER NOT NULL REFERENCES titles(id),
    tmdb_person_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    character_name TEXT,
    profile_path TEXT,
    UNIQUE(title_id, tmdb_person_id)
);

CREATE TABLE IF NOT EXISTS favorite_characters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id INTEGER NOT NULL REFERENCES entries(id),
    character_id INTEGER NOT NULL REFERENCES characters(id),
    notes TEXT,
    UNIQUE(entry_id, character_id)
);

CREATE TABLE IF NOT EXISTS rejected_recommendations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tmdb_id INTEGER UNIQUE NOT NULL,
    type TEXT NOT NULL,
    title TEXT NOT NULL,
    poster_path TEXT,
    rejected_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS recommendations_cache (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tmdb_id INTEGER NOT NULL,
    type TEXT NOT NULL,
    title TEXT NOT NULL,
    year INTEGER,
    poster_url TEXT,
    overview TEXT,
    vote_average REAL,
    popularity REAL,
    is_adult INTEGER NOT NULL DEFAULT 0,
    seeds TEXT NOT NULL,
    score REAL NOT NULL,
    computed_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS rating_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id INTEGER NOT NULL REFERENCES entries(id),
    rating REAL,
    cat_historia REAL,
    cat_animacion REAL,
    cat_personajes REAL,
    cat_musica REAL,
    cat_disfrute REAL,
    comment TEXT,
    replaced_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS duels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    table_name TEXT NOT NULL,
    winner_id INTEGER NOT NULL,
    loser_id INTEGER NOT NULL,
    result REAL NOT NULL DEFAULT 1.0,
    decided_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS season_cache (
    season TEXT NOT NULL,
    year INTEGER NOT NULL,
    data TEXT NOT NULL,
    computed_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (season, year)
);

CREATE TABLE IF NOT EXISTS elo_snapshots (
    table_name TEXT NOT NULL,
    item_id INTEGER NOT NULL,
    elo REAL NOT NULL,
    rank INTEGER NOT NULL,
    snapshot_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (table_name, item_id)
);

CREATE TABLE IF NOT EXISTS profile_history (
    date TEXT PRIMARY KEY,
    covered INTEGER NOT NULL,
    total_rated INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS season_ratings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id INTEGER NOT NULL REFERENCES entries(id),
    season_number INTEGER NOT NULL,
    rating REAL CHECK(rating IS NULL OR (rating >= 0 AND rating <= 10)),
    comment TEXT,
    cat_historia REAL,
    cat_animacion REAL,
    cat_personajes REAL,
    cat_musica REAL,
    cat_disfrute REAL,
    rated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(entry_id, season_number)
);

CREATE TABLE IF NOT EXISTS taste_profile (
    attr_type TEXT NOT NULL,
    attr_name TEXT NOT NULL,
    sig_volumen REAL,
    sig_lift REAL,
    sig_ritmo REAL,
    sig_dia1 REAL,
    sig_rewatch REAL,
    sig_curacion REAL,
    sig_techo REAL,
    sig_suelo REAL,
    sig_disfrute REAL,
    sig_media REAL,
    sig_elo REAL,
    apetito REAL NOT NULL,
    calidad REAL NOT NULL,
    inercia REAL NOT NULL,
    afinidad REAL NOT NULL,
    confianza REAL NOT NULL,
    n_titulos INTEGER NOT NULL,
    n_episodios INTEGER NOT NULL,
    computed_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (attr_type, attr_name)
);

CREATE TABLE IF NOT EXISTS score_distribution (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_score REAL NOT NULL,
    computed_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Historial real de visionados por episodio (2026-08-20, "Volver a ver" estilo Trakt):
-- episodes.watched_at sigue siendo la fecha MAS RECIENTE (cache, todo el codigo viejo
-- que ya lo lee como "la fecha" sigue funcionando igual), pero cada marcado/re-marcado
-- deja aqui su propia fila - "Volver a ver" ya no borra episodes.watched_at, asi que
-- ninguna fecha antigua se pierde nunca, ni siquiera al re-marcar el mismo episodio.
CREATE TABLE IF NOT EXISTS episode_watches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    episode_id INTEGER NOT NULL REFERENCES episodes(id),
    watched_at TEXT NOT NULL
);

-- Dia de emision manual por titulo (Tara, notas.txt: "lunes Grand Blue pero en el
-- calendario sale los martes, es molesto ir a buscar") - pisa el weekday calculado en
-- UTC desde airing_at/startDate de AniList (app/anime.py) SOLO para ese titulo, sin
-- tocar el calculo automatico que funciona bien para el resto. Keyed por anilist_id,
-- no por titles.id, porque el calendario de temporada enseña anime aun no anadido a
-- la biblioteca (anilist_id es el unico identificador que tienen todos los items).
CREATE TABLE IF NOT EXISTS weekday_overrides (
    anilist_id INTEGER PRIMARY KEY,
    weekday INTEGER NOT NULL
);
"""

# Columnas anadidas sobre esquemas ya desplegados - CREATE TABLE IF NOT EXISTS no altera
# tablas existentes, asi que las columnas nuevas van aqui como ALTER TABLE idempotente.
MIGRATIONS = [
    "ALTER TABLE titles ADD COLUMN show_status TEXT",
    "ALTER TABLE titles ADD COLUMN next_episode_air_date TEXT",
    "ALTER TABLE titles ADD COLUMN next_episode_label TEXT",
    "ALTER TABLE titles ADD COLUMN vote_average REAL",
    "ALTER TABLE titles ADD COLUMN runtime_minutes INTEGER",
    "ALTER TABLE entries ADD COLUMN cat_historia REAL",
    "ALTER TABLE entries ADD COLUMN cat_animacion REAL",
    "ALTER TABLE entries ADD COLUMN cat_personajes REAL",
    "ALTER TABLE entries ADD COLUMN cat_musica REAL",
    "ALTER TABLE entries ADD COLUMN cat_disfrute REAL",
    "ALTER TABLE titles ADD COLUMN episode_count INTEGER",
    "ALTER TABLE list_items ADD COLUMN position INTEGER",
    "ALTER TABLE episodes ADD COLUMN comment TEXT",
    "ALTER TABLE episodes ADD COLUMN is_favorite INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE favorite_characters ADD COLUMN position INTEGER",
    "ALTER TABLE titles ADD COLUMN is_adult INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE titles ADD COLUMN release_date TEXT",
    "ALTER TABLE rejected_recommendations ADD COLUMN seed_title TEXT",
    "ALTER TABLE list_items ADD COLUMN elo REAL NOT NULL DEFAULT 1500",
    "ALTER TABLE favorite_characters ADD COLUMN elo REAL NOT NULL DEFAULT 1500",
    "ALTER TABLE lists ADD COLUMN order_mode TEXT NOT NULL DEFAULT 'manual'",
    "ALTER TABLE duels ADD COLUMN result REAL NOT NULL DEFAULT 1.0",
    "ALTER TABLE entries ADD COLUMN elo REAL NOT NULL DEFAULT 1500",
    "ALTER TABLE titles ADD COLUMN original_language TEXT",
    "ALTER TABLE titles ADD COLUMN anilist_id INTEGER",
    "ALTER TABLE titles ADD COLUMN anilist_genres TEXT",
    "ALTER TABLE titles ADD COLUMN anilist_studio TEXT",
    "ALTER TABLE titles ADD COLUMN custom_poster INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE entries ADD COLUMN predicted_score INTEGER",
    "ALTER TABLE titles ADD COLUMN anilist_tags TEXT",
    "ALTER TABLE titles ADD COLUMN anilist_prequel_ids TEXT",
    "ALTER TABLE titles ADD COLUMN anilist_cross_rec_ids TEXT",
    # Sin NOT NULL a proposito: tres estados - NULL (no decidido todavia),
    # 0 (decidido, no es habito), 1 (habito). Con un booleano a 0 por defecto no se
    # podria distinguir "no lo es" de "aun no lo he mirado". Marcado siempre a mano
    # (ficha o formulario de puntuar), sin sugerencias automaticas - el triaje en
    # bloque y el goteo se quitaron 2026-08-20 (Tara: "una vez lo pongo es porque
    # lo se al 100%", no queria que la app le propusiera candidatos).
    "ALTER TABLE entries ADD COLUMN is_habit INTEGER",
    "ALTER TABLE titles ADD COLUMN original_title TEXT",
    "ALTER TABLE titles ADD COLUMN anilist_title_romaji TEXT",
    "ALTER TABLE titles ADD COLUMN anilist_title_english TEXT",
    "ALTER TABLE titles ADD COLUMN anilist_match_attempts INTEGER NOT NULL DEFAULT 0",
    # Autover (2026-08-15): distinto de is_habit a proposito - habito solo afecta a
    # las estadisticas de afinidad (silencioso), autover marca vistos episodios reales
    # sin que Tara lo pida - mezclarlos habria hecho que marcar algo "habito" tocara
    # el historial real de golpe. Booleano simple (no tri-estado): es una decision
    # explicita de Tara sobre una serie concreta, no algo que se pregunte solo.
    "ALTER TABLE entries ADD COLUMN auto_watch INTEGER NOT NULL DEFAULT 0",
    # Glicko (2026-08-15): sustituye al Elo de K fijo para el duelo (entries/list_items/
    # favorite_characters) - RD es la incertidumbre de cada item (350=nada seguro,
    # baja con cada duelo), universal para los 3 (no depende de tener nota). Ver
    # repo._glicko_update.
    "ALTER TABLE entries ADD COLUMN rd REAL NOT NULL DEFAULT 350",
    "ALTER TABLE list_items ADD COLUMN rd REAL NOT NULL DEFAULT 350",
    "ALTER TABLE favorite_characters ADD COLUMN rd REAL NOT NULL DEFAULT 350",
    # "Volver a ver" estilo Trakt (2026-08-20): fecha en la que empezo la ronda de
    # rewatch ACTIVA (NULL = nunca, o ya puesta al dia). Compararla con
    # episodes.watched_at (nunca borrado ahora) es lo que distingue "visto en esta
    # ronda" de "visto antes de empezar a re-ver" sin perder ninguna fecha vieja - ver
    # repo._episode_pending_sql/repo.next_unwatched_episode.
    "ALTER TABLE entries ADD COLUMN rewatch_started_at TEXT",
    # Backfill de episode_watches (2026-08-21, "volver a ver un episodio suelto"):
    # la tabla solo tenia filas para lo marcado/re-marcado DESDE el 2026-08-20, asi
    # que las estadisticas (episodios vistos, tiempo total, por mes/año) no podian
    # pasar a leerla sin perder de golpe todo el historico. NOT EXISTS la hace
    # idempotente (barata en cada arranque tras la primera vez, usa el indice de
    # episode_watches.episode_id) - una fila por episodio ya visto con su fecha real.
    """INSERT INTO episode_watches (episode_id, watched_at)
       SELECT id, watched_at FROM episodes
       WHERE watched_at IS NOT NULL
         AND NOT EXISTS (SELECT 1 FROM episode_watches WHERE episode_watches.episode_id = episodes.id)""",
]

# Indices para que las subqueries de inicio/calendario no barran tablas enteras.
INDEXES = """
CREATE INDEX IF NOT EXISTS idx_episodes_title ON episodes(title_id);
CREATE INDEX IF NOT EXISTS idx_episodes_air_date ON episodes(air_date);
CREATE INDEX IF NOT EXISTS idx_episodes_watched ON episodes(watched_at);
CREATE INDEX IF NOT EXISTS idx_episodes_title_watched ON episodes(title_id, watched_at);
CREATE INDEX IF NOT EXISTS idx_entries_status ON entries(status);
CREATE INDEX IF NOT EXISTS idx_list_items_list ON list_items(list_id);
CREATE INDEX IF NOT EXISTS idx_rating_history_entry ON rating_history(entry_id);
CREATE INDEX IF NOT EXISTS idx_season_ratings_entry ON season_ratings(entry_id);
CREATE INDEX IF NOT EXISTS idx_duels_table ON duels(table_name);
-- Solo table_name estaba indexado - winner_id/loser_id (los que usa _duel_counts
-- para "cuantos duelos lleva cada uno") dependian de un scan tras el filtro de
-- table_name. Con los datos actuales (173 filas) es intrascendente (confirmado
-- midiendo en real, 15-30ms por voto), pero cuesta cero añadirlo ya de cara a
-- cuando la tabla crezca de verdad.
CREATE INDEX IF NOT EXISTS idx_duels_winner ON duels(table_name, winner_id);
CREATE INDEX IF NOT EXISTS idx_duels_loser ON duels(table_name, loser_id);
CREATE INDEX IF NOT EXISTS idx_score_distribution_raw ON score_distribution(raw_score);
CREATE INDEX IF NOT EXISTS idx_watch_sessions_entry ON watch_sessions(entry_id);
CREATE INDEX IF NOT EXISTS idx_episode_watches_episode ON episode_watches(episode_id);
"""


def _migrate_rating_scale_0_10(conn):
    """SQLite no deja tocar un CHECK con ALTER TABLE, asi que subir la escala de notas
    de 1-10 a 0-10 (Tara: "nota del 0 al 10, no del 1 al 10") exige reconstruir las
    tablas con nota (entries, season_ratings). Se hace UNA vez (marca en app_settings) -
    crear tabla nueva + copiar + borrar la vieja + renombrar, con foreign_keys=OFF
    mientras dura para que el hueco intermedio sin la tabla no rompa las referencias de
    list_items/favorite_characters/watch_sessions/rating_history/season_ratings (todo
    dentro de la misma transaccion, nadie mas ve ese estado intermedio)."""
    done = conn.execute(
        "SELECT value FROM app_settings WHERE key = 'rating_scale_0_10'"
    ).fetchone()
    if done:
        return
    conn.execute("PRAGMA foreign_keys = OFF")
    # Columnas posteriores a cat_disfrute (elo, is_habit, auto_watch, rd,
    # rewatch_started_at...) tienen que estar aqui tambien - antes de arreglar esto
    # (2026-08-20) esta reconstruccion se las comia enteras en cualquier BBDD fresca
    # (bug ya documentado, "inofensivo en produccion" porque el flag de arriba ya
    # esta puesto ahi, pero rompia tests/instalaciones nuevas de verdad).
    conn.execute("""
        CREATE TABLE entries_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title_id INTEGER NOT NULL UNIQUE REFERENCES titles(id),
            status TEXT NOT NULL CHECK(status IN ('pending', 'watched')) DEFAULT 'pending',
            rating REAL CHECK(rating IS NULL OR (rating >= 0 AND rating <= 10)),
            comment TEXT,
            added_at TEXT NOT NULL DEFAULT (datetime('now')),
            watched_at TEXT,
            cat_historia REAL, cat_animacion REAL, cat_personajes REAL, cat_musica REAL, cat_disfrute REAL,
            elo REAL NOT NULL DEFAULT 1500,
            predicted_score INTEGER,
            is_habit INTEGER,
            auto_watch INTEGER NOT NULL DEFAULT 0,
            rd REAL NOT NULL DEFAULT 350,
            rewatch_started_at TEXT
        )
    """)
    conn.execute("""
        INSERT INTO entries_new
            (id, title_id, status, rating, comment, added_at, watched_at,
             cat_historia, cat_animacion, cat_personajes, cat_musica, cat_disfrute,
             elo, predicted_score, is_habit, auto_watch, rd, rewatch_started_at)
        SELECT id, title_id, status, rating, comment, added_at, watched_at,
               cat_historia, cat_animacion, cat_personajes, cat_musica, cat_disfrute,
               elo, predicted_score, is_habit, auto_watch, rd, rewatch_started_at
        FROM entries
    """)
    conn.execute("DROP TABLE entries")
    conn.execute("ALTER TABLE entries_new RENAME TO entries")

    conn.execute("""
        CREATE TABLE season_ratings_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entry_id INTEGER NOT NULL REFERENCES entries(id),
            season_number INTEGER NOT NULL,
            rating REAL CHECK(rating IS NULL OR (rating >= 0 AND rating <= 10)),
            comment TEXT,
            cat_historia REAL, cat_animacion REAL, cat_personajes REAL, cat_musica REAL, cat_disfrute REAL,
            rated_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(entry_id, season_number)
        )
    """)
    conn.execute("INSERT INTO season_ratings_new SELECT * FROM season_ratings")
    conn.execute("DROP TABLE season_ratings")
    conn.execute("ALTER TABLE season_ratings_new RENAME TO season_ratings")

    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        "INSERT OR REPLACE INTO app_settings (key, value) VALUES ('rating_scale_0_10', '1')"
    )


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with get_connection() as conn:
        conn.executescript(SCHEMA)
        for stmt in MIGRATIONS:
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError as e:
                if "duplicate column" not in str(e):
                    raise
        _migrate_rating_scale_0_10(conn)
        conn.executescript(INDEXES)
        conn.execute(
            "INSERT OR IGNORE INTO lists (name, is_default) VALUES ('Favoritos', 1)"
        )


@contextmanager
def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # Si otro proceso tiene la escritura pillada (sync de fondo), esperar en vez de petar.
    conn.execute("PRAGMA busy_timeout = 15000")
    # WAL: lecturas no bloqueadas por escrituras (la sync de fondo escribe mientras navegas).
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()
