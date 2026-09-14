"""app.repo.titles - extraido de repo.py en el split de modulos (ronda 2026-08-21).
Ver app/repo/__init__.py para el mapa completo de que vive en cada fichero -
el resto del proyecto sigue usando `from app import repo; repo.funcion(...)`
exactamente igual que antes, este split es puramente interno."""
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone

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
    """Portada propia subida a mano desde la ficha - ver POST /titulo/{id}/{type}/portada.
    custom_poster=1 para poder distinguirla de una portada de TMDB en Mosaico/ficha
    (Tara: "que en el mosaico se vea qué es lo que he añadido", 2026-08-13) - sin este
    flag no había forma de saberlo, el nombre de fichero es igual en los dos casos."""
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




def refresh_metadata(conn, title_row):
    """Vuelve a consultar TMDB para refrescar nota/duracion (y proximo episodio si es serie).
    Tambien pisa titulo/sinopsis/generos: los titulos cacheados antes del cambio a
    es-ES se quedaban en ingles para siempre, sin ninguna sync los volviera a pedir."""
    if title_row["tmdb_id"] < 0:
        return
    details = tmdb.get_details(title_row["tmdb_id"], title_row["type"])
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
            details.get("next_episode_air_date"),
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
    for episodes in episodes_by_season:
        for ep in episodes:
            conn.execute(
                """INSERT INTO episodes (title_id, season_number, episode_number, name, air_date)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(title_id, season_number, episode_number)
                   DO UPDATE SET name = excluded.name, air_date = excluded.air_date""",
                (title_row["id"], ep["season_number"], ep["episode_number"], ep["name"], ep["air_date"]),
            )




def list_episodes(conn, title_id):
    # watch_count (2026-08-20, "x2/x3" visual del rewatch): cuantas veces se ha
    # marcado ESTE episodio en total (episode_watches), no solo "visto si/no" - Tara
    # pidio explicitamente que un episodio ya visto antes nunca se vea "vacio" al
    # empezar un rewatch, solo que el contador suba.
    return conn.execute(
        """SELECT episodes.*,
                  (SELECT count(*) FROM episode_watches
                     WHERE episode_watches.episode_id = episodes.id) AS watch_count
           FROM episodes WHERE title_id = ? ORDER BY season_number, episode_number""",
        (title_id,),
    ).fetchall()




def toggle_episode(conn, episode_id):
    """Desmarcar el ultimo episodio visto de una serie dejaba la entry en 'watched'
    sin un solo episodio visto de verdad (Tara: "no esta vista por ningun ep, esto es
    un bug, deberia estar en pendientes") - si al desmarcar no queda ningun episodio
    visto, la entry vuelve a 'pending'. "Visto ahora" es round-aware (2026-08-20): si
    hay un rewatch en curso, un episodio con fecha ANTERIOR a esa ronda cuenta como
    pendiente de re-ver, aunque episodes.watched_at no sea NULL - sin esto, tocar el
    checkbox de un episodio ya visto en una ronda anterior lo desmarcaria (accion
    contraria a lo que se ve en pantalla: la casilla aparece vacia = "falta por ver")."""
    row = conn.execute("SELECT title_id, watched_at FROM episodes WHERE id = ?", (episode_id,)).fetchone()
    rewatch_started_at = conn.execute(
        "SELECT rewatch_started_at FROM entries WHERE title_id = ?", (row["title_id"],)
    ).fetchone()["rewatch_started_at"]
    checked_now = bool(row["watched_at"]) and (
        not rewatch_started_at or row["watched_at"] >= rewatch_started_at
    )
    if checked_now:
        _unlog_episode_watch(conn, episode_id)
        quedan = conn.execute(
            "SELECT count(*) AS c FROM episodes WHERE title_id = ? AND watched_at IS NOT NULL",
            (row["title_id"],),
        ).fetchone()["c"]
        if quedan == 0:
            conn.execute(
                "UPDATE entries SET status = 'pending' WHERE title_id = ? AND status = 'watched'",
                (row["title_id"],),
            )
    else:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        _log_episode_watch(conn, episode_id, now)
        _promote_if_first_watch(conn, row["title_id"])




