import os
import sqlite3
from contextlib import contextmanager

from app import config

# Atributo de modulo normal (no una funcion) a proposito - conftest.py y los
# tests monkeypatchean db.DB_PATH directamente (monkeypatch.setattr(db, "DB_PATH", ...));
# la lectura real de la variable de entorno vive en config.py, esto solo la reexporta.
DB_PATH = config.DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    password_salt TEXT NOT NULL,
    is_admin INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

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
    user_id INTEGER REFERENCES users(id),
    date TEXT NOT NULL,
    covered INTEGER NOT NULL,
    total_rated INTEGER NOT NULL,
    PRIMARY KEY (user_id, date)
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
    user_id INTEGER REFERENCES users(id),
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
    PRIMARY KEY (user_id, attr_type, attr_name)
);

CREATE TABLE IF NOT EXISTS score_distribution (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER REFERENCES users(id),
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
    user_id INTEGER REFERENCES users(id),
    watched_at TEXT NOT NULL
);

-- Comentario/favorito por episodio, POR USUARIO (2026-09-17, multiusuario Fase 2) -
-- antes vivian como columnas directas en `episodes` (comment/is_favorite), compartidas
-- por toda la instancia. Esas dos columnas se dejan sin usar en `episodes` (no se
-- borran - SQLite no deja quitar columnas sin reconstruir la tabla entera, y no hace
-- falta el riesgo solo por limpieza) pero el codigo ya no las lee ni las escribe.
CREATE TABLE IF NOT EXISTS episode_user_state (
    episode_id INTEGER NOT NULL REFERENCES episodes(id),
    user_id INTEGER NOT NULL REFERENCES users(id),
    comment TEXT,
    is_favorite INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (episode_id, user_id)
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

-- Debate por episodio (2026-09-18, Fase 4 multiusuario) - a diferencia de
-- episode_user_state (privado, un comentario por usuario), esta es la UNICA tabla
-- pensada para ser visible ENTRE usuarios: un hilo cronologico por episodio. Se
-- enseña difuminado en la ficha hasta que el propio usuario marca ese episodio
-- como visto (ver partials/episode_row.html), con opcion de quitar el spoiler
-- antes de tiempo - eso es solo de render, aqui no hay nada que filtrar por usuario.
CREATE TABLE IF NOT EXISTS episode_comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    episode_id INTEGER NOT NULL REFERENCES episodes(id),
    user_id INTEGER NOT NULL REFERENCES users(id),
    body TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
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
    # Multiusuario Fase 2 (2026-09-17): episode_watches necesita saber DE QUIEN es cada
    # visionado - antes de esto era compartido, cualquier usuario marcando un episodio
    # lo marcaba "visto" para todos. El backfill real (asignar el admin a las filas ya
    # existentes) vive en _migrate_multiuser, aqui solo se añade la columna.
    "ALTER TABLE episode_watches ADD COLUMN user_id INTEGER REFERENCES users(id)",
    # Multiusuario Fase 3 (2026-09-18): score_distribution necesita saber de quien es
    # cada score backtesteado - taste_profile/profile_history tambien se vuelven
    # por-usuario, pero sus PRIMARY KEY cambian (attr_type+attr_name -> +user_id;
    # date -> user_id+date) asi que necesitan reconstruccion completa, no un ALTER
    # ADD COLUMN simple - ver _migrate_affinity_multiuser mas abajo.
    "ALTER TABLE score_distribution ADD COLUMN user_id INTEGER REFERENCES users(id)",
    # Bug real (2026-09-18, Tara: "recomienda full anime a la cuenta random"):
    # recommendations_cache era UNA tabla global calculada a partir de TODAS las
    # entries de la instancia (mayoria de Tara), asi que cualquier cuenta nueva veia
    # los recomendados de Tara. No es una tabla de datos del usuario (es cache
    # recalculable), asi que no hace falta reconstruccion: se añade la columna y se
    # tira lo que hubiera - se recalcula sola por usuario en la siguiente visita/sync
    # (ver refresh_recommendations_cache). El DELETE es barato (tabla pequeña) e
    # idempotente, se puede dejar corriendo en cada arranque sin problema.
    "ALTER TABLE recommendations_cache ADD COLUMN user_id INTEGER REFERENCES users(id)",
    "DELETE FROM recommendations_cache WHERE user_id IS NULL",
    # Perfil de cuenta (Tara, 2026-09-18: "como pongo fotos de usr, si quiero cambiar
    # el nombre") - foto propia (avatar), independiente del username en si.
    "ALTER TABLE users ADD COLUMN avatar_path TEXT",
    # Aviso de comentarios nuevos en el nav (Tara, 2026-09-18: "estilo tvtime") -
    # marca de tiempo de "hasta aqui ya lo he visto", por usuario. Backfill a AHORA
    # (no NULL) para que una cuenta ya existente no vea de golpe todo su historico de
    # comentarios como "nuevo" el dia que se despliega esto - solo cuenta lo que pase
    # DESDE este momento en adelante. Idempotente (solo toca las filas sin fecha).
    "ALTER TABLE users ADD COLUMN comments_seen_at TEXT",
    "UPDATE users SET comments_seen_at = datetime('now') WHERE comments_seen_at IS NULL",
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
CREATE INDEX IF NOT EXISTS idx_episode_comments_episode ON episode_comments(episode_id);
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
    # Bug real encontrado probando esta migracion contra una copia de la BBDD real
    # de Tara (2026-09-18, antes de desplegar el multiusuario): "PRAGMA foreign_keys"
    # es un no-op si ya hay una transaccion abierta (documentado en la propia SQLite) -
    # y la hay, porque SCHEMA/MIGRATIONS ya han escrito antes en esta misma conexion.
    # Sin este commit, el DROP TABLE de mas abajo revienta con
    # "FOREIGN KEY constraint failed" en cuanto la tabla tiene filas reales
    # referenciadas desde otras tablas - invisible en tests con BBDD siempre vacia.
    conn.commit()
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


def _migrate_multiuser(conn):
    """Cuentas de verdad (2026-09-17, Fase 1 del multiusuario - hermana y un amigo
    de Tara quieren su propio seguimiento). Antes no habia ningun concepto de
    usuario: entries/lists/rejected_recommendations eran unicos por titulo/nombre
    en TODA la instancia. Se hace UNA vez (flag en app_settings, mismo patron que
    _migrate_rating_scale_0_10 de arriba) - SQLite no deja alterar un UNIQUE ya
    creado, hace falta reconstruir la tabla igual que alli.

    user_id se deja NULLABLE a proposito: los datos ya existentes quedan con el id
    del admin recien creado, pero dejar la columna nullable evita romper insercion
    directa por SQL de tests antiguos que no la mencionan - solo los puntos de
    escritura reales de la app (repo.ensure_entry, repo.create_list,
    repo.reject_recommendation) tienen la obligacion de rellenarla desde ahora.
    Los listados (pendientes/vistas/afinidad/duelos...) TODAVIA no filtran por
    usuario - eso es la Fase 2/3 del plan, deliberadamente fuera de esta migracion."""
    done = conn.execute("SELECT value FROM app_settings WHERE key = 'multiuser_migrated'").fetchone()
    if done:
        return
    # Bug real encontrado probando esta migracion contra una copia de la BBDD real de
    # Tara (2026-09-18): "PRAGMA foreign_keys" no tiene efecto con una transaccion ya
    # abierta (SCHEMA/MIGRATIONS ya escribieron en esta conexion) - sin este commit,
    # el DROP TABLE de mas abajo revienta con "FOREIGN KEY constraint failed" en
    # cuanto entries/lists/rejected_recommendations tienen filas reales referenciadas
    # (list_items, favorite_characters, episode_watches...) - invisible en tests con
    # BBDD siempre vacia, donde nunca hay nada que viole la referencia.
    conn.commit()
    conn.execute("PRAGMA foreign_keys = OFF")

    # 1) Admin desde TARATRACK_PASSWORD/TARATRACK_ADMIN_USERNAME - insert directo
    # (no repo.users.create_user, que ya asume el schema NUEVO de `lists`, que
    # todavia no existe en este punto de la migracion).
    admin_row = conn.execute("SELECT id FROM users ORDER BY id LIMIT 1").fetchone()
    if not admin_row:
        from app import config
        from app.repo.users import hash_password

        password = config.get_password()
        if password:
            password_hash, password_salt = hash_password(password)
            conn.execute(
                "INSERT INTO users (username, password_hash, password_salt, is_admin) VALUES (?, ?, ?, 1)",
                (config.get_admin_username(), password_hash, password_salt),
            )
            admin_row = conn.execute("SELECT id FROM users ORDER BY id LIMIT 1").fetchone()
    admin_id = admin_row["id"] if admin_row else None

    # 2) entries: UNIQUE(title_id) -> UNIQUE(title_id, user_id).
    conn.execute("""
        CREATE TABLE entries_mu (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title_id INTEGER NOT NULL REFERENCES titles(id),
            user_id INTEGER REFERENCES users(id),
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
            rewatch_started_at TEXT,
            UNIQUE(title_id, user_id)
        )
    """)
    conn.execute(
        """INSERT INTO entries_mu
               (id, title_id, user_id, status, rating, comment, added_at, watched_at,
                cat_historia, cat_animacion, cat_personajes, cat_musica, cat_disfrute,
                elo, predicted_score, is_habit, auto_watch, rd, rewatch_started_at)
           SELECT id, title_id, ?, status, rating, comment, added_at, watched_at,
                  cat_historia, cat_animacion, cat_personajes, cat_musica, cat_disfrute,
                  elo, predicted_score, is_habit, auto_watch, rd, rewatch_started_at
           FROM entries""",
        (admin_id,),
    )
    conn.execute("DROP TABLE entries")
    conn.execute("ALTER TABLE entries_mu RENAME TO entries")

    # 3) lists: UNIQUE(name) -> UNIQUE(user_id, name). Conserva la fila "Favoritos"
    # ya existente (con su id/created_at originales) asignandola al admin.
    conn.execute("""
        CREATE TABLE lists_mu (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER REFERENCES users(id),
            name TEXT NOT NULL,
            is_default INTEGER NOT NULL DEFAULT 0,
            order_mode TEXT NOT NULL DEFAULT 'manual',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(user_id, name)
        )
    """)
    conn.execute(
        """INSERT INTO lists_mu (id, user_id, name, is_default, order_mode, created_at)
           SELECT id, ?, name, is_default, order_mode, created_at FROM lists""",
        (admin_id,),
    )
    conn.execute("DROP TABLE lists")
    conn.execute("ALTER TABLE lists_mu RENAME TO lists")

    # 4) rejected_recommendations: UNIQUE(tmdb_id) -> UNIQUE(user_id, tmdb_id).
    conn.execute("""
        CREATE TABLE rejected_recommendations_mu (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER REFERENCES users(id),
            tmdb_id INTEGER NOT NULL,
            type TEXT NOT NULL,
            title TEXT NOT NULL,
            poster_path TEXT,
            seed_title TEXT,
            rejected_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(user_id, tmdb_id)
        )
    """)
    conn.execute(
        """INSERT INTO rejected_recommendations_mu
               (id, user_id, tmdb_id, type, title, poster_path, seed_title, rejected_at)
           SELECT id, ?, tmdb_id, type, title, poster_path, seed_title, rejected_at
           FROM rejected_recommendations""",
        (admin_id,),
    )
    conn.execute("DROP TABLE rejected_recommendations")
    conn.execute("ALTER TABLE rejected_recommendations_mu RENAME TO rejected_recommendations")

    # 5) Instalacion nueva sin ninguna lista previa: el admin necesita su propia
    # "Favoritos" (antes lo hacia init_db() de forma global, ahora es por-usuario).
    if admin_id is not None:
        has_default = conn.execute(
            "SELECT 1 FROM lists WHERE user_id = ? AND is_default = 1", (admin_id,)
        ).fetchone()
        if not has_default:
            conn.execute(
                "INSERT INTO lists (user_id, name, is_default) VALUES (?, 'Favoritos', 1)", (admin_id,)
            )

    # 6) episode_watches: todo lo ya registrado (historial real de visionados, ver el
    # comentario junto a la tabla) era del admin - backfill directo, sin ambiguedad
    # posible porque hasta ahora solo existia un usuario real.
    conn.execute("UPDATE episode_watches SET user_id = ? WHERE user_id IS NULL", (admin_id,))

    # 7) episode_user_state: `episodes.comment`/`is_favorite` (comentario/estrella por
    # episodio suelto) eran columnas compartidas - se vuelcan aqui como datos del
    # admin, una unica vez; el codigo deja de leer/escribir esas dos columnas desde
    # ahora (se quedan en `episodes` sin usar, ver comentario junto a la tabla nueva).
    if admin_id is not None:
        conn.execute(
            """INSERT OR IGNORE INTO episode_user_state (episode_id, user_id, comment, is_favorite)
               SELECT id, ?, comment, is_favorite FROM episodes
               WHERE comment IS NOT NULL OR is_favorite = 1""",
            (admin_id,),
        )

    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        "INSERT OR REPLACE INTO app_settings (key, value) VALUES ('multiuser_migrated', '1')"
    )


