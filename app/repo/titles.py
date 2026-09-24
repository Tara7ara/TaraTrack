"""app.repo.titles - títulos, entries, episodios, puntuación y listados de la
biblioteca. El resto del proyecto usa `from app import repo; repo.funcion(...)`."""
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

from app import anime, tmdb
from app.repo._shared import (
    _GENRE_ES,
    _IS_ANIME_SQL,
    POSTERS_DIR,
    _log_episode_watch,
    _pending_clause,
    _promote_if_first_watch,
    _unlog_episode_watch,
)
from app.repo.duels import reseed_undueled_entries_elo
from app.repo.lists import get_default_list


def get_title(conn, tmdb_id: int):
    return conn.execute("SELECT * FROM titles WHERE tmdb_id = ?", (tmdb_id,)).fetchone()



def set_poster(conn, tmdb_id: int, poster_path: str):
    """Portada subida a mano desde la ficha (POST /titulo/{id}/{type}/portada).
    custom_poster=1 la distingue de una de TMDB en el Mosaico y la ficha."""
    conn.execute(
        "UPDATE titles SET poster_path = ?, custom_poster = 1 WHERE tmdb_id = ?", (poster_path, tmdb_id)
    )




def ensure_title(conn, tmdb_id: int, media_type: str):
    """Devuelve la fila de titles, creandola (con detalles+poster cacheado) si no existe."""
    row = get_title(conn, tmdb_id)
    if row:
        return row

    details = tmdb.get_details(tmdb_id, media_type)
    poster_path = None
    if details.get("poster_url"):
        os.makedirs(POSTERS_DIR, exist_ok=True)
        filename = f"{tmdb_id}.jpg"
        if tmdb.download_poster(details["poster_url"], os.path.join(POSTERS_DIR, filename)):
            poster_path = f"/static/posters/{filename}"

    # INSERT OR IGNORE en vez de INSERT a secas: entre el SELECT de arriba y este INSERT
    # otra peticion (doble toque en el movil, dos pestañas) puede haber creado ya la
    # misma fila (tmdb_id UNIQUE) - sin el OR IGNORE eso era un IntegrityError sin capturar.
    conn.execute(
        """INSERT OR IGNORE INTO titles
           (tmdb_id, type, title, year, poster_path, overview, genres, imdb_id,
            show_status, next_episode_air_date, next_episode_label, vote_average,
            runtime_minutes, episode_count, is_adult, release_date, original_language,
            original_title)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            tmdb_id,
            media_type,
            details["title"],
            details["year"],
            poster_path,
            details["overview"],
            ",".join(details.get("genres", [])),
            details.get("imdb_id"),
            details.get("show_status"),
            details.get("next_episode_air_date"),
            details.get("next_episode_label"),
            details.get("vote_average"),
            details.get("runtime_minutes"),
            details.get("episode_count"),
            int(bool(details.get("adult"))),
            details.get("release_date"),
            details.get("original_language"),
            details.get("original_title"),
        ),
    )
    return get_title(conn, tmdb_id)




def _shift_date(value: str | None, offset_days: int) -> str | None:
    """Suma offset_days (con signo) a una fecha 'YYYY-MM-DD' (o con hora detras, se
    conserva tal cual esa parte). None/vacia se devuelve igual - un titulo sin fecha
    cacheada no tiene nada que desplazar."""
    if not value or not offset_days:
        return value
    day_part, _, rest = value.partition("T")
    shifted = date.fromisoformat(day_part[:10]) + timedelta(days=offset_days)
    return f"{shifted.isoformat()}T{rest}" if rest else shifted.isoformat()


def set_air_date_offset(conn, title_id: int, days: int):
    """Desfase en días (con signo) entre la fecha de emisión de TMDB y la real; 0 lo
    quita. Se aplica al guardar (sync_episodes/refresh_metadata), nunca al comparar."""
    conn.execute("UPDATE titles SET air_date_offset_days = ? WHERE id = ?", (days, title_id))


def refresh_metadata(conn, title_row):
    """Vuelve a consultar TMDB para refrescar nota/duracion (y proximo episodio si es serie).
    Tambien pisa titulo/sinopsis/generos: los titulos cacheados antes del cambio a
    es-ES se quedaban en ingles para siempre, sin ninguna sync los volviera a pedir."""
    if title_row["tmdb_id"] < 0:
        return
    details = tmdb.get_details(title_row["tmdb_id"], title_row["type"])
    offset = title_row["air_date_offset_days"] or 0
    conn.execute(
        """UPDATE titles SET title = ?, overview = ?, genres = ?, show_status = ?,
           next_episode_air_date = ?, next_episode_label = ?,
           vote_average = ?, runtime_minutes = ?, episode_count = ?, release_date = ?,
           original_language = ?, original_title = ? WHERE id = ?""",
        (
            details["title"],
            details["overview"],
            ",".join(details.get("genres", [])),
            details.get("show_status"),
            _shift_date(details.get("next_episode_air_date"), offset),
            details.get("next_episode_label"),
            details.get("vote_average"),
            details.get("runtime_minutes"),
            details.get("episode_count"),
            details.get("release_date"),
            details.get("original_language"),
            details.get("original_title"),
            title_row["id"],
        ),
    )




def sync_episodes(conn, title_row):
    """Descarga y cachea todas las temporadas/episodios de una serie desde TMDB (no pisa watched_at)."""
    if title_row["type"] != "show" or title_row["tmdb_id"] < 0:
        return
    details = tmdb.get_details(title_row["tmdb_id"], "show")
    seasons = details.get("seasons", [])
    # Una temporada no depende de otra - pedirlas todas a la vez evita que una serie
    # con muchas temporadas tarde segundos la primera vez que se abre su ficha.
    with ThreadPoolExecutor(max_workers=8) as pool:
        episodes_by_season = pool.map(
            lambda s: tmdb.get_season_episodes(title_row["tmdb_id"], s), seasons
        )
    offset = title_row["air_date_offset_days"] or 0
    for episodes in episodes_by_season:
        for ep in episodes:
            conn.execute(
                """INSERT INTO episodes (title_id, season_number, episode_number, name, air_date)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(title_id, season_number, episode_number)
                   DO UPDATE SET name = excluded.name, air_date = excluded.air_date""",
                (title_row["id"], ep["season_number"], ep["episode_number"], ep["name"],
                 _shift_date(ep["air_date"], offset)),
            )




def list_episodes(conn, title_id):
    # watch_count: cuántas veces se ha marcado este episodio (episode_watches), para el
    # "x2/x3" del rewatch. Un episodio ya visto nunca aparece vacío al re-verlo.
    return conn.execute(
        """SELECT episodes.*,
                  (SELECT count(*) FROM episode_watches
                     WHERE episode_watches.episode_id = episodes.id) AS watch_count
           FROM episodes WHERE title_id = ? ORDER BY season_number, episode_number""",
        (title_id,),
    ).fetchall()




def toggle_episode(conn, episode_id, user_id):
    """Marca o desmarca un episodio para este usuario. Si al desmarcar no le queda
    ningún episodio visto, su entry vuelve a 'pending'. Con un rewatch en curso, lo
    marcado antes de esa ronda cuenta como pendiente."""
    title_id = conn.execute("SELECT title_id FROM episodes WHERE id = ?", (episode_id,)).fetchone()["title_id"]
    rewatch_started_at = conn.execute(
        "SELECT rewatch_started_at FROM entries WHERE title_id = ? AND user_id = ?", (title_id, user_id)
    ).fetchone()
    rewatch_started_at = rewatch_started_at["rewatch_started_at"] if rewatch_started_at else None
    last_watch = conn.execute(
        "SELECT watched_at FROM episode_watches WHERE episode_id = ? AND user_id = ? "
        "ORDER BY watched_at DESC LIMIT 1",
        (episode_id, user_id),
    ).fetchone()
    checked_now = bool(last_watch) and (
        not rewatch_started_at or last_watch["watched_at"] >= rewatch_started_at
    )
    if checked_now:
        _unlog_episode_watch(conn, episode_id, user_id)
        quedan = conn.execute(
            """SELECT count(*) AS c FROM episode_watches ew JOIN episodes e ON e.id = ew.episode_id
               WHERE e.title_id = ? AND ew.user_id = ?""",
            (title_id, user_id),
        ).fetchone()["c"]
        if quedan == 0:
            conn.execute(
                "UPDATE entries SET status = 'pending' WHERE title_id = ? AND user_id = ? AND status = 'watched'",
                (title_id, user_id),
            )
        return False
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    _log_episode_watch(conn, episode_id, user_id, now)
    _promote_if_first_watch(conn, title_id, user_id)
    # True = se acaba de marcar como visto (no desmarcar): dispara el aviso de
    # "¿comentas?".
    return True




def rewatch_episode(conn, episode_id, user_id):
    """Vuelve a ver un episodio suelto: añade siempre una fila a episode_watches, sin
    desmarcar ni tocar entries.status ni rewatch_started_at. Para episodios ya vistos;
    la primera vez se marca con toggle_episode."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    _log_episode_watch(conn, episode_id, user_id, now)


def list_episode_watch_dates(conn, episode_id, user_id) -> list[str]:
    """Todas las fechas en que ESTE usuario ha marcado visto este episodio, mas
    recientes primero - para el historial plegable junto a "Volver a ver este episodio"."""
    rows = conn.execute(
        "SELECT watched_at FROM episode_watches WHERE episode_id = ? AND user_id = ? ORDER BY watched_at DESC",
        (episode_id, user_id),
    ).fetchall()
    return [r["watched_at"] for r in rows]


def mark_all_aired_watched(conn, entry_id):
    """Doble tick de la tarjeta de inicio: marca vistos todos los episodios emitidos
    para el dueño de la entry. Con un rewatch en curso, incluye lo visto en rondas
    anteriores (ver _pending_clause). Recibe `entry_id`: de ahí salen title_id,
    user_id y rewatch_started_at."""
    entry = conn.execute(
        "SELECT title_id, user_id, rewatch_started_at FROM entries WHERE id = ?", (entry_id,)
    ).fetchone()
    title_id, user_id, rewatch_started_at = entry["title_id"], entry["user_id"], entry["rewatch_started_at"]
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    clause, params = _pending_clause(rewatch_started_at, user_id)
    rows = conn.execute(
        f"""SELECT id FROM episodes WHERE title_id = ? AND {clause}
           AND air_date IS NOT NULL AND date(air_date) <= date('now')""",
        (title_id, *params),
    ).fetchall()
    for r in rows:
        _log_episode_watch(conn, r["id"], user_id, now)
    _promote_if_first_watch(conn, title_id, user_id)




def get_season_rating(conn, entry_id: int, season_number: int):
    return conn.execute(
        "SELECT * FROM season_ratings WHERE entry_id = ? AND season_number = ?",
        (entry_id, season_number),
    ).fetchone()




def list_season_ratings(conn, entry_id: int) -> dict:
    """Notas por temporada ya puestas, indexadas por numero de temporada - para pintar
    el badge junto a la barra de progreso de cada temporada sin una query por temporada."""
    rows = conn.execute("SELECT * FROM season_ratings WHERE entry_id = ?", (entry_id,)).fetchall()
    return {row["season_number"]: row for row in rows}




def set_season_rating(conn, entry_id: int, season_number: int, rating: float, comment: str,
                       categories: dict | None = None):
    """Puntúa una temporada suelta, para series cuyas temporadas no valen lo mismo."""
    categories = categories or {}
    conn.execute(
        """INSERT INTO season_ratings
           (entry_id, season_number, rating, comment, cat_historia, cat_animacion,
            cat_personajes, cat_musica, cat_disfrute, rated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
           ON CONFLICT(entry_id, season_number) DO UPDATE SET
             rating = excluded.rating, comment = excluded.comment,
             cat_historia = excluded.cat_historia, cat_animacion = excluded.cat_animacion,
             cat_personajes = excluded.cat_personajes, cat_musica = excluded.cat_musica,
             cat_disfrute = excluded.cat_disfrute, rated_at = excluded.rated_at""",
        (
            entry_id, season_number, rating, comment,
            categories.get("historia"), categories.get("animacion"), categories.get("personajes"),
            categories.get("musica"), categories.get("disfrute"),
        ),
    )
    # La nota general de una serie puntuada por temporadas es la media simple de sus
    # season_ratings, recalculada en cada guardado. Sin esto se quedaba sin nota y
    # volvía a /puntuar sin fin.
    avg = conn.execute(
        "SELECT AVG(rating) AS avg FROM season_ratings WHERE entry_id = ?", (entry_id,)
    ).fetchone()["avg"]
    conn.execute("UPDATE entries SET rating = ? WHERE id = ?", (round(avg, 2), entry_id))




def mark_season_watched(conn, entry_id, season_number: int):
    """Marca vistos los episodios ya emitidos de una temporada concreta, para el dueño
    de la entry. Con rewatch en curso, igual que mark_all_aired_watched."""
    entry = conn.execute(
        "SELECT title_id, user_id, rewatch_started_at FROM entries WHERE id = ?", (entry_id,)
    ).fetchone()
    title_id, user_id, rewatch_started_at = entry["title_id"], entry["user_id"], entry["rewatch_started_at"]
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    clause, params = _pending_clause(rewatch_started_at, user_id)
    rows = conn.execute(
        f"""SELECT id FROM episodes WHERE title_id = ? AND season_number = ? AND {clause}
           AND air_date IS NOT NULL AND date(air_date) <= date('now')""",
        (title_id, season_number, *params),
    ).fetchall()
    for r in rows:
        _log_episode_watch(conn, r["id"], user_id, now)
    _promote_if_first_watch(conn, title_id, user_id)




def toggle_episode_favorite(conn, episode_id, user_id) -> bool:
    """Favorito por episodio, propio de cada usuario (episode_user_state)."""
    row = conn.execute(
        "SELECT is_favorite FROM episode_user_state WHERE episode_id = ? AND user_id = ?",
        (episode_id, user_id),
    ).fetchone()
    new_value = 0 if (row and row["is_favorite"]) else 1
    conn.execute(
        """INSERT INTO episode_user_state (episode_id, user_id, is_favorite) VALUES (?, ?, ?)
           ON CONFLICT(episode_id, user_id) DO UPDATE SET is_favorite = excluded.is_favorite""",
        (episode_id, user_id, new_value),
    )
    return bool(new_value)




def get_episode_user_state(conn, episode_id, user_id):
    return conn.execute(
        "SELECT * FROM episode_user_state WHERE episode_id = ? AND user_id = ?", (episode_id, user_id)
    ).fetchone()




def set_episode_comment(conn, episode_id, user_id, comment: str):
    """Comentario privado por episodio (episode_user_state). El debate compartido vive
    en repo/comments.py."""
    comment = comment.strip() or None
    conn.execute(
        """INSERT INTO episode_user_state (episode_id, user_id, comment) VALUES (?, ?, ?)
           ON CONFLICT(episode_id, user_id) DO UPDATE SET comment = excluded.comment""",
        (episode_id, user_id, comment),
    )




def list_history(conn, user_id, limit=300):
    """Historial de este usuario, lo más reciente primero: episodios vistos, títulos
    marcados vistos y rewatches. 'detail' es la etiqueta corta (T1E6, Vista entera,
    Rewatch).

    Cada rama se ordena y corta a `limit` antes del UNION ALL: el resultado nunca
    necesita más de `limit` filas de una sola fuente, y así no se ordena todo el
    historial. SQLite exige envolver cada rama en su subconsulta para eso."""
    return conn.execute(
        """SELECT * FROM (
             SELECT * FROM (
               SELECT episode_watches.watched_at AS at, titles.title, titles.tmdb_id, titles.type,
                      titles.poster_path,
                      'T' || episodes.season_number || 'E' || episodes.episode_number AS detail
               FROM episode_watches
               JOIN episodes ON episodes.id = episode_watches.episode_id
               JOIN titles ON titles.id = episodes.title_id
               WHERE episode_watches.user_id = ?
               ORDER BY episode_watches.watched_at DESC LIMIT ?
             )
             UNION ALL
             SELECT * FROM (
               SELECT entries.watched_at AS at, titles.title, titles.tmdb_id, titles.type,
                      titles.poster_path, 'Vista entera' AS detail
               FROM entries JOIN titles ON titles.id = entries.title_id
               WHERE entries.watched_at IS NOT NULL AND entries.user_id = ?
               ORDER BY entries.watched_at DESC LIMIT ?
             )
             UNION ALL
             SELECT * FROM (
               SELECT watch_sessions.watched_at AS at, titles.title, titles.tmdb_id, titles.type,
                      titles.poster_path, 'Volver a ver' AS detail
               FROM watch_sessions
               JOIN entries ON entries.id = watch_sessions.entry_id
               JOIN titles ON titles.id = entries.title_id
               WHERE entries.user_id = ?
               ORDER BY watch_sessions.watched_at DESC LIMIT ?
             )
           )
           ORDER BY at DESC LIMIT ?""",
        (user_id, limit, user_id, limit, user_id, limit, limit),
    ).fetchall()




def get_similar(conn, title_row, limit=10):
    """Titulos afines a este segun TMDB, incluyendo los ya vistos/pendientes (con enlace
    interno si ya estan en la BBDD). Para la seccion 'Similares' del detalle."""
    if title_row["tmdb_id"] < 0:
        return []
    recs = tmdb.get_recommendations(title_row["tmdb_id"], title_row["type"])[:limit]
    # Antes traia TODA la tabla titles (~900 filas) para cruzar solo estos `limit` ids -
    # mismo patron que get_entry_states, acotar con WHERE IN en vez de cargar todo a memoria.
    ids = [r["tmdb_id"] for r in recs]
    known = {}
    if ids:
        placeholders = ",".join("?" * len(ids))
        known = {
            row["tmdb_id"]: row
            for row in conn.execute(
                f"SELECT tmdb_id, poster_path FROM titles WHERE tmdb_id IN ({placeholders})", ids
            )
        }
    for rec in recs:
        local = known.get(rec["tmdb_id"])
        rec["known"] = local is not None
        if local and local["poster_path"]:
            rec["poster_url"] = local["poster_path"]
    return recs




def list_calendar(conn, user_id, days_before=7, days_after=30):
    """Episodios que caen en la ventana, solo de series que este usuario ha empezado
    (al menos un episodio visto). `watched_now` indica si ya los ha visto."""
    return conn.execute(
        """SELECT titles.tmdb_id, titles.title, titles.poster_path,
                  episodes.season_number, episodes.episode_number, episodes.name AS ep_name,
                  episodes.air_date, entries.auto_watch,
                  EXISTS(SELECT 1 FROM episode_watches ew WHERE ew.episode_id = episodes.id
                         AND ew.user_id = ?) AS watched_now
           FROM episodes
           JOIN titles ON titles.id = episodes.title_id
           JOIN entries ON entries.title_id = titles.id AND entries.user_id = ?
           WHERE episodes.air_date IS NOT NULL
             AND date(episodes.air_date) BETWEEN date('now', ?) AND date('now', ?)
             AND EXISTS(SELECT 1 FROM episode_watches ew2 JOIN episodes started ON started.id = ew2.episode_id
                        WHERE started.title_id = titles.id AND ew2.user_id = ?)
           ORDER BY episodes.air_date, titles.title, episodes.episode_number""",
        (user_id, user_id, f"-{days_before} days", f"+{days_after} days", user_id),
    ).fetchall()




# Series pendientes con sus contadores de episodios, para la pantalla de inicio.
# "in_season" = algún episodio emitido o programado a ±21 días de hoy; 'Returning
# Series' de TMDB no sirve de filtro (sigue activo entre temporadas). Los contadores
# consultan episode_watches con el user_id de la propia fila de entries; el primer "?"
# es el user_id de la sesión, para el WHERE final.
_HOME_SHOWS_SQL = """
    SELECT entries.*, titles.title, titles.year, titles.poster_path, titles.type,
           titles.tmdb_id, titles.runtime_minutes, titles.release_date,
           (SELECT count(*) FROM episodes
              WHERE episodes.title_id = titles.id
                AND EXISTS(SELECT 1 FROM episode_watches ew WHERE ew.episode_id = episodes.id
                           AND ew.user_id = entries.user_id)) AS watched_eps,
           -- "Falta por ver AHORA": nunca visto POR ESTE USUARIO, o la unica vez que lo
           -- vio fue ANTES de que empezara SU ronda de rewatch activa.
           (SELECT count(*) FROM episodes
              WHERE episodes.title_id = titles.id
                AND NOT EXISTS(
                    SELECT 1 FROM episode_watches ew WHERE ew.episode_id = episodes.id
                      AND ew.user_id = entries.user_id
                      AND (entries.rewatch_started_at IS NULL OR ew.watched_at >= entries.rewatch_started_at))
                AND episodes.air_date IS NOT NULL
                AND date(episodes.air_date) <= date('now')) AS missing_eps,
           (SELECT max(ew.watched_at) FROM episode_watches ew JOIN episodes e2 ON e2.id = ew.episode_id
              WHERE e2.title_id = titles.id AND ew.user_id = entries.user_id) AS last_watched_at,
           (SELECT max(watch_sessions.watched_at) FROM watch_sessions
              WHERE watch_sessions.entry_id = entries.id) AS last_rewatch_at,
           EXISTS(SELECT 1 FROM episodes WHERE episodes.title_id = titles.id
              AND episodes.air_date IS NOT NULL
              AND date(episodes.air_date) BETWEEN date('now', '-21 days')
                                              AND date('now', '+21 days')) AS in_season
    FROM entries JOIN titles ON titles.id = entries.title_id
    WHERE titles.type = 'show' AND entries.user_id = ?"""




def next_unwatched_episode(conn, title_id: int, rewatch_started_at: str | None, user_id: int):
    """El siguiente episodio ya emitido que le falta por ver a este usuario (el 'T1E6'
    de la tarjeta). Con rewatch en curso, lo visto antes de la ronda vuelve a contar."""
    clause, params = _pending_clause(rewatch_started_at, user_id)
    return conn.execute(
        f"""SELECT * FROM episodes
           WHERE title_id = ? AND {clause} AND air_date IS NOT NULL
             AND date(air_date) <= date('now') AND season_number > 0
           ORDER BY season_number, episode_number LIMIT 1""",
        (title_id, *params),
    ).fetchone()




def _with_next_episode(conn, row):
    data = dict(row)
    data["next_ep"] = next_unwatched_episode(conn, row["title_id"], row["rewatch_started_at"], row["user_id"])
    return data




def list_continue_watching(conn, user_id):
    """Estilo Trakt: cualquier serie con al menos un episodio visto (por ESTE usuario)
    y emitidos por ver, lo mas reciente primero - da igual el status de la entry (The
    Boys a medias cuenta aunque este marcada vista y no emita ahora). Las no empezadas
    NO salen aqui. "missing_eps" es round-aware (ver _HOME_SHOWS_SQL): una serie recien
    puesta en "Volver a ver" vuelve a tener episodios "pendientes" desde el principio
    sin que se le haya borrado ningun marcado viejo."""
    rows = conn.execute(_HOME_SHOWS_SQL, (user_id,)).fetchall()
    continuar = sorted(
        (r for r in rows if (r["watched_eps"] or r["last_rewatch_at"]) and r["missing_eps"]),
        key=lambda r: max(r["last_watched_at"] or "", r["last_rewatch_at"] or ""),
        reverse=True,
    )
    return [_with_next_episode(conn, r) for r in continuar]




def _current_anime_season_start() -> str:
    """Primer dia de la temporada de anime actual (Invierno=dic-feb, Primavera=mar-may,
    Verano=jun-ago, Otoño=sep-nov) - mismo criterio de meses que main._current_season(),
    usado para acotar list_new_airing a "de la temporada", no cualquier pendiente
    atrasado con algun episodio suelto por ver."""
    today = datetime.now(timezone.utc).date()
    if today.month == 12:
        return date(today.year, 12, 1).isoformat()
    if today.month in (1, 2):
        return date(today.year - 1, 12, 1).isoformat()
    if today.month in (3, 4, 5):
        return date(today.year, 3, 1).isoformat()
    if today.month in (6, 7, 8):
        return date(today.year, 6, 1).isoformat()
    return date(today.year, 9, 1).isoformat()




def list_new_airing(conn, user_id):
    """Series pendientes sin ningún episodio visto, con algún episodio ya emitido y
    estrenadas en la temporada actual: para que no se pierdan entre los pendientes.
    Complementa a list_continue_watching (esa exige al menos uno visto)."""
    rows = conn.execute(_HOME_SHOWS_SQL, (user_id,)).fetchall()
    season_start = _current_anime_season_start()
    nuevas = sorted(
        (
            r for r in rows
            if not r["watched_eps"] and not r["last_rewatch_at"] and r["missing_eps"]
            and r["release_date"] and r["release_date"] >= season_start
        ),
        key=lambda r: r["added_at"] or "",
        reverse=True,
    )
    return [_with_next_episode(conn, r) for r in nuevas]




def get_home_card(conn, entry_id: int, user_id: int):
    """Re-render de una tarjeta tras marcar episodios. Aplica el mismo filtro que
    list_continue_watching: si ya no quedan emitidos por ver, devuelve None y la
    tarjeta desaparece del carrusel. También hace de guarda de propiedad: si la entry
    no es de este usuario, devuelve None."""
    row = conn.execute(_HOME_SHOWS_SQL + " AND entries.id = ?", (user_id, entry_id)).fetchone()
    if not row or not ((row["watched_eps"] or row["last_rewatch_at"]) and row["missing_eps"]):
        return None
    return _with_next_episode(conn, row)




def list_trackable_titles(conn):
    """Todo lo seguido (pendiente o visto) con tmdb_id real: candidatos a refrescar metadatos."""
    return conn.execute(
        """SELECT DISTINCT titles.* FROM titles JOIN entries ON entries.title_id = titles.id
           WHERE titles.tmdb_id > 0"""
    ).fetchall()




def ensure_manual_title(conn, media_type: str, title: str, year: int | None, imdb_id: str | None = None):
    """Alta manual para titulos sin match en TMDB: id sintetico negativo, sin poster de TMDB.
    Dedupe por (title, type, year) para no duplicar si se llama dos veces (p.ej. re-ejecutar el seed)."""
    existing = conn.execute(
        "SELECT * FROM titles WHERE title = ? AND type = ? AND year IS ?", (title, media_type, year)
    ).fetchone()
    if existing:
        return existing

    next_id_row = conn.execute("SELECT MIN(tmdb_id) - 1 AS next_id FROM titles WHERE tmdb_id < 0").fetchone()
    synthetic_id = next_id_row["next_id"] if next_id_row["next_id"] is not None else -1

    conn.execute(
        "INSERT INTO titles (tmdb_id, type, title, year, imdb_id) VALUES (?, ?, ?, ?, ?)",
        (synthetic_id, media_type, title, year, imdb_id),
    )
    return get_title(conn, synthetic_id)




def create_manual_entry(conn, media_type: str, title: str, year: int | None, poster_url: str | None, user_id: int):
    """Alta manual desde /buscar cuando TMDB no encuentra el titulo.
    Sin poster_url pegado a mano, intenta completar portada/año/episodios desde
    MyAnimeList/AniList (ver anime.py)."""
    extra = {}
    if not poster_url:
        match = anime.search_anime(title)
        if match:
            poster_url = match["poster_url"]
            extra = {
                "year": year or match["year"],
                "episode_count": match["episode_count"],
                "runtime_minutes": match["runtime_minutes"],
                "vote_average": match["score"],
            }

    title_row = ensure_manual_title(conn, media_type, title, extra.get("year", year))

    if poster_url and not title_row["poster_path"]:
        os.makedirs(POSTERS_DIR, exist_ok=True)
        filename = f"manual_{-title_row['tmdb_id']}.jpg"
        try:
            if tmdb.download_poster(poster_url, os.path.join(POSTERS_DIR, filename)):
                extra["poster_path"] = f"/static/posters/{filename}"
        except Exception:
            pass

    if extra:
        sets = ", ".join(f"{col} = ?" for col in extra)
        conn.execute(f"UPDATE titles SET {sets} WHERE id = ?", (*extra.values(), title_row["id"]))

    ensure_entry(conn, title_row["tmdb_id"], media_type, user_id)
    return get_title(conn, title_row["tmdb_id"])




def get_entry_states(conn, tmdb_ids: list[int], user_id: int) -> dict[int, str]:
    """Estado (pending/watched) de este usuario para una tanda de tmdb_ids, para que
    búsqueda y similares no ofrezcan '+ Pendientes' en algo que ya tienes. Lo que no
    aparece en el dict es 'new'."""
    if not tmdb_ids:
        return {}
    placeholders = ",".join("?" * len(tmdb_ids))
    rows = conn.execute(
        f"""SELECT titles.tmdb_id, entries.status FROM titles
           JOIN entries ON entries.title_id = titles.id
           WHERE titles.tmdb_id IN ({placeholders}) AND entries.user_id = ?""",
        (*tmdb_ids, user_id),
    ).fetchall()
    return {row["tmdb_id"]: row["status"] for row in rows}




def ensure_entry(conn, tmdb_id: int, media_type: str, user_id: int):
    """La entry de este usuario para el título, creándola como 'pending' si no existe."""
    title = ensure_title(conn, tmdb_id, media_type)
    entry = conn.execute(
        "SELECT * FROM entries WHERE title_id = ? AND user_id = ?", (title["id"], user_id)
    ).fetchone()
    if entry:
        return entry
    conn.execute(
        "INSERT INTO entries (title_id, user_id, status) VALUES (?, ?, 'pending')", (title["id"], user_id)
    )
    return conn.execute(
        "SELECT * FROM entries WHERE title_id = ? AND user_id = ?", (title["id"], user_id)
    ).fetchone()




def get_entry_with_title(conn, entry_id: int):
    return conn.execute(
        """SELECT entries.*, titles.title, titles.year, titles.poster_path, titles.type,
                  titles.tmdb_id, titles.release_date
           FROM entries JOIN titles ON titles.id = entries.title_id
           WHERE entries.id = ?""",
        (entry_id,),
    ).fetchone()


def get_owned_entry_with_title(conn, entry_id: int, user_id: int):
    """Como get_entry_with_title, pero None si la entry no es de este usuario: guarda
    de propiedad para las rutas que reciben un entry_id en la URL."""
    entry = get_entry_with_title(conn, entry_id)
    if entry and entry["user_id"] == user_id:
        return entry
    return None




def _snapshot_rating_history(conn, entry_id: int):
    """Guarda la nota, las categorías y el comentario actuales en rating_history antes
    de pisarlos, para ver cómo cambia la opinión con el tiempo. Solo si ya había nota."""
    row = conn.execute(
        """SELECT rating, cat_historia, cat_animacion, cat_personajes, cat_musica, cat_disfrute, comment
           FROM entries WHERE id = ?""",
        (entry_id,),
    ).fetchone()
    if row and row["rating"] is not None:
        conn.execute(
            """INSERT INTO rating_history
               (entry_id, rating, cat_historia, cat_animacion, cat_personajes, cat_musica, cat_disfrute, comment)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                entry_id, row["rating"], row["cat_historia"], row["cat_animacion"],
                row["cat_personajes"], row["cat_musica"], row["cat_disfrute"], row["comment"],
            ),
        )




def list_rating_history(conn, entry_id: int):
    return conn.execute(
        "SELECT * FROM rating_history WHERE entry_id = ? ORDER BY replaced_at DESC", (entry_id,)
    ).fetchall()




def mark_watched(conn, entry_id: int, rating: float, comment: str, categories: dict | None = None,
                 watched_at: str | None = None, is_habit: bool | None = None):
    """watched_at opcional (YYYY-MM-DD) para registrar visionados antiguos con su fecha real.
    Sin fecha explicita, se conserva la que hubiera (puntuar un import de Trakt no debe
    pisar la fecha real con la de hoy) y solo si no habia ninguna se pone ahora.
    is_habit opcional (Bloque 3b, check junto al examen): None deja is_habit tal cual
    estuviera (atajos como "no me acuerdo, 5 y ya" no tocan la pregunta de habito);
    True/False lo fija de forma explicita, reflejando el checkbox del formulario."""
    categories = categories or {}
    _snapshot_rating_history(conn, entry_id)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    habit_sql = ", is_habit = ?" if is_habit is not None else ""
    params = [
        rating,
        comment,
        watched_at,
        now,
        categories.get("historia"),
        categories.get("animacion"),
        categories.get("personajes"),
        categories.get("musica"),
        categories.get("disfrute"),
    ]
    if is_habit is not None:
        params.append(1 if is_habit else 0)
    params.append(entry_id)
    conn.execute(
        f"""UPDATE entries SET status = 'watched', rating = ?, comment = ?,
           watched_at = COALESCE(?, watched_at, ?),
           cat_historia = ?, cat_animacion = ?, cat_personajes = ?, cat_musica = ?, cat_disfrute = ?{habit_sql}
           WHERE id = ?""",
        params,
    )
    user_id = conn.execute("SELECT user_id FROM entries WHERE id = ?", (entry_id,)).fetchone()["user_id"]
    reseed_undueled_entries_elo(conn, user_id)
    # Serie en emisión con una sola temporada cacheada: la nota también se guarda como
    # season_ratings[1]. Así, cuando salga y se complete la temporada 2, vuelve a
    # /puntuar. set_season_rating recalcula entries.rating como la media, que con una
    # sola fila es la propia nota.
    title = conn.execute(
        "SELECT titles.id AS title_id, titles.type, titles.show_status FROM entries "
        "JOIN titles ON titles.id = entries.title_id WHERE entries.id = ?", (entry_id,)
    ).fetchone()
    if title and title["type"] == "show" and title["show_status"] == "Returning Series":
        season_count = conn.execute(
            "SELECT COUNT(DISTINCT season_number) AS n FROM episodes WHERE title_id = ? AND season_number > 0",
            (title["title_id"],),
        ).fetchone()["n"]
        if season_count == 1:
            set_season_rating(conn, entry_id, 1, rating, comment, categories)




def set_watched_at(conn, entry_id: int, date_str: str):
    """Corrige la fecha de visionado de una entry ya vista, sin pasar por el formulario
    de nota (p. ej. series importadas con la fecha de hoy). Solo el día; la hora se fija
    a mediodía UTC para que el huso horario no cambie la fecha.

    En series también corrige el último marcado de cada episodio visto, que es lo que
    alimenta historial y estadísticas. Cada episodio toma su propio air_date; la fecha
    del formulario solo se usa para episodios sin fecha. Es un UPDATE en el sitio, no un
    visionado nuevo: no suma al contador "x2"."""
    entry = conn.execute("SELECT title_id, user_id FROM entries WHERE id = ?", (entry_id,)).fetchone()
    new_value = f"{date_str}T12:00:00Z"
    conn.execute(
        "UPDATE entries SET watched_at = ? WHERE id = ? AND status = 'watched'",
        (new_value, entry_id),
    )
    if entry is None:
        return
    episodes = conn.execute(
        """SELECT id, air_date FROM episodes WHERE title_id = ?
           AND EXISTS(SELECT 1 FROM episode_watches WHERE episode_watches.episode_id = episodes.id
                      AND episode_watches.user_id = ?)""",
        (entry["title_id"], entry["user_id"]),
    ).fetchall()
    for ep in episodes:
        ep_value = f"{ep['air_date']}T12:00:00Z" if ep["air_date"] else new_value
        last_watch = conn.execute(
            "SELECT id FROM episode_watches WHERE episode_id = ? AND user_id = ? ORDER BY watched_at DESC, id DESC LIMIT 1",
            (ep["id"], entry["user_id"]),
        ).fetchone()
        if last_watch:
            conn.execute("UPDATE episode_watches SET watched_at = ? WHERE id = ?", (ep_value, last_watch["id"]))




def undo_mark_watched(conn, entry_id: int):
    """Deshace un "Marcar vista" por error: la entry vuelve a pending y se desmarcan
    solo los episodios marcados en ese mismo instante (mismo watched_at que puso
    mark_watched_quick), así no se pierde historial previo. Solo para lo recién
    marcado sin puntuar; si ya tiene nota, lo que aplica es "Quitar nota"."""
    row = conn.execute("SELECT title_id, user_id, watched_at FROM entries WHERE id = ?", (entry_id,)).fetchone()
    conn.execute("UPDATE entries SET status = 'pending', watched_at = NULL WHERE id = ?", (entry_id,))
    if row["watched_at"]:
        eps = conn.execute(
            """SELECT episodes.id FROM episodes
               JOIN episode_watches ON episode_watches.episode_id = episodes.id
               WHERE episodes.title_id = ? AND episode_watches.user_id = ? AND episode_watches.watched_at = ?""",
            (row["title_id"], row["user_id"], row["watched_at"]),
        ).fetchall()
        for ep in eps:
            _unlog_episode_watch(conn, ep["id"], row["user_id"])




def clear_rating(conn, entry_id: int):
    """Quita la nota (y las categorias del examen) sin desmarcar como vista - pensado
    para notas puestas antes de tiempo (p.ej. un 10 a mitad de una serie que aun no
    ha acabado). El comentario se conserva. Vuelve a caer en la cola de /puntuar."""
    _snapshot_rating_history(conn, entry_id)
    conn.execute(
        """UPDATE entries SET rating = NULL,
           cat_historia = NULL, cat_animacion = NULL, cat_personajes = NULL,
           cat_musica = NULL, cat_disfrute = NULL
           WHERE id = ?""",
        (entry_id,),
    )




def toggle_habit(conn, entry_id: int) -> bool:
    """Toggle binario desde la ficha (a): NULL o 0 pasan a 1, 1 pasa a 0. El propio
    toggle ES la respuesta a la pregunta, nunca deja NULL."""
    row = conn.execute("SELECT is_habit FROM entries WHERE id = ?", (entry_id,)).fetchone()
    new_value = 0 if row["is_habit"] else 1
    conn.execute("UPDATE entries SET is_habit = ? WHERE id = ?", (new_value, entry_id))
    return bool(new_value)




def toggle_auto_watch(conn, entry_id: int) -> bool:
    """Activa o desactiva el autover de una serie: sync_library marca vistos solos los
    episodios nuevos emitidos. Independiente de is_habit (ver MIGRATIONS)."""
    row = conn.execute("SELECT auto_watch FROM entries WHERE id = ?", (entry_id,)).fetchone()
    new_value = 0 if row["auto_watch"] else 1
    conn.execute("UPDATE entries SET auto_watch = ? WHERE id = ?", (new_value, entry_id))
    return bool(new_value)




def mark_watched_quick(conn, entry_id: int, title_row=None):
    """Marca como vista sin pedir nota/comentario - queda en la cola de /puntuar para puntuar despues.
    En series marca tambien el primer episodio sin visto (normalmente T1E1, via el
    mismo next_unwatched_episode que usa el check de la tarjeta de inicio) - un solo
    episodio, no la serie entera. Si ya se ha visto todo, para eso esta el doble tick."""
    row = conn.execute(
        "SELECT status, title_id, user_id, rewatch_started_at FROM entries WHERE id = ?", (entry_id,)
    ).fetchone()
    if row["status"] == "watched":
        return
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute("UPDATE entries SET status = 'watched', watched_at = ? WHERE id = ?", (now, entry_id))
    if title_row and title_row["type"] == "show":
        first_ep = next_unwatched_episode(conn, row["title_id"], row["rewatch_started_at"], row["user_id"])
        if first_ep:
            _log_episode_watch(conn, first_ep["id"], row["user_id"], now)




TIPOS = {"series": "show", "pelis": "movie"}




def _tipo_sql(tipo):
    """Filtro Todo/Series/Pelis/Anime compartido por pendientes y vistas. "anime" no es
    un titles.type: usa _IS_ANIME_SQL (idioma original japonés)."""
    if tipo == "anime":
        return f" AND {_IS_ANIME_SQL}"
    media_type = TIPOS.get(tipo)
    return (f" AND titles.type = '{media_type}'" if media_type else "")




ORDENES_PENDIENTES = {
    "anadido": "entries.added_at",
    "lanzamiento": "titles.year",
    "nota_internet": "titles.vote_average",
    "titulo": "titles.title COLLATE NOCASE",
}


# Dirección por defecto de cada orden: fechas y notas de más reciente o alto a menos,
# título alfabético. Se puede invertir a mano.
ORDENES_PENDIENTES_DEFAULT_DIR = {"anadido": "desc", "lanzamiento": "desc", "nota_internet": "desc", "titulo": "asc"}




def _orden_sql(columnas: dict, defaults: dict, orden: str, direccion: str) -> str:
    """ORDER BY final: columna según `orden`, dirección explícita (asc/desc) o la de
    por defecto. NULLS LAST siempre, para que lo que no tiene dato no suba arriba."""
    columna = columnas.get(orden, columnas[next(iter(columnas))])
    direccion = direccion if direccion in ("asc", "desc") else defaults.get(orden, "desc")
    return f"{columna} {direccion.upper()} NULLS LAST"




def list_pending(conn, user_id, tipo="", orden="anadido", q="", genero="", direccion=""):
    order_sql = _orden_sql(ORDENES_PENDIENTES, ORDENES_PENDIENTES_DEFAULT_DIR, orden, direccion)
    params: list = [user_id]
    filtro_q = ""
    if q.strip():
        filtro_q = " AND titles.title LIKE ?"
        params.append(f"%{q.strip()}%")
    filtro_genero, genero_params = _genero_sql(genero)
    params.extend(genero_params)
    return conn.execute(
        f"""SELECT entries.*, titles.title, titles.year, titles.poster_path, titles.type,
                   titles.tmdb_id, titles.vote_average, titles.anilist_id, titles.anilist_genres,
                   titles.anilist_studio, titles.anilist_tags, titles.anilist_prequel_ids,
                   titles.anilist_cross_rec_ids, titles.show_status, titles.next_episode_air_date, titles.next_episode_label
           FROM entries JOIN titles ON titles.id = entries.title_id
           WHERE entries.status = 'pending' AND entries.user_id = ?{_tipo_sql(tipo)}{filtro_q}{filtro_genero}
           ORDER BY {order_sql}""",
        params,
    ).fetchall()




def random_pending(conn, user_id, tipo="", excluir: int | None = None):
    """Un pendiente al azar de ESTE usuario para el boton "Sorprendeme". excluir
    descarta el que se acaba de enseñar, para que "Otro" no repita el mismo dos
    veces seguidas."""
    params: list = [user_id]
    filtro_excluir = ""
    if excluir:
        filtro_excluir = " AND titles.tmdb_id != ?"
        params.append(excluir)
    return conn.execute(
        f"""SELECT entries.*, titles.tmdb_id, titles.type, titles.title, titles.year,
                   titles.poster_path, titles.overview, titles.vote_average,
                   titles.runtime_minutes, titles.episode_count, titles.is_adult
           FROM entries JOIN titles ON titles.id = entries.title_id
           WHERE entries.status = 'pending' AND entries.user_id = ?{_tipo_sql(tipo)}{filtro_excluir}
           ORDER BY RANDOM() LIMIT 1""",
        params,
    ).fetchone()




# Duracion total: en series, minutos por episodio x episodios; en pelis, el runtime tal cual.
DURACION_TOTAL_SQL = (
    "titles.runtime_minutes * CASE WHEN titles.type = 'show' "
    "THEN COALESCE(titles.episode_count, 1) ELSE 1 END"
)



ORDENES_VISTAS = {
    "recientes": "entries.watched_at",
    "mi_nota": "entries.rating",
    "nota_internet": "titles.vote_average",
    "duracion": DURACION_TOTAL_SQL,
    "anadido": "entries.added_at",
    "lanzamiento": "titles.year",
    "titulo": "titles.title COLLATE NOCASE",
    "elo": "entries.elo",
}


ORDENES_VISTAS_DEFAULT_DIR = {
    "recientes": "desc", "mi_nota": "desc", "nota_internet": "desc", "elo": "desc",
    "duracion": "desc", "anadido": "desc", "lanzamiento": "desc", "titulo": "asc",
}




def list_watched(conn, user_id: int, orden="recientes", tipo="", q="", genero="", direccion=""):
    order_sql = _orden_sql(ORDENES_VISTAS, ORDENES_VISTAS_DEFAULT_DIR, orden, direccion)
    default_list = get_default_list(conn, user_id)
    params: list = [default_list["id"] if default_list else 0, user_id]
    filtro = ""
    if q.strip():
        filtro = " AND titles.title LIKE ?"
        params.append(f"%{q.strip()}%")
    filtro_genero, genero_params = _genero_sql(genero)
    params.extend(genero_params)
    return conn.execute(
        f"""SELECT entries.*, titles.title, titles.year, titles.poster_path, titles.type, titles.tmdb_id,
                   titles.vote_average, titles.runtime_minutes, titles.episode_count, titles.show_status, titles.next_episode_air_date, titles.next_episode_label,
                   EXISTS(SELECT 1 FROM list_items WHERE list_items.entry_id = entries.id
                          AND list_items.list_id = ?) AS is_favorite,
                   {_IS_ANIME_SQL} AS is_anime
           FROM entries JOIN titles ON titles.id = entries.title_id
           WHERE entries.status = 'watched' AND entries.user_id = ?{_tipo_sql(tipo)}{filtro}{filtro_genero}
           ORDER BY {order_sql}""",
        params,
    ).fetchall()




def start_rewatch(conn, entry_id: int):
    """Botón "Volver a ver": registra una ronda en watch_sessions y, en series, la
    devuelve a "Continuar viendo" desde el principio. Nota, comentario y fecha del
    primer visionado no se tocan. (speed es herencia de un diseño anterior.)

    No borra ninguna fecha: solo guarda cuándo empezó la ronda
    (entries.rewatch_started_at), y _pending_clause trata como pendiente lo visto antes
    de esa fecha. Las fechas anteriores se siguen viendo en la ficha."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute(
        "INSERT INTO watch_sessions (entry_id, watched_at) VALUES (?, ?)", (entry_id, now)
    )
    conn.execute("UPDATE entries SET rewatch_started_at = ? WHERE id = ?", (now, entry_id))




def list_watch_sessions(conn, entry_id: int):
    return conn.execute(
        "SELECT * FROM watch_sessions WHERE entry_id = ? ORDER BY watched_at DESC", (entry_id,)
    ).fetchall()




def count_plays(conn, entry_id: int) -> int:
    """1 (visionado original) + rewatches registrados."""
    extra = conn.execute(
        "SELECT count(*) AS c FROM watch_sessions WHERE entry_id = ?", (entry_id,)
    ).fetchone()["c"]
    return 1 + extra




def set_predicted_score(conn, entry_id: int, predicted: int):
    """Guarda el "% que te gustará" del momento de añadir, solo si no había uno ya, para
    compararlo después con la nota (predicted_outcome_label)."""
    conn.execute(
        "UPDATE entries SET predicted_score = ? WHERE id = ? AND predicted_score IS NULL",
        (predicted, entry_id),
    )




def predicted_outcome_label(predicted: int, rating: float) -> str | None:
    """Expectativa frente a nota final. Nada si falta la predicción o la nota. Gana la
    primera regla que encaje:
    - "Flechazo inesperado": predijo muy bajo (<=40%) y acabó siendo obra maestra (>=9.5)
    - "Superó las expectativas": predijo bajo/medio (<=65%) y puntuó muy alto (>=8.5)
    - "El timo de la temporada": predijo alto (>=80%) y decepcionó (<=4.5)
    """
    actual = rating * 10
    if predicted <= 40 and actual >= 95:
        return "Flechazo inesperado"
    if predicted <= 65 and actual >= 85:
        return "Superó las expectativas"
    if predicted >= 80 and actual <= 45:
        return "El timo de la temporada"
    return None




def get_prediction_calibration(conn, user_id, n_surprises: int = 10):
    """Error medio absoluto de este usuario entre lo predicho al añadir
    (`entries.predicted_score`) y la nota puesta después: por franjas de predicción y
    con las mayores sorpresas. Solo pares reales (predicho y puntuado)."""
    rows = conn.execute(
        """SELECT titles.title, titles.poster_path, titles.tmdb_id, titles.type,
                  entries.predicted_score, entries.rating,
                  (entries.rating * 10 - entries.predicted_score) AS diff
           FROM entries JOIN titles ON titles.id = entries.title_id
           WHERE entries.predicted_score IS NOT NULL AND entries.rating IS NOT NULL AND entries.user_id = ?""",
        (user_id,),
    ).fetchall()
    if not rows:
        return {"n": 0}

    errors = [abs(r["diff"]) for r in rows]
    mae = sum(errors) / len(errors)

    bands = [(0, 20), (20, 40), (40, 60), (60, 80), (80, 101)]
    band_stats = []
    for lo, hi in bands:
        in_band = [r for r in rows if lo <= r["predicted_score"] < hi]
        if not in_band:
            band_stats.append({"lo": lo, "hi": min(hi, 100), "n": 0, "mae": None, "bias": None})
            continue
        band_errors = [abs(r["diff"]) for r in in_band]
        band_bias = sum(r["diff"] for r in in_band) / len(in_band)  # + = se quedo corto, - = se paso
        band_stats.append({
            "lo": lo, "hi": min(hi, 100), "n": len(in_band),
            "mae": sum(band_errors) / len(band_errors), "bias": band_bias,
        })

    surprises = sorted(rows, key=lambda r: -abs(r["diff"]))[:n_surprises]

    return {
        "n": len(rows), "mae": mae, "bands": band_stats,
        "surprises": [
            {"title": r["title"], "poster_path": r["poster_path"], "tmdb_id": r["tmdb_id"],
             "type": r["type"], "predicted": r["predicted_score"], "rating": r["rating"], "diff": r["diff"]}
            for r in surprises
        ],
    }




def list_all_posters(conn):
    """Todas las portadas con imagen real (no el placeholder), para el mosaico
    puramente visual de /mosaico - la "estanteria" de toda la biblioteca."""
    return conn.execute(
        """SELECT tmdb_id, type, poster_path, custom_poster FROM titles
           WHERE poster_path IS NOT NULL AND poster_path != ''
           ORDER BY title COLLATE NOCASE"""
    ).fetchall()




# 'Returning Series' de TMDB no significa "en emisión": mucho anime se queda así años
# entre temporadas. Solo se excluye si hay un próximo episodio con fecha futura (una
# fecha ya pasada en ese caché no es un próximo episodio real); si el de hoy aún no está
# visto, _SIN_PENDIENTES_SQL ya se encarga.
_NO_EN_EMISION_SQL = (
    "(titles.next_episode_air_date IS NULL OR date(titles.next_episode_air_date) <= date('now'))"
)

# Una serie pasa a 'watched' con su primer episodio, así que una serie a medias no
# puede entrar en /puntuar: si le queda algún episodio emitido sin ver (mismo criterio
# que missing_eps), no está lista. Mira episode_watches del usuario de la fila de
# entries, que tiene que estar en la query.
_SIN_PENDIENTES_SQL = """(
    titles.type != 'show'
    OR NOT EXISTS (
        SELECT 1 FROM episodes
        WHERE episodes.title_id = titles.id
          AND NOT EXISTS (
              SELECT 1 FROM episode_watches ew WHERE ew.episode_id = episodes.id
                AND ew.user_id = entries.user_id
                AND (entries.rewatch_started_at IS NULL OR ew.watched_at >= entries.rewatch_started_at)
          )
          AND episodes.air_date IS NOT NULL
          AND date(episodes.air_date) <= date('now')
    )
)"""




def _entry_unrated_season(conn, entry_id: int, title_id: int, user_id: int, rewatch_started_at) -> int | None:
    """Para una entry que ya puntúa por temporada (>=1 fila en season_ratings): la
    temporada más baja con todos sus episodios cacheados vistos y sin nota, o None. No
    exige que la temporada haya terminado de emitir."""
    rated = {
        r["season_number"]
        for r in conn.execute("SELECT season_number FROM season_ratings WHERE entry_id = ?", (entry_id,)).fetchall()
    }
    if not rated:
        return None
    seasons: dict[int, list[int]] = {}
    for row in conn.execute(
        "SELECT id, season_number FROM episodes WHERE title_id = ? AND season_number > 0", (title_id,)
    ).fetchall():
        seasons.setdefault(row["season_number"], []).append(row["id"])
    for season_number in sorted(s for s in seasons if s not in rated):
        ep_ids = seasons[season_number]
        placeholders = ",".join("?" * len(ep_ids))
        query = f"""SELECT COUNT(DISTINCT episode_id) AS c FROM episode_watches
                    WHERE user_id = ? AND episode_id IN ({placeholders})"""
        params = [user_id, *ep_ids]
        if rewatch_started_at is not None:
            query += " AND watched_at >= ?"
            params.append(rewatch_started_at)
        watched = conn.execute(query, params).fetchone()["c"]
        if watched == len(ep_ids):
            return season_number
    return None


def _season_review_candidates(conn, user_id: int):
    """Entries de ESTE usuario que ya usan puntuacion por temporada - candidatas a
    tener una temporada nueva completa sin puntuar todavia (ver _entry_unrated_season)."""
    return conn.execute(
        """SELECT DISTINCT entries.id, entries.title_id, entries.rewatch_started_at,
                  titles.title, titles.year, titles.poster_path, titles.type, titles.tmdb_id
           FROM entries
           JOIN season_ratings ON season_ratings.entry_id = entries.id
           JOIN titles ON titles.id = entries.title_id
           WHERE entries.user_id = ? AND entries.status = 'watched'""",
        (user_id,),
    ).fetchall()


def count_review_queue(conn, user_id: int) -> int:
    """Vistas de este usuario sin nota, sin contar series en emisión
    (_NO_EN_EMISION_SQL) ni a medio ver (_SIN_PENDIENTES_SQL). Suma también las series
    puntuadas por temporada con una temporada nueva completa sin puntuar."""
    base = conn.execute(
        f"""SELECT count(*) AS c FROM entries JOIN titles ON titles.id = entries.title_id
           WHERE entries.status = 'watched' AND entries.rating IS NULL AND entries.user_id = ?
             AND {_NO_EN_EMISION_SQL} AND {_SIN_PENDIENTES_SQL}""",
        (user_id,),
    ).fetchone()["c"]
    extra = sum(
        1
        for row in _season_review_candidates(conn, user_id)
        if _entry_unrated_season(conn, row["id"], row["title_id"], user_id, row["rewatch_started_at"]) is not None
    )
    return base + extra




def next_review_item(conn, user_id: int, excluir_ids: list[int]):
    """Una entry al azar de este usuario sin nota, con los mismos filtros que
    count_review_queue y excluyendo las ya pasadas en esta ronda. Si no queda ninguna,
    una serie con temporada nueva sin puntuar: `season_number` (None en el caso normal)
    indica a la plantilla si enlazar al examen general o a /temporada/{n}/puntuar."""
    query = f"""SELECT entries.*, titles.title, titles.year, titles.poster_path, titles.type, titles.tmdb_id
               FROM entries JOIN titles ON titles.id = entries.title_id
               WHERE entries.status = 'watched' AND entries.rating IS NULL AND entries.user_id = ?
                 AND {_NO_EN_EMISION_SQL} AND {_SIN_PENDIENTES_SQL}"""
    params: list = [user_id]
    if excluir_ids:
        placeholders = ",".join("?" * len(excluir_ids))
        query += f" AND entries.id NOT IN ({placeholders})"
        params.extend(excluir_ids)
    query += " ORDER BY RANDOM() LIMIT 1"
    row = conn.execute(query, params).fetchone()
    if row is not None:
        result = dict(row)
        result["season_number"] = None
        return result

    for candidate in _season_review_candidates(conn, user_id):
        if candidate["id"] in excluir_ids:
            continue
        season_number = _entry_unrated_season(
            conn, candidate["id"], candidate["title_id"], user_id, candidate["rewatch_started_at"]
        )
        if season_number is not None:
            result = dict(candidate)
            result["season_number"] = season_number
            return result
    return None




def remove_pending_entry(conn, entry_id: int):
    """Borra un pendiente entero y lo que cuelga de él (listas, personajes favoritos,
    rewatches). Solo para pending. titles/episodes/characters son catálogo compartido:
    solo se borran si ningún otro usuario sigue el título."""
    row = conn.execute(
        "SELECT title_id FROM entries WHERE id = ? AND status = 'pending'", (entry_id,)
    ).fetchone()
    if not row:
        return
    title_id = row["title_id"]
    conn.execute("DELETE FROM favorite_characters WHERE entry_id = ?", (entry_id,))
    conn.execute("DELETE FROM list_items WHERE entry_id = ?", (entry_id,))
    conn.execute("DELETE FROM watch_sessions WHERE entry_id = ?", (entry_id,))
    conn.execute("DELETE FROM rating_history WHERE entry_id = ?", (entry_id,))
    conn.execute("DELETE FROM season_ratings WHERE entry_id = ?", (entry_id,))
    conn.execute("DELETE FROM entries WHERE id = ?", (entry_id,))
    still_tracked = conn.execute(
        "SELECT EXISTS(SELECT 1 FROM entries WHERE title_id = ?)", (title_id,)
    ).fetchone()[0]
    if still_tracked:
        return
    conn.execute(
        "DELETE FROM favorite_characters WHERE character_id IN (SELECT id FROM characters WHERE title_id = ?)",
        (title_id,),
    )
    conn.execute("DELETE FROM characters WHERE title_id = ?", (title_id,))
    conn.execute(
        "DELETE FROM episode_user_state WHERE episode_id IN (SELECT id FROM episodes WHERE title_id = ?)",
        (title_id,),
    )
    conn.execute(
        "DELETE FROM episode_watches WHERE episode_id IN (SELECT id FROM episodes WHERE title_id = ?)",
        (title_id,),
    )
    # Los comentarios del debate van antes que episodes, por la FOREIGN KEY.
    conn.execute(
        "DELETE FROM episode_comments WHERE episode_id IN (SELECT id FROM episodes WHERE title_id = ?)",
        (title_id,),
    )
    conn.execute("DELETE FROM episodes WHERE title_id = ?", (title_id,))
    conn.execute("DELETE FROM titles WHERE id = ?", (title_id,))




def _genero_sql(genero):
    """Filtro de género compartido por /pendientes y /vistas: LIKE sobre titles.genres
    probando el nombre en español y en inglés (depende del idioma de TMDB al cachear,
    ver _GENRE_ES). 'Anime' usa _IS_ANIME_SQL (idioma original japonés), porque el
    género Animación también incluye animación occidental."""
    if not genero:
        return "", []
    if genero == "Anime":
        return f" AND {_IS_ANIME_SQL}", []
    variants = {genero}
    for en, es in _GENRE_ES.items():
        if es == genero:
            variants.add(en)
    clauses = " OR ".join("titles.genres LIKE ?" for _ in variants)
    params = [f"%{v}%" for v in variants]
    return f" AND ({clauses})", params




def list_genres(conn):
    """Generos unicos ya presentes en la biblioteca, en español, para el desplegable
    de filtro de /pendientes y /vistas. 'Anime' va siempre primero, aunque no sea un
    genero real de TMDB (ver _genero_sql)."""
    names = set()
    for row in conn.execute("SELECT DISTINCT genres FROM titles WHERE genres IS NOT NULL AND genres != ''"):
        for g in row["genres"].split(","):
            g = g.strip()
            if g:
                names.add(_GENRE_ES.get(g, g))
    return ["Anime", *sorted(names)]