def rewatch_episode(conn, episode_id):
    """Volver a ver UN episodio suelto (2026-08-21, Tara: "puedo mirar violet ep 7 por
    x motivo y no quiero ver todo") - a diferencia de toggle_episode, nunca desmarca:
    siempre añade una fila nueva a episode_watches, sin tocar entries.status ni
    rewatch_started_at. Pensado para episodios YA vistos (watched_now=True) - si el
    episodio no estaba visto, toggle_episode ya sirve para marcarlo la primera vez."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    _log_episode_watch(conn, episode_id, now)


def list_episode_watch_dates(conn, episode_id) -> list[str]:
    """Todas las fechas en que se ha marcado visto este episodio, mas recientes primero -
    para el historial plegable junto al boton "Volver a ver este episodio"."""
    rows = conn.execute(
        "SELECT watched_at FROM episode_watches WHERE episode_id = ? ORDER BY watched_at DESC",
        (episode_id,),
    ).fetchall()
    return [r["watched_at"] for r in rows]


def mark_all_aired_watched(conn, title_id):
    """El doble tick de la tarjeta de inicio: marca vistos todos los emitidos de golpe
    - round-aware (ver _pending_clause), asi que durante un rewatch en curso vuelve a
    tragarse tambien lo visto en rondas anteriores, no solo lo nunca visto."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    rewatch_started_at = conn.execute(
        "SELECT rewatch_started_at FROM entries WHERE title_id = ?", (title_id,)
    ).fetchone()["rewatch_started_at"]
    clause, params = _pending_clause(rewatch_started_at)
    rows = conn.execute(
        f"""SELECT id FROM episodes WHERE title_id = ? AND {clause}
           AND air_date IS NOT NULL AND date(air_date) <= date('now')""",
        (title_id, *params),
    ).fetchall()
    for r in rows:
        _log_episode_watch(conn, r["id"], now)
    _promote_if_first_watch(conn, title_id)




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
    """Puntua una temporada suelta en vez de la serie entera - pensado para series donde
    unas temporadas valen mas que otras (Tara: 'hay series que valoro mas la primera que
    la segunda') y una sola nota por serie completa las mezclaba todas en una media mala."""
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




