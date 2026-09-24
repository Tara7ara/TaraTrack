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

-- Historial de visionados por episodio: cada marcado o re-marcado deja su propia fila,
-- así "Volver a ver" no pierde ninguna fecha.
CREATE TABLE IF NOT EXISTS episode_watches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    episode_id INTEGER NOT NULL REFERENCES episodes(id),
    user_id INTEGER REFERENCES users(id),
    watched_at TEXT NOT NULL
);

-- Comentario y favorito por episodio, de cada usuario. Las columnas antiguas
-- episodes.comment/is_favorite se quedan sin usar (SQLite no permite quitarlas sin
-- reconstruir la tabla).
CREATE TABLE IF NOT EXISTS episode_user_state (
    episode_id INTEGER NOT NULL REFERENCES episodes(id),
    user_id INTEGER NOT NULL REFERENCES users(id),
    comment TEXT,
    is_favorite INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (episode_id, user_id)
);

-- Día de emisión corregido a mano por título: pisa el weekday calculado en UTC
-- (app/anime.py). Por anilist_id y no por titles.id, porque el calendario de temporada
-- enseña anime que aún no está en la biblioteca.
CREATE TABLE IF NOT EXISTS weekday_overrides (
    anilist_id INTEGER PRIMARY KEY,
    weekday INTEGER NOT NULL
);

-- Tarjeta del calendario de temporada -> título de TMDB al que se resolvió.
-- Una tarjeta de "Temporada 2" en AniList es el MISMO show en TMDB, asi que ni
-- titles.anilist_id ni el titulo exacto la encuentran: se guarda al añadir/abrir
-- desde el propio calendario para poder marcarla luego como "ya en tu lista".
CREATE TABLE IF NOT EXISTS calendar_links (
    anilist_id INTEGER PRIMARY KEY,
    title_id INTEGER NOT NULL REFERENCES titles(id)
);

