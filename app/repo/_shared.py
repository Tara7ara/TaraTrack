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


def user_has_anime(conn, user_id) -> bool:
    """¿Tiene ESTE usuario algo de anime en su biblioteca? (Tara, 2026-09-18: "que
    a las personas normales le salga 'personajes fav', a la que haya un anime
    puesto en la lista se transforme a lista de waifus") - decide si /waifus,
    /listas y /estadisticas hablan de "Waifus" (termino de nicho, tiene sentido
    para Tara) o de "Personajes favoritos" (para quien no ve anime, como tu
    hermana/amigo puedan ser)."""
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
    """Fragmento SQL (+ params) para "este episodio falta por ver ahora PARA ESTE
    USUARIO" sobre la tabla episodes directamente (sin JOIN a entries) - version
    parametrizada gemela de la subquery correlacionada de _HOME_SHOWS_SQL, para las
    funciones que marcan episodios en bloque (mark_all_aired_watched,
    mark_season_watched, next_unwatched_episode). `episodes.id` debe estar en scope
    en la query que use este fragmento (referencia correlacionada a episode_watches).

    Multiusuario Fase 2 (2026-09-17): antes miraba episodes.watched_at directamente
    (compartido); ahora consulta episode_watches filtrado por user_id - "pendiente"
    significa que ESTE usuario no tiene ningun marcado (o ninguno posterior al inicio
    de su propio rewatch, si tiene uno en curso)."""
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
    """Marca un episodio visto AHORA por ESTE usuario, sin pisar el historial
    (2026-08-20, "Volver a ver" estilo Trakt) - cada marcado/re-marcado deja su
    propia fila en episode_watches, que nunca se borra sola. Multiusuario Fase 2
    (2026-09-17): episodes.watched_at DEJA de actualizarse como cache - con varias
    personas viendo lo mismo, "la ultima vez que se vio" ya no tiene un unico dueño;
    todo el codigo que necesita saber si ALGUIEN concreto lo ha visto consulta
    episode_watches filtrado por user_id directamente."""
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
    """Simetrico al 'si al desmarcar no queda ninguno, vuelve a pending' de abajo:
    si al marcar (episodio suelto, temporada completa o doble tick) la entry de ESTE
    usuario seguia 'pending' pero ya hay algun episodio visto de verdad POR EL, pasa
    a 'watched'. Sin esto, marcar episodios sin pasar por el boton "Marcar vista"
    dejaba la entry en pending para siempre aunque estuviera vista entera - bug real,
    Tara: "he visto toda la serie pero no me deja valorar" (entry 913, 2026-08-13).

    Multiusuario Fase 2 (2026-09-17): antes actualizaba CUALQUIER entry 'pending' de
    ese title_id (bug real encontrado en pruebas - con varios usuarios, el episodio
    marcado por uno promocionaria tambien la entry de otro que no ha visto nada).
    Ahora solo toca la entry de ESTE user_id."""
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