def mark_season_watched(conn, title_id, season_number: int):
    """Traga una temporada entera de una sentada: marca vistos los episodios ya
    emitidos de ESA temporada en concreto, sin ir episodio a episodio ni marcar
    la serie entera (eso ya lo hace el doble tick de la tarjeta de inicio).
    Round-aware, igual que mark_all_aired_watched."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    rewatch_started_at = conn.execute(
        "SELECT rewatch_started_at FROM entries WHERE title_id = ?", (title_id,)
    ).fetchone()["rewatch_started_at"]
    clause, params = _pending_clause(rewatch_started_at)
    rows = conn.execute(
        f"""SELECT id FROM episodes WHERE title_id = ? AND season_number = ? AND {clause}
           AND air_date IS NOT NULL AND date(air_date) <= date('now')""",
        (title_id, season_number, *params),
    ).fetchall()
    for r in rows:
        _log_episode_watch(conn, r["id"], now)
    _promote_if_first_watch(conn, title_id)




def toggle_episode_favorite(conn, episode_id) -> bool:
    row = conn.execute("SELECT is_favorite FROM episodes WHERE id = ?", (episode_id,)).fetchone()
    new_value = 0 if row["is_favorite"] else 1
    conn.execute("UPDATE episodes SET is_favorite = ? WHERE id = ?", (new_value, episode_id))
    return bool(new_value)




def set_episode_comment(conn, episode_id, comment: str):
    conn.execute("UPDATE episodes SET comment = ? WHERE id = ?", (comment.strip() or None, episode_id))




def list_history(conn, limit=300):
    """Cronologico unificado, lo mas reciente primero: episodios vistos, titulos marcados
    vistos y rewatches. 'detail' es la etiqueta corta de la fila (T1E6, Vista entera, Rewatch).

    Cada subconsulta se ordena y corta a `limit` ANTES del UNION ALL: el top-`limit`
    final nunca puede necesitar mas de `limit` filas de una sola fuente, asi que cortar
    cada una por separado (usando el indice de watched_at) evita materializar y ordenar
    TODO el historial (miles de episodios en una biblioteca grande) solo para quedarse
    con los primeros 300. SQLite exige envolver cada rama en su propia subconsulta para
    poder llevar ORDER BY/LIMIT propios dentro de un UNION ALL."""
    return conn.execute(
        """SELECT * FROM (
             SELECT * FROM (
               SELECT episodes.watched_at AS at, titles.title, titles.tmdb_id, titles.type,
                      titles.poster_path,
                      'T' || episodes.season_number || 'E' || episodes.episode_number AS detail
               FROM episodes JOIN titles ON titles.id = episodes.title_id
               WHERE episodes.watched_at IS NOT NULL
               ORDER BY episodes.watched_at DESC LIMIT ?
             )
             UNION ALL
             SELECT * FROM (
               SELECT entries.watched_at AS at, titles.title, titles.tmdb_id, titles.type,
                      titles.poster_path, 'Vista entera' AS detail
               FROM entries JOIN titles ON titles.id = entries.title_id
               WHERE entries.watched_at IS NOT NULL
               ORDER BY entries.watched_at DESC LIMIT ?
             )
             UNION ALL
             SELECT * FROM (
               SELECT watch_sessions.watched_at AS at, titles.title, titles.tmdb_id, titles.type,
                      titles.poster_path, 'Volver a ver' AS detail
               FROM watch_sessions
               JOIN entries ON entries.id = watch_sessions.entry_id
               JOIN titles ON titles.id = entries.title_id
               ORDER BY watch_sessions.watched_at DESC LIMIT ?
             )
           )
           ORDER BY at DESC LIMIT ?""",
        (limit, limit, limit, limit),
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




def list_calendar(conn, days_before=7, days_after=30):
    """Episodios (de la tabla episodes, no solo el proximo) que caen en la ventana.
    Solo de series EMPEZADAS (>=1 episodio visto) - las que aun no ha tocado solo meten ruido."""
    return conn.execute(
        """SELECT titles.tmdb_id, titles.title, titles.poster_path,
                  episodes.season_number, episodes.episode_number, episodes.name AS ep_name,
                  episodes.air_date, episodes.watched_at, entries.auto_watch
           FROM episodes
           JOIN titles ON titles.id = episodes.title_id
           JOIN entries ON entries.title_id = titles.id
           WHERE episodes.air_date IS NOT NULL
             AND date(episodes.air_date) BETWEEN date('now', ?) AND date('now', ?)
             AND EXISTS(SELECT 1 FROM episodes started
                        WHERE started.title_id = titles.id AND started.watched_at IS NOT NULL)
           ORDER BY episodes.air_date, titles.title, episodes.episode_number""",
        (f"-{days_before} days", f"+{days_after} days"),
    ).fetchall()




# Series pendientes con sus contadores de episodios, para la pantalla de inicio estilo Trakt.
# "in_season" = tiene algun episodio emitido/programado a +-21 dias de hoy; el
# show_status = 'Returning Series' de TMDB NO vale de filtro (sigue activo entre temporadas).
_HOME_SHOWS_SQL = """
    SELECT entries.*, titles.title, titles.year, titles.poster_path, titles.type,
           titles.tmdb_id, titles.runtime_minutes, titles.release_date,
           (SELECT count(*) FROM episodes WHERE episodes.title_id = titles.id
              AND episodes.watched_at IS NOT NULL) AS watched_eps,
           -- "Falta por ver AHORA": nunca visto, o la unica vez que se vio fue ANTES de
           -- que empezara la ronda de rewatch activa (entries.rewatch_started_at, ver
           -- start_rewatch - ya NO borra episodes.watched_at, asi que sin esta comparacion
           -- todo parece "visto para siempre" y la serie nunca volveria a Continuar viendo).
           (SELECT count(*) FROM episodes WHERE episodes.title_id = titles.id
              AND (episodes.watched_at IS NULL
                   OR (entries.rewatch_started_at IS NOT NULL
                       AND episodes.watched_at < entries.rewatch_started_at))
              AND episodes.air_date IS NOT NULL
              AND date(episodes.air_date) <= date('now')) AS missing_eps,
           (SELECT max(episodes.watched_at) FROM episodes
              WHERE episodes.title_id = titles.id) AS last_watched_at,
           (SELECT max(watch_sessions.watched_at) FROM watch_sessions
              WHERE watch_sessions.entry_id = entries.id) AS last_rewatch_at,
           EXISTS(SELECT 1 FROM episodes WHERE episodes.title_id = titles.id
              AND episodes.air_date IS NOT NULL
              AND date(episodes.air_date) BETWEEN date('now', '-21 days')
                                              AND date('now', '+21 days')) AS in_season
    FROM entries JOIN titles ON titles.id = entries.title_id
    WHERE titles.type = 'show'"""




def next_unwatched_episode(conn, title_id: int, rewatch_started_at: str | None = None):
    """El siguiente episodio ya emitido que falta por ver (el 'T1E6' de la tarjeta).
    Round-aware (ver _pending_clause): con un rewatch en curso, el primer episodio con
    fecha anterior a esa ronda cuenta otra vez como "el siguiente", sin que haga falta
    borrar su fecha vieja."""
    clause, params = _pending_clause(rewatch_started_at)
    return conn.execute(
        f"""SELECT * FROM episodes
           WHERE title_id = ? AND {clause} AND air_date IS NOT NULL
             AND date(air_date) <= date('now') AND season_number > 0
           ORDER BY season_number, episode_number LIMIT 1""",
        (title_id, *params),
    ).fetchone()




def _with_next_episode(conn, row):
    data = dict(row)
    data["next_ep"] = next_unwatched_episode(conn, row["title_id"], row["rewatch_started_at"])
    return data




def list_continue_watching(conn):
    """Estilo Trakt: cualquier serie con al menos un episodio visto y emitidos por ver,
    lo mas reciente primero - da igual el status de la entry (The Boys a medias cuenta
    aunque este marcada vista y no emita ahora). Las no empezadas NO salen aqui.
    "missing_eps" es round-aware (ver _HOME_SHOWS_SQL): una serie recien puesta en
    "Volver a ver" vuelve a tener episodios "pendientes" desde el principio sin que se
    le haya borrado ninguna fecha vieja de episodes.watched_at."""
    rows = conn.execute(_HOME_SHOWS_SQL).fetchall()
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




def list_new_airing(conn):
    """Series recien añadidas (pendientes, 0 episodios vistos) que YA tienen algun
    episodio emitido esperando Y son de la temporada actual (release_date dentro de la
    temporada en curso) - complementario a list_continue_watching (esa exige >=1
    visto, esta exige 0). Pedido por Tara (2026-08-13): si añade algo con solo el
    primer episodio fuera porque el resto aun no ha salido, quiere que se lo recuerde
    "arriba" en vez de perderse entre pendientes - pero solo lo de esta temporada, no
    cualquier pendiente atrasado de hace tiempo con un episodio suelto sin ver."""
    rows = conn.execute(_HOME_SHOWS_SQL).fetchall()
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




def get_home_card(conn, entry_id: int):
    """Re-render de una tarjeta tras marcar un episodio (check individual o doble tick).
    Aplica el MISMO filtro que list_continue_watching (empezada Y quedan emitidos por
    ver) - sin esto, marcar el ultimo episodio que faltaba devolvia la fila igual
    (missing_eps=0) y la tarjeta se quedaba en pantalla con el badge "Al dia" en vez de
    desaparecer del carrusel, obligando a recargar para que se fuera de verdad. Bug
    real, Tara: "si marco visto deberia salir, no quedarse ahi con 'esta visto y ya'"."""
    row = conn.execute(_HOME_SHOWS_SQL + " AND entries.id = ?", (entry_id,)).fetchone()
    if not row or not ((row["watched_eps"] or row["last_rewatch_at"]) and row["missing_eps"]):
        return None
    return _with_next_episode(conn, row)




def list_trackable_titles(conn):
    """Todo lo que Tara sigue (pendiente o visto) con tmdb_id real - candidatos a refrescar metadatos."""
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




def create_manual_entry(conn, media_type: str, title: str, year: int | None, poster_url: str | None):
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

    entry = conn.execute("SELECT * FROM entries WHERE title_id = ?", (title_row["id"],)).fetchone()
    if not entry:
        conn.execute("INSERT INTO entries (title_id, status) VALUES (?, 'pending')", (title_row["id"],))
    return get_title(conn, title_row["tmdb_id"])




def get_entry_states(conn, tmdb_ids: list[int]) -> dict[int, str]:
    """Estado real (pending/watched) de una tanda de tmdb_ids ya en la biblioteca -
    para que resultados de busqueda/similares no ofrezcan '+ Pendientes' en algo que
    ya esta visto y en Favoritos (Black Clover: ya lo tenia, el buscador lo trataba
    como nuevo). Los que no aparecen en el dict son 'new'."""
    if not tmdb_ids:
        return {}
    placeholders = ",".join("?" * len(tmdb_ids))
    rows = conn.execute(
        f"""SELECT titles.tmdb_id, entries.status FROM titles
           JOIN entries ON entries.title_id = titles.id
           WHERE titles.tmdb_id IN ({placeholders})""",
        tmdb_ids,
    ).fetchall()
    return {row["tmdb_id"]: row["status"] for row in rows}




def ensure_entry(conn, tmdb_id: int, media_type: str):
    """Devuelve la fila de entries para ese titulo, creandola como 'pending' si no existe."""
    title = ensure_title(conn, tmdb_id, media_type)
    entry = conn.execute("SELECT * FROM entries WHERE title_id = ?", (title["id"],)).fetchone()
    if entry:
        return entry
    conn.execute("INSERT INTO entries (title_id, status) VALUES (?, 'pending')", (title["id"],))
    return conn.execute("SELECT * FROM entries WHERE title_id = ?", (title["id"],)).fetchone()




def get_entry_with_title(conn, entry_id: int):
    return conn.execute(
        """SELECT entries.*, titles.title, titles.year, titles.poster_path, titles.type,
                  titles.tmdb_id, titles.release_date
           FROM entries JOIN titles ON titles.id = entries.title_id
           WHERE entries.id = ?""",
        (entry_id,),
    ).fetchone()




def _snapshot_rating_history(conn, entry_id: int):
    """Guarda la nota/categorias/comentario actuales en rating_history antes de pisarlas -
    para poder ver como cambia de opinion Tara con el tiempo ('le puse un 9 hace dos años,
    ahora un 6'). Solo si ya habia una nota puesta - no tiene sentido guardar un hueco vacio."""
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
    reseed_undueled_entries_elo(conn)




def set_watched_at(conn, entry_id: int, date_str: str):
    """Corrige la fecha de visionado de una entry YA marcada como vista, sin pasar por
    el formulario de nota (Tara: series vistas hace tiempo que se marcaron con la fecha
    de hoy al importarlas/recuperarlas, e inflaban las estadisticas del año en curso).
    Solo el dia, la hora se fija a mediodia UTC para que no cambie de dia por huso horario.

    Bug real #1 (Tara, 2026-08-23): "Nande Koko ni Sensei ga!?" corregida a 2019 en la
    ficha, pero seguia saliendo con hoy en /historial y en "Episodios por mes" - esto
    solo tocaba entries.watched_at (el resumen de la entry), pero para una serie la
    fecha que de verdad alimenta historial/calendario/estadisticas es
    episodes.watched_at (cache de episode_watches, ver _log_episode_watch) - la
    entry y sus episodios podian quedar con dos fechas distintas sin que la UI
    avisara. Se corrige tambien el ultimo marcado de cada episodio ya visto (UPDATE
    en el sitio, no _log_episode_watch: esto es arreglar un dato, no un nuevo
    visionado - no debe sumar al contador "x2" de episode_watches).

    Bug real #2, misma ronda (Tara, captura real de "Episodios"): el primer intento
    ponia LA MISMA fecha (la escrita en el formulario) en los 12 episodios de golpe -
    "como vas a poner la fecha y se me pone la fecha del primer ep y no de la fecha
    que le corresponde". Cada episodio usa su PROPIO air_date (ya visible en la propia
    fila, columna izquierda) en vez de la fecha unica del formulario - as calza con
    el patron real de ver una serie semana a semana segun emite, no de un tiron el
    mismo dia. El valor escrito en el formulario se queda como fallback solo para
    episodios sin air_date cacheado (altas manuales, episodios sin fecha de TMDB)."""
    entry = conn.execute("SELECT title_id FROM entries WHERE id = ?", (entry_id,)).fetchone()
    new_value = f"{date_str}T12:00:00Z"
    conn.execute(
        "UPDATE entries SET watched_at = ? WHERE id = ? AND status = 'watched'",
        (new_value, entry_id),
    )
    if entry is None:
        return
    episodes = conn.execute(
        "SELECT id, air_date FROM episodes WHERE title_id = ? AND watched_at IS NOT NULL",
        (entry["title_id"],),
    ).fetchall()
    for ep in episodes:
        ep_value = f"{ep['air_date']}T12:00:00Z" if ep["air_date"] else new_value
        last_watch = conn.execute(
            "SELECT id FROM episode_watches WHERE episode_id = ? ORDER BY watched_at DESC, id DESC LIMIT 1",
            (ep["id"],),
        ).fetchone()
        if last_watch:
            conn.execute("UPDATE episode_watches SET watched_at = ? WHERE id = ?", (ep_value, last_watch["id"]))
        conn.execute("UPDATE episodes SET watched_at = ? WHERE id = ?", (ep_value, ep["id"]))




def undo_mark_watched(conn, entry_id: int):
    """Deshace un "Marcar vista" por error (Tara, Tougen Anki 2026-08-13: lo marco sin
    querer, no se lo ha visto de verdad, y no habia ninguna via en la UI para revertirlo
    del todo - el desmarcado por episodio solo revierte a pending si habia justo un
    episodio marcado, pero si el marcado inicial ni siquiera llego a marcar ninguno
    (hipo de TMDB, ver mark_watched_quick) no quedaba ningun checkbox que desmarcar).
    Vuelve la entry a pending y desmarca solo lo marcado en ESE mismo instante - solo
    tiene sentido para lo recien marcado sin puntuar (ver la condicion en
    title_detail.html, entry.rating is none); si ya se puntuo, es "Quitar nota" quien
    aplica. 2026-08-20: ya NO desmarca TODOS los episodios del titulo a ciegas - solo
    los que comparten el mismo watched_at que puso mark_watched_quick en la entry (asi
    no se pierden episodios ya vistos de verdad de antes, si el titulo ya tenia
    historial parcial cuando se hizo el marcado por error)."""
    row = conn.execute("SELECT title_id, watched_at FROM entries WHERE id = ?", (entry_id,)).fetchone()
    conn.execute("UPDATE entries SET status = 'pending', watched_at = NULL WHERE id = ?", (entry_id,))
    if row["watched_at"]:
        eps = conn.execute(
            "SELECT id FROM episodes WHERE title_id = ? AND watched_at = ?",
            (row["title_id"], row["watched_at"]),
        ).fetchall()
        for ep in eps:
            _unlog_episode_watch(conn, ep["id"])




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
    """Toggle binario en la ficha: la serie marcada se autover - en cuanto sync_library
    detecta episodios nuevos emitidos, se marcan vistos solos (ver mas abajo), sin que
    Tara tenga que entrar ni clicar "Marcar vista" cada semana. Ejemplo pedido: One
    Piece. Flag independiente de is_habit, ver el porque en MIGRATIONS."""
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
        "SELECT status, title_id, rewatch_started_at FROM entries WHERE id = ?", (entry_id,)
    ).fetchone()
    if row["status"] == "watched":
        return
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute("UPDATE entries SET status = 'watched', watched_at = ? WHERE id = ?", (now, entry_id))
    if title_row and title_row["type"] == "show":
        first_ep = next_unwatched_episode(conn, row["title_id"], row["rewatch_started_at"])
        if first_ep:
            _log_episode_watch(conn, first_ep["id"], now)