-- Debate por episodio: única tabla visible entre usuarios, un hilo cronológico por
-- episodio. El difuminado de spoilers es solo de render (partials/episode_row.html).
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
    # Tres estados a propósito: NULL (sin decidir), 0 (no es hábito), 1 (hábito).
    # Con un booleano a 0 no se distinguiría "no lo es" de "aún no lo he mirado".
    "ALTER TABLE entries ADD COLUMN is_habit INTEGER",
    "ALTER TABLE titles ADD COLUMN original_title TEXT",
    "ALTER TABLE titles ADD COLUMN anilist_title_romaji TEXT",
    "ALTER TABLE titles ADD COLUMN anilist_title_english TEXT",
    "ALTER TABLE titles ADD COLUMN anilist_match_attempts INTEGER NOT NULL DEFAULT 0",
    # Autover, separado de is_habit: el hábito solo afecta a la afinidad; el autover
    # marca episodios vistos.
    "ALTER TABLE entries ADD COLUMN auto_watch INTEGER NOT NULL DEFAULT 0",
    # Glicko: RD es la incertidumbre de cada elemento en los duelos (350 = nada
    # seguro, baja con cada duelo). Ver repo._glicko_update.
    "ALTER TABLE entries ADD COLUMN rd REAL NOT NULL DEFAULT 350",
    "ALTER TABLE list_items ADD COLUMN rd REAL NOT NULL DEFAULT 350",
    "ALTER TABLE favorite_characters ADD COLUMN rd REAL NOT NULL DEFAULT 350",
    # Inicio del rewatch activo (NULL = ninguno). Lo visto antes de esta fecha vuelve
    # a contar como pendiente sin borrar el historial.
    "ALTER TABLE entries ADD COLUMN rewatch_started_at TEXT",
    # Backfill idempotente de episode_watches: una fila por episodio ya visto, para
    # que las estadísticas lean de aquí sin perder el histórico.
    """INSERT INTO episode_watches (episode_id, watched_at)
       SELECT id, watched_at FROM episodes
       WHERE watched_at IS NOT NULL
         AND NOT EXISTS (SELECT 1 FROM episode_watches WHERE episode_watches.episode_id = episodes.id)""",
    # Cada visionado pertenece a un usuario. La asignación de las filas existentes al
    # admin vive en _migrate_multiuser.
    "ALTER TABLE episode_watches ADD COLUMN user_id INTEGER REFERENCES users(id)",
    # score_distribution por usuario. taste_profile/profile_history cambian de PRIMARY
    # KEY y se reconstruyen en _migrate_affinity_multiuser.
    "ALTER TABLE score_distribution ADD COLUMN user_id INTEGER REFERENCES users(id)",
    # Caché de recomendados por usuario. Es recalculable, así que basta con añadir la
    # columna y vaciarla: se rellena sola en la siguiente visita o sync.
    "ALTER TABLE recommendations_cache ADD COLUMN user_id INTEGER REFERENCES users(id)",
    "DELETE FROM recommendations_cache WHERE user_id IS NULL",
    # Foto de perfil de cada cuenta.
    "ALTER TABLE users ADD COLUMN avatar_path TEXT",
    # Hasta cuándo ha visto cada usuario los comentarios ajenos. Las cuentas existentes
    # arrancan en AHORA para no ver todo el histórico como nuevo.
    "ALTER TABLE users ADD COLUMN comments_seen_at TEXT",
    "UPDATE users SET comments_seen_at = datetime('now') WHERE comments_seen_at IS NULL",
    # Desfase en días (con signo) para cuando TMDB fecha los episodios un día tarde o
    # pronto. Se aplica al guardar cada fecha (sync_episodes/refresh_metadata), nunca
    # al comparar, así que el resto del código no necesita saber que existe.
    "ALTER TABLE titles ADD COLUMN air_date_offset_days INTEGER NOT NULL DEFAULT 0",
    # Votos de la comunidad de cada recomendacion de AniList, en el mismo orden que
    # anilist_cross_rec_ids ("12,3,40") - para no pesar igual una recomendacion votada
    # por cientos de personas que una con 1 voto (repo.recommendations).
    "ALTER TABLE titles ADD COLUMN anilist_cross_rec_votes TEXT",
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
    """Pasa la escala de notas de 1-10 a 0-10. SQLite no permite modificar un CHECK con
    ALTER TABLE, así que se reconstruyen entries y season_ratings una sola vez (marca
    en app_settings), con foreign_keys desactivado y todo en la misma transacción."""
    done = conn.execute(
        "SELECT value FROM app_settings WHERE key = 'rating_scale_0_10'"
    ).fetchone()
    if done:
        return
    # PRAGMA foreign_keys no tiene efecto con una transacción abierta, y SCHEMA/
    # MIGRATIONS ya han escrito en esta conexión: sin este commit, el DROP TABLE falla
    # en cuanto haya filas referenciadas.
    conn.commit()
    conn.execute("PRAGMA foreign_keys = OFF")
    # Todas las columnas posteriores a cat_disfrute tienen que estar aquí, o una BBDD
    # nueva las pierde en la reconstrucción.
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
    """Introduce cuentas de usuario. SQLite no permite alterar un UNIQUE, así que
    entries, lists y rejected_recommendations se reconstruyen una sola vez (marca en
    app_settings) con UNIQUE compuestos por usuario. Los datos existentes pasan al
    admin recién creado. user_id queda nullable para no romper inserciones directas
    de tests antiguos; los puntos de escritura de la app siempre lo rellenan."""
    done = conn.execute("SELECT value FROM app_settings WHERE key = 'multiuser_migrated'").fetchone()
    if done:
        return
    # PRAGMA foreign_keys no tiene efecto con una transacción abierta: sin este commit
    # el DROP TABLE falla en cuanto haya filas referenciadas.
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

    # 6) episode_watches: todo lo ya registrado pasa a ser del admin.
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
    """Afinidad, duelos y waifus por usuario. taste_profile/profile_history cambian de
    PRIMARY KEY (SQLite obliga a reconstruir); lo ya calculado pasa al admin."""
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
