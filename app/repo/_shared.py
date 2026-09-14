"""app.repo._shared - extraido de repo.py en el split de modulos (ronda 2026-08-21).
Ver app/repo/__init__.py para el mapa completo de que vive en cada fichero -
el resto del proyecto sigue usando `from app import repo; repo.funcion(...)`
exactamente igual que antes, este split es puramente interno."""
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




# Señal principal: idioma original japones, dato real de TMDB - "Animation" en TMDB
# tambien mete dibujos occidentales (Futurama, Rick and Morty, Bluey...), asi que ese
# genero solo no basta (bug real, Tara 2026-08-13: le salio Futurama en el duelo de
# "solo anime"). Si original_language aun no se ha sincronizado (NULL), cae al criterio
# antiguo (genero Animation/Animacion, o alta manual sin genero) para no perder de golpe
# anime ya cacheado que todavia no ha pasado por un refresh_metadata.
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




# TMDB devuelve generos en ingles o español segun cuando se cacheo el titulo - unificar para stats.
_GENRE_ES = {
    "Animation": "Animación", "Comedy": "Comedia", "Drama": "Drama", "Action & Adventure": "Acción y Aventura",
    "Sci-Fi & Fantasy": "Ciencia ficción y Fantasía", "Mystery": "Misterio", "Romance": "Romance",
    "Crime": "Crimen", "Documentary": "Documental", "Family": "Familia", "Kids": "Infantil",
    "Fantasy": "Fantasía", "Action": "Acción", "Adventure": "Aventura", "Science Fiction": "Ciencia ficción",
    "Horror": "Terror", "Thriller": "Suspense", "War": "Bélica", "Western": "Western", "Music": "Música",
    "History": "Historia", "Talk": "Talk show", "Reality": "Reality", "News": "Noticias", "Soap": "Telenovela",
}




def _pending_clause(rewatch_started_at: str | None):
    """Fragmento SQL (+ params) para "este episodio falta por ver ahora" sobre la
    tabla episodes directamente (sin JOIN a entries) - version parametrizada gemela
    de la subquery correlacionada de _HOME_SHOWS_SQL, para las funciones que marcan
    episodios en bloque (mark_all_aired_watched, mark_season_watched,
    next_unwatched_episode)."""
    if rewatch_started_at:
        return "(watched_at IS NULL OR watched_at < ?)", (rewatch_started_at,)
    return "(watched_at IS NULL)", ()




def _log_episode_watch(conn, episode_id: int, when: str):
    """Marca un episodio visto AHORA sin pisar el historial (2026-08-20, "Volver a
    ver" estilo Trakt): cada marcado/re-marcado deja su propia fila en
    episode_watches, que nunca se borra sola - episodes.watched_at se actualiza como
    cache de "la vez mas reciente", asi que todo el codigo que ya lo lee como "la
    fecha" (estadisticas, historial, exportar...) sigue funcionando igual, solo que
    ahora esa fecha nunca se pierde de verdad al re-marcar."""
    conn.execute("INSERT INTO episode_watches (episode_id, watched_at) VALUES (?, ?)", (episode_id, when))
    conn.execute("UPDATE episodes SET watched_at = ? WHERE id = ?", (when, episode_id))




def _unlog_episode_watch(conn, episode_id: int):
    """Deshace el ULTIMO marcado de este episodio (no todo su historial) - simetrico a
    _log_episode_watch, para cuando se desmarca desde la UI. episodes.watched_at
    vuelve a la fecha anterior si la habia (p.ej. visto antes de un rewatch en curso),
    no a NULL sin mas - eso solo pasa si de verdad no queda ningun marcado."""
    last = conn.execute(
        "SELECT id FROM episode_watches WHERE episode_id = ? ORDER BY watched_at DESC, id DESC LIMIT 1",
        (episode_id,),
    ).fetchone()
    if last:
        conn.execute("DELETE FROM episode_watches WHERE id = ?", (last["id"],))
    conn.execute(
        """UPDATE episodes SET watched_at = (
             SELECT max(watched_at) FROM episode_watches WHERE episode_id = ?
           ) WHERE id = ?""",
        (episode_id, episode_id),
    )




def _promote_if_first_watch(conn, title_id):
    """Simetrico al 'si al desmarcar no queda ninguno, vuelve a pending' de abajo:
    si al marcar (episodio suelto, temporada completa o doble tick) la entry seguia
    'pending' pero ya hay algun episodio visto de verdad, pasa a 'watched'. Sin esto,
    marcar episodios sin pasar por el boton "Marcar vista" (checkboxes uno a uno,
    "Temp. completa", doble tick) dejaba la entry en pending para siempre aunque
    estuviera vista entera - bug real, Tara: "he visto toda la serie pero no me deja
    valorar, aunque sea en la ficha tecnica" (confirmado en produccion, entry 913,
    tmdb_id 325052: 12/12 episodios vistos, status seguia 'pending')."""
    has_watched = conn.execute(
        "SELECT EXISTS(SELECT 1 FROM episodes WHERE title_id = ? AND watched_at IS NOT NULL)",
        (title_id,),
    ).fetchone()[0]
    if has_watched:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn.execute(
            """UPDATE entries SET status = 'watched', watched_at = COALESCE(watched_at, ?)
               WHERE title_id = ? AND status = 'pending'""",
            (now, title_id),
        )


POSTERS_DIR = config.POSTERS_DIR


PROFILES_DIR = config.PROFILES_DIR




# Pesos de la puntuacion detallada por categorias (opcional, ver mark_watched):
# Disfrute pesa mas (es la nota "de tripas"), Musica pesa menos (Tara es sorda y normalmente
# se salta openings/endings, poca base para juzgar esta categoria en concreto). Sin "Ritmo" -
# se descarto explicitamente. Una categoria vacia ("no valorar") no cuenta ni en la media ni en el peso.
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