TIPOS = {"series": "show", "pelis": "movie"}




def _tipo_sql(tipo):
    """Filtro Todo/Series/Pelis/Anime compartido por pendientes y vistas. "anime" no es
    un titles.type real (series/peliculas ya sean anime o no) - reusa _IS_ANIME_SQL,
    el mismo criterio (idioma original japones) que ya usa el duelo global y el genero
    "Anime" del desplegable (Tara: "ya tienes la categoria montada", 2026-08-13)."""
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


# Direccion por defecto de cada orden si Tara no la ha tocado - fechas/notas de mas
# reciente/alto a menos, titulo alfabetico. Tara puede invertir cualquiera a mano.
ORDENES_PENDIENTES_DEFAULT_DIR = {"anadido": "desc", "lanzamiento": "desc", "nota_internet": "desc", "titulo": "asc"}




def _orden_sql(columnas: dict, defaults: dict, orden: str, direccion: str) -> str:
    """Arma el ORDER BY final: columna segun `orden`, direccion explicita si Tara la ha
    puesto (asc/desc) o el default de esa columna si no. NULLS LAST siempre, para que
    los titulos sin nota/duracion/año cacheado no se cuelen arriba en ascendente."""
    columna = columnas.get(orden, columnas[next(iter(columnas))])
    direccion = direccion if direccion in ("asc", "desc") else defaults.get(orden, "desc")
    return f"{columna} {direccion.upper()} NULLS LAST"