def _migrate_affinity_multiuser(conn):
    """Multiusuario Fase 3 (2026-09-18): el indice de afinidad, duelos/Elo y waifus
    pasan a ser por-usuario. `taste_profile`/`profile_history` cambian de PRIMARY KEY
    (hace falta reconstruir, SQLite no permite alterar una PK existente);
    `score_distribution` ya gano su columna `user_id` via MIGRATIONS (ALTER simple,
    sin PK que tocar). Todo lo ya calculado hasta ahora (el unico usuario real hasta
    hoy) se asigna al mismo admin que ya tiene todo lo demas desde la Fase 1."""
    done = conn.execute(
        "SELECT value FROM app_settings WHERE key = 'affinity_multiuser_migrated'"
    ).fetchone()
    if done:
        return
    admin_row = conn.execute("SELECT id FROM users ORDER BY id LIMIT 1").fetchone()
    admin_id = admin_row["id"] if admin_row else None

    # Mismo bug/fix que _migrate_multiuser: sin este commit, "PRAGMA foreign_keys=OFF"
    # es un no-op (transaccion ya abierta) y el DROP TABLE de mas abajo revienta en
    # cuanto taste_profile/profile_history tienen filas reales.
    conn.commit()
    conn.execute("PRAGMA foreign_keys = OFF")

    conn.execute("""
        CREATE TABLE taste_profile_mu (
            user_id INTEGER REFERENCES users(id),
            attr_type TEXT NOT NULL,
            attr_name TEXT NOT NULL,
            sig_volumen REAL, sig_lift REAL, sig_ritmo REAL, sig_dia1 REAL,
            sig_rewatch REAL, sig_curacion REAL, sig_techo REAL, sig_suelo REAL,
            sig_disfrute REAL, sig_media REAL, sig_elo REAL,
            apetito REAL NOT NULL, calidad REAL NOT NULL, inercia REAL NOT NULL,
            afinidad REAL NOT NULL, confianza REAL NOT NULL,
            n_titulos INTEGER NOT NULL, n_episodios INTEGER NOT NULL,
            computed_at TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (user_id, attr_type, attr_name)
        )
    """)
    conn.execute(
        """INSERT INTO taste_profile_mu
               (user_id, attr_type, attr_name, sig_volumen, sig_lift, sig_ritmo, sig_dia1,
                sig_rewatch, sig_curacion, sig_techo, sig_suelo, sig_disfrute, sig_media,
                sig_elo, apetito, calidad, inercia, afinidad, confianza, n_titulos,
                n_episodios, computed_at)
           SELECT ?, attr_type, attr_name, sig_volumen, sig_lift, sig_ritmo, sig_dia1,
                  sig_rewatch, sig_curacion, sig_techo, sig_suelo, sig_disfrute, sig_media,
                  sig_elo, apetito, calidad, inercia, afinidad, confianza, n_titulos,
                  n_episodios, computed_at
           FROM taste_profile""",
        (admin_id,),
    )
    conn.execute("DROP TABLE taste_profile")
    conn.execute("ALTER TABLE taste_profile_mu RENAME TO taste_profile")

    conn.execute("""
        CREATE TABLE profile_history_mu (
            user_id INTEGER REFERENCES users(id),
            date TEXT NOT NULL,
            covered INTEGER NOT NULL,
            total_rated INTEGER NOT NULL,
            PRIMARY KEY (user_id, date)
        )
    """)
    conn.execute(
        "INSERT INTO profile_history_mu (user_id, date, covered, total_rated) SELECT ?, date, covered, total_rated FROM profile_history",
        (admin_id,),
    )
    conn.execute("DROP TABLE profile_history")
    conn.execute("ALTER TABLE profile_history_mu RENAME TO profile_history")

    conn.execute("UPDATE score_distribution SET user_id = ? WHERE user_id IS NULL", (admin_id,))

    # app_settings de clave/valor GLOBAL que pasan a namespacarse por usuario
    # (":<user_id>" al final de la clave) - sin este paso, cualquier ajuste que ya
    # hubieras guardado (pesos de afinidad, orden de waifus) se habria perdido en
    # silencio al buscar la clave nueva y no encontrar nada.
    if admin_id is not None:
        for old_key, new_key in (
            ("affinity_config", f"affinity_config:{admin_id}"),
            ("waifus_order_mode", f"waifus_order_mode:{admin_id}"),
        ):
            row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (old_key,)).fetchone()
            if row is not None:
                conn.execute(
                    "INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, ?)", (new_key, row["value"])
                )
                conn.execute("DELETE FROM app_settings WHERE key = ?", (old_key,))

    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        "INSERT OR REPLACE INTO app_settings (key, value) VALUES ('affinity_multiuser_migrated', '1')"
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
        _migrate_multiuser(conn)
        _migrate_affinity_multiuser(conn)
        conn.executescript(INDEXES)


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
