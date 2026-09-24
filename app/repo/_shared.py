"""app.repo._shared - utilidades compartidas por los módulos de app/repo/. El resto
del proyecto usa `from app import repo; repo.funcion(...)` (ver app/repo/__init__.py)."""
from datetime import datetime, timezone

from app import config


def get_setting(conn, key: str, default=None):
    row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default




def set_setting(conn, key: str, value: str):
    conn.execute(
        "INSERT INTO app_settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )




# Anime = idioma original japonés. El género "Animation" de TMDB también incluye
# animación occidental. Si original_language aún no está sincronizado, se usa el
# criterio antiguo (género Animation o alta manual sin género).
_IS_ANIME_SQL = (
    "(titles.original_language = 'ja' OR (titles.original_language IS NULL AND "
    "(titles.genres IS NULL OR titles.genres = '' "
    "OR titles.genres LIKE '%Animation%' OR titles.genres LIKE '%Animación%')))"
)




def _is_anime(title_row) -> bool:
    keys = title_row.keys()
    lang = title_row["original_language"] if "original_language" in keys else None
    if lang:
        return lang == "ja"
    genres = title_row["genres"] if "genres" in keys else None
    return genres is None or genres == "" or "Animation" in genres or "Animación" in genres


def user_has_anime(conn, user_id) -> bool:
    """¿Tiene este usuario anime en su biblioteca? Decide si la app habla de "Waifus"
    o de "Personajes favoritos"."""
    return bool(
        conn.execute(
            f"""SELECT 1 FROM entries JOIN titles ON titles.id = entries.title_id
               WHERE entries.user_id = ? AND {_IS_ANIME_SQL} LIMIT 1""",
            (user_id,),
        ).fetchone()
    )




# TMDB devuelve generos en ingles o español segun cuando se cacheo el titulo - unificar para stats.
_GENRE_ES = {
    "Animation": "Animación", "Comedy": "Comedia", "Drama": "Drama", "Action & Adventure": "Acción y Aventura",
    "Sci-Fi & Fantasy": "Ciencia ficción y Fantasía", "Mystery": "Misterio", "Romance": "Romance",
    "Crime": "Crimen", "Documentary": "Documental", "Family": "Familia", "Kids": "Infantil",
    "Fantasy": "Fantasía", "Action": "Acción", "Adventure": "Aventura", "Science Fiction": "Ciencia ficción",
    "Horror": "Terror", "Thriller": "Suspense", "War": "Bélica", "Western": "Western", "Music": "Música",
    "History": "Historia", "Talk": "Talk show", "Reality": "Reality", "News": "Noticias", "Soap": "Telenovela",
}




def _pending_clause(rewatch_started_at: str | None, user_id: int):
    """Fragmento SQL (+ params) para "este episodio le falta por ver a este usuario":
    ningún visionado, o ninguno posterior al inicio de su rewatch activo. Requiere
    `episodes.id` en scope. Lo usan mark_all_aired_watched, mark_season_watched y
    next_unwatched_episode."""
    if rewatch_started_at:
        return (
            "NOT EXISTS (SELECT 1 FROM episode_watches ew WHERE ew.episode_id = episodes.id "
            "AND ew.user_id = ? AND ew.watched_at >= ?)",
            (user_id, rewatch_started_at),
        )
    return (
        "NOT EXISTS (SELECT 1 FROM episode_watches ew WHERE ew.episode_id = episodes.id AND ew.user_id = ?)",
        (user_id,),
    )




def _log_episode_watch(conn, episode_id: int, user_id: int, when: str):
    """Marca un episodio visto ahora por este usuario. Cada marcado deja su propia fila
    en episode_watches, que no se borra nunca sola; así un rewatch no pierde fechas."""
    conn.execute(
        "INSERT INTO episode_watches (episode_id, user_id, watched_at) VALUES (?, ?, ?)",
        (episode_id, user_id, when),
    )




def _unlog_episode_watch(conn, episode_id: int, user_id: int):
    """Deshace el ULTIMO marcado de ESTE usuario para este episodio (no todo su
    historial, y no el de otros usuarios) - simetrico a _log_episode_watch."""
    last = conn.execute(
        "SELECT id FROM episode_watches WHERE episode_id = ? AND user_id = ? ORDER BY watched_at DESC, id DESC LIMIT 1",
        (episode_id, user_id),
    ).fetchone()
    if last:
        conn.execute("DELETE FROM episode_watches WHERE id = ?", (last["id"],))




def _promote_if_first_watch(conn, title_id, user_id):
    """Si la entry de este usuario sigue 'pending' pero ya tiene algún episodio visto,
    pasa a 'watched'. Sin esto, marcar episodios sin pulsar "Marcar vista" dejaba la
    serie en pendientes para siempre. Solo toca la entry de este usuario."""
    has_watched = conn.execute(
        """SELECT EXISTS(
             SELECT 1 FROM episode_watches ew JOIN episodes e ON e.id = ew.episode_id
             WHERE e.title_id = ? AND ew.user_id = ?)""",
        (title_id, user_id),
    ).fetchone()[0]
    if has_watched:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn.execute(
            """UPDATE entries SET status = 'watched', watched_at = COALESCE(watched_at, ?)
               WHERE title_id = ? AND user_id = ? AND status = 'pending'""",
            (now, title_id, user_id),
        )


POSTERS_DIR = config.POSTERS_DIR


PROFILES_DIR = config.PROFILES_DIR




# Pesos de la puntuación por categorías (opcional, ver mark_watched). Disfrute pesa
# más; Música, menos. Una categoría sin valorar no cuenta ni en la media ni en el peso.
CATEGORY_WEIGHTS = {
    "historia": 1.0,
    "animacion": 1.0,
    "personajes": 1.0,
    "musica": 0.5,
    "disfrute": 1.5,
}




def compute_weighted_rating(categories: dict) -> float | None:
    total, total_weight = 0.0, 0.0
    for cat, value in categories.items():
        if value is None:
            continue
        weight = CATEGORY_WEIGHTS[cat]
        total += value * weight
        total_weight += weight
    if total_weight == 0:
        return None
    return round(total / total_weight, 2)