def list_pending(conn, tipo="", orden="anadido", q="", genero="", direccion=""):
    order_sql = _orden_sql(ORDENES_PENDIENTES, ORDENES_PENDIENTES_DEFAULT_DIR, orden, direccion)
    params: list = []
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
                   titles.anilist_cross_rec_ids
           FROM entries JOIN titles ON titles.id = entries.title_id
           WHERE entries.status = 'pending'{_tipo_sql(tipo)}{filtro_q}{filtro_genero}
           ORDER BY {order_sql}""",
        params,
    ).fetchall()




def random_pending(conn, tipo="", excluir: int | None = None):
    """Un pendiente al azar para el boton "Sorprendeme". excluir descarta el que
    se acaba de enseñar, para que "Otro" no repita el mismo dos veces seguidas."""
    params: list = []
    filtro_excluir = ""
    if excluir:
        filtro_excluir = " AND titles.tmdb_id != ?"
        params.append(excluir)
    return conn.execute(
        f"""SELECT entries.*, titles.tmdb_id, titles.type, titles.title, titles.year,
                   titles.poster_path, titles.overview, titles.vote_average,
                   titles.runtime_minutes, titles.episode_count, titles.is_adult
           FROM entries JOIN titles ON titles.id = entries.title_id
           WHERE entries.status = 'pending'{_tipo_sql(tipo)}{filtro_excluir}
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




def list_watched(conn, orden="recientes", tipo="", q="", genero="", direccion=""):
    order_sql = _orden_sql(ORDENES_VISTAS, ORDENES_VISTAS_DEFAULT_DIR, orden, direccion)
    default_list = get_default_list(conn)
    params: list = [default_list["id"]]
    filtro = ""
    if q.strip():
        filtro = " AND titles.title LIKE ?"
        params.append(f"%{q.strip()}%")
    filtro_genero, genero_params = _genero_sql(genero)
    params.extend(genero_params)
    return conn.execute(
        f"""SELECT entries.*, titles.title, titles.year, titles.poster_path, titles.type, titles.tmdb_id,
                   titles.vote_average, titles.runtime_minutes, titles.episode_count,
                   EXISTS(SELECT 1 FROM list_items WHERE list_items.entry_id = entries.id
                          AND list_items.list_id = ?) AS is_favorite,
                   {_IS_ANIME_SQL} AS is_anime
           FROM entries JOIN titles ON titles.id = entries.title_id
           WHERE entries.status = 'watched'{_tipo_sql(tipo)}{filtro}{filtro_genero}
           ORDER BY {order_sql}""",
        params,
    ).fetchall()




def start_rewatch(conn, entry_id: int):
    """El boton "Volver a ver": registra una ronda nueva en watch_sessions y, en series,
    la deja lista para volver a "Continuar viendo" desde el principio. La nota, el
    comentario y la fecha del primer visionado no se tocan.
    (La columna speed es herencia del diseño anterior - se queda en su default '1x'.)

    2026-08-20, estilo Trakt: YA NO desmarca (borra la fecha de) ningun episodio -
    Tara: "el volver a ver que hay ahora es poco intuitivo" tras investigar como lo
    hace Trakt ("no plays will be lost - solo recalculamos el progreso desde ese
    punto"). Ahora solo se guarda CUANDO empezo esta ronda (entries.rewatch_started_at)
    - next_unwatched_episode y mark_all_aired_watched/mark_season_watched (ver
    _pending_clause) tratan como "pendiente otra vez" todo lo visto ANTES de esa
    fecha, sin tocar episodes.watched_at para nada. Las fechas del visionado anterior
    quedan intactas y siguen viéndose en la ficha (episode_row.html) aunque el
    episodio vuelva a aparecer como pendiente de re-ver."""
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
    """Guarda la prediccion "% que te gustara" que se enseñaba en la tarjeta del
    calendario en el momento de añadir - solo si no habia ninguna ya (no pisar la
    prediccion original con una recalculada mas tarde, con el perfil ya cambiado).
    Sirve de "expectativa" para comparar contra la nota real una vez puntuada
    (predicted_outcome_label, Tara 2026-08-13)."""
    conn.execute(
        "UPDATE entries SET predicted_score = ? WHERE id = ? AND predicted_score IS NULL",
        (predicted, entry_id),
    )




def predicted_outcome_label(predicted: int, rating: float) -> str | None:
    """'Expectativa vs realidad': compara la prediccion guardada al añadir con la nota
    final. Ninguna señal si no hay prediccion o nota. Umbrales fijados a ojo (Tara no
    dio numeros exactos, solo un par de ejemplos) - de mas extremo a menos, la primera
    que encaje gana:
    - "Flechazo inesperado": predijo muy bajo (<=40%) y acabo siendo obra maestra (>=9.5)
    - "Superó las expectativas": predijo bajo/medio (<=65%) y puntuo muy alto (>=8.5)
    - "El timo de la temporada": predijo alto (>=80%) y decepciono (<=4.5)
    """
    actual = rating * 10
    if predicted <= 40 and actual >= 95:
        return "Flechazo inesperado"
    if predicted <= 65 and actual >= 85:
        return "Superó las expectativas"
    if predicted >= 80 and actual <= 45:
        return "El timo de la temporada"
    return None




def get_prediction_calibration(conn, n_surprises: int = 10):
    """Fase 6 del indice de afinidad (encargo 2026-08-14): error medio absoluto entre
    lo predicho al añadir (`entries.predicted_score`, guardado UNA vez, sin pisar) y
    la nota real puesta despues - en que franjas de predicción el sistema se pasa o se
    queda corto, y las mayores sorpresas en ambos sentidos. Solo sobre pares reales
    (predicho Y puntuado), nada estimado."""
    rows = conn.execute(
        """SELECT titles.title, titles.poster_path, titles.tmdb_id, titles.type,
                  entries.predicted_score, entries.rating,
                  (entries.rating * 10 - entries.predicted_score) AS diff
           FROM entries JOIN titles ON titles.id = entries.title_id
           WHERE entries.predicted_score IS NOT NULL AND entries.rating IS NOT NULL"""
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




# "Returning Series" de TMDB NO significa "en emision ahora" - solo dice que TMDB no
# ha marcado la serie como definitivamente terminada, y muchisimo anime se queda asi
# durante AÑOS entre temporada y temporada (renovaciones tardias). El criterio
# original excluia por ese campo solo, y dejaba 42 de 70 series "pendientes de
# puntuar" atascadas para siempre sin tener nada pendiente de verdad (Frieren,
# Jujutsu Kaisen... con la temporada ya acabada, sin proximo episodio programado) -
# bug real, Tara: "me parecen demasiadas". Ahora excluye solo si HAY un proximo
# episodio con fecha real - eso si es "en emision de verdad", coincide con el mismo
# criterio que ya usa in_season en _HOME_SHOWS_SQL.
_NO_EN_EMISION_SQL = "titles.next_episode_air_date IS NULL"

# Bug real (Tara, 2026-08-21): Dorohedoro entro en /puntuar con solo 4 de 23
# episodios emitidos vistos - entries.status pasa a 'watched' con solo marcar el
# PRIMER episodio (decision de diseño de siempre, ver CLAUDE.md), asi que sin
# esta condicion cualquier serie "empezada y abandonada" (o simplemente a medio
# ver) que ademas no este emitiendo AHORA MISMO (_NO_EN_EMISION_SQL) cae en la
# cola de puntuar antes de que Tara la haya terminado de verdad. Mismo criterio
# de "falta por ver" que ya usa _HOME_SHOWS_SQL (missing_eps) - si es un show y
# le queda algun episodio ya emitido sin ver, no esta lista para puntuar todavia.
_SIN_PENDIENTES_SQL = """(
    titles.type != 'show'
    OR NOT EXISTS (
        SELECT 1 FROM episodes
        WHERE episodes.title_id = titles.id
          AND (episodes.watched_at IS NULL
               OR (entries.rewatch_started_at IS NOT NULL
                   AND episodes.watched_at < entries.rewatch_started_at))
          AND episodes.air_date IS NOT NULL
          AND date(episodes.air_date) <= date('now')
    )
)"""




def count_review_queue(conn) -> int:
    """Vistas importadas sin nota todavia - la 'deuda de datos' de la Fase 2.
    No cuenta series que siguen en emision (ver _NO_EN_EMISION_SQL) ni series a
    medio ver con episodios emitidos pendientes (ver _SIN_PENDIENTES_SQL)."""
    return conn.execute(
        f"""SELECT count(*) AS c FROM entries JOIN titles ON titles.id = entries.title_id
           WHERE entries.status = 'watched' AND entries.rating IS NULL
             AND {_NO_EN_EMISION_SQL} AND {_SIN_PENDIENTES_SQL}"""
    ).fetchone()["c"]




def next_review_item(conn, excluir_ids: list[int]):
    """Una entrada al azar sin nota, excluyendo las ya pasadas en esta ronda de /puntuar,
    las series que siguen en emision (ver _NO_EN_EMISION_SQL) y las que aun tienen
    episodios emitidos sin ver (ver _SIN_PENDIENTES_SQL)."""
    query = f"""SELECT entries.*, titles.title, titles.year, titles.poster_path, titles.type, titles.tmdb_id
               FROM entries JOIN titles ON titles.id = entries.title_id
               WHERE entries.status = 'watched' AND entries.rating IS NULL
                 AND {_NO_EN_EMISION_SQL} AND {_SIN_PENDIENTES_SQL}"""
    params: list = []
    if excluir_ids:
        placeholders = ",".join("?" * len(excluir_ids))
        query += f" AND entries.id NOT IN ({placeholders})"
        params.extend(excluir_ids)
    query += " ORDER BY RANDOM() LIMIT 1"
    return conn.execute(query, params).fetchone()




def remove_pending_entry(conn, entry_id: int):
    """Deshace un pendiente entero (p.ej. una entrada de prueba anadida sin querer, o
    abrir la ficha de una recomendacion solo para mirarla): borra la entry y lo que
    cuelgue de ella (listas, personajes favoritos, rewatches). Solo tiene sentido en
    pending - lo visto no se borra asi, se gestiona desde su ficha.

    Como entries es 1:1 con titles, al borrar la (unica) entry el titulo se queda
    huerfano sin remedio - si no se borra tambien, list_recommendations lo sigue
    tratando como "ya conocido" para siempre y una recomendacion mirada por curiosidad
    desaparecia de recomendados sin haberla baneado y sin que Tara se enterase."""
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
    conn.execute(
        "DELETE FROM favorite_characters WHERE character_id IN (SELECT id FROM characters WHERE title_id = ?)",
        (title_id,),
    )
    conn.execute("DELETE FROM characters WHERE title_id = ?", (title_id,))
    conn.execute("DELETE FROM episodes WHERE title_id = ?", (title_id,))
    conn.execute("DELETE FROM titles WHERE id = ?", (title_id,))




def _genero_sql(genero):
    """Filtro de genero compartido por /pendientes y /vistas: LIKE sobre titles.genres
    (CSV), probando tanto el nombre en español como en ingles - genres se guarda en el
    idioma que tuviera TMDB cuando se cacheo el titulo (ver _GENRE_ES), asi que filtrar
    solo por uno de los dos dejaria fuera la mitad segun cuando se añadio cada cosa.

    'Anime' es una etiqueta propia (no viene de TMDB) - ver _IS_ANIME_SQL/_is_anime():
    idioma original japones como señal principal, no solo genero Animacion (ese genero
    solo tambien mete dibujos occidentales - Futurama, Rick and Morty, Bluey... - bug
    real visto en el duelo global, Tara 2026-08-13)."""
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
