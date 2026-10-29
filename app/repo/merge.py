"""app.repo.merge - fichas que TMDB borra (404) estando en la biblioteca.

TMDB a veces crea una serie aparte para una temporada nueva y luego la borra y la
mete como temporada de la serie original. Sin esto, la ficha huérfana se quedaba
para siempre y la misma serie salía dos veces con portadas distintas.

merge_gone_title fusiona la huérfana en la ficha que la sustituye: mueve entries,
notas por temporada, episodios vistos/comentados y personajes, emparejando cada
temporada con la de la ficha nueva que empieza el mismo día. Si algún dato de un
usuario no casa, no fusiona nada: marca la ficha como tmdb_gone y la ficha muestra
un aviso."""
import logging
import re

import httpx

from app import tmdb
from app.matching import _plausible_match, _strip_season_suffix
from app.repo._shared import forget_duels
from app.repo.titles import _purge_title, ensure_title, sync_episodes


def is_tmdb_gone_error(exc: Exception) -> bool:
    return isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 404


def _base_names(row) -> list[str]:
    """Nombres con los que buscar la ficha que sustituye a la huérfana: sin sufijo de
    temporada ("Season 2") ni número suelto al final."""
    names = []
    for name in (row["original_title"], row["title"]):
        if not name:
            continue
        for candidate in (_strip_season_suffix(name), re.sub(r"\s+\d+$", "", name).strip()):
            if candidate and candidate != name and candidate not in names:
                names.append(candidate)
    return names


def _candidate_targets(conn, orphan):
    """Fichas candidatas a sustituir a la huérfana, de más a menos fiable: misma
    anilist_id ya en la biblioteca y, si no, búsqueda en TMDB por el nombre base."""
    seen = set()
    if orphan["anilist_id"]:
        for row in conn.execute(
            """SELECT * FROM titles WHERE anilist_id = ? AND type = ? AND id != ?
                 AND tmdb_id > 0 AND tmdb_gone = 0""",
            (orphan["anilist_id"], orphan["type"], orphan["id"]),
        ).fetchall():
            seen.add(row["tmdb_id"])
            yield row
    for name in _base_names(orphan):
        try:
            results = tmdb.search(name)
        except Exception:
            continue
        for r in results[:3]:
            if r["type"] != orphan["type"] or r["tmdb_id"] in seen or r["tmdb_id"] == orphan["tmdb_id"]:
                continue
            if not _plausible_match(name, r["title"]):
                continue
            seen.add(r["tmdb_id"])
            yield ensure_title(conn, r["tmdb_id"], r["type"])


def _season_starts(conn, title_id: int) -> dict[int, str]:
    return {
        r["season_number"]: r["first_air"]
        for r in conn.execute(
            """SELECT season_number, MIN(air_date) AS first_air FROM episodes
               WHERE title_id = ? AND season_number > 0 AND air_date IS NOT NULL
               GROUP BY season_number""",
            (title_id,),
        ).fetchall()
    }


def _plan_merge(conn, orphan_id: int, target_id: int):
    """Emparejamiento temporada->temporada y episodio->episodio, o None si algún dato
    de usuario de la huérfana (episodio visto/comentado, nota de temporada) no tiene
    sitio en la ficha nueva."""
    target_by_start = {start: s for s, start in _season_starts(conn, target_id).items()}
    season_map = {
        s: target_by_start[start]
        for s, start in _season_starts(conn, orphan_id).items()
        if start in target_by_start
    }
    target_eps = {
        (r["season_number"], r["episode_number"]): r["id"]
        for r in conn.execute(
            "SELECT id, season_number, episode_number FROM episodes WHERE title_id = ?", (target_id,)
        ).fetchall()
    }
    episode_map = {}
    for ep in conn.execute(
        "SELECT id, season_number, episode_number FROM episodes WHERE title_id = ?", (orphan_id,)
    ).fetchall():
        target_season = season_map.get(ep["season_number"])
        if target_season is not None and (target_season, ep["episode_number"]) in target_eps:
            episode_map[ep["id"]] = target_eps[(target_season, ep["episode_number"])]

    with_user_data = conn.execute(
        """SELECT id FROM episodes WHERE title_id = ? AND (
               EXISTS(SELECT 1 FROM episode_watches WHERE episode_id = episodes.id)
               OR EXISTS(SELECT 1 FROM episode_user_state WHERE episode_id = episodes.id)
               OR EXISTS(SELECT 1 FROM episode_comments WHERE episode_id = episodes.id))""",
        (orphan_id,),
    ).fetchall()
    if any(r["id"] not in episode_map for r in with_user_data):
        return None
    rated_seasons = conn.execute(
        """SELECT DISTINCT season_ratings.season_number FROM season_ratings
           JOIN entries ON entries.id = season_ratings.entry_id WHERE entries.title_id = ?""",
        (orphan_id,),
    ).fetchall()
    if any(r["season_number"] not in season_map for r in rated_seasons):
        return None
    return season_map, episode_map


def _move_entry(conn, entry, target_id: int, season_map: dict):
    """Pasa una entry de la huérfana a la ficha nueva. Si el usuario ya tenía entry en
    la nueva, se queda esa y solo se traen los datos que le falten."""
    existing = conn.execute(
        "SELECT * FROM entries WHERE title_id = ? AND user_id = ?", (target_id, entry["user_id"])
    ).fetchone()
    ratings = conn.execute("SELECT * FROM season_ratings WHERE entry_id = ?", (entry["id"],)).fetchall()
    if existing is None:
        conn.execute("UPDATE entries SET title_id = ? WHERE id = ?", (target_id, entry["id"]))
        # Dos pasadas para no chocar con UNIQUE(entry_id, season_number) al renumerar.
        for r in ratings:
            conn.execute("UPDATE season_ratings SET season_number = ? WHERE id = ?", (-1 - r["id"], r["id"]))
        for r in ratings:
            conn.execute(
                "UPDATE season_ratings SET season_number = ? WHERE id = ?", (season_map[r["season_number"]], r["id"])
            )
        return
    keep = existing["id"]
    for r in ratings:
        conn.execute(
            "UPDATE OR IGNORE season_ratings SET entry_id = ?, season_number = ? WHERE id = ?",
            (keep, season_map[r["season_number"]], r["id"]),
        )
    conn.execute("DELETE FROM season_ratings WHERE entry_id = ?", (entry["id"],))
    conn.execute("UPDATE OR IGNORE list_items SET entry_id = ? WHERE entry_id = ?", (keep, entry["id"]))
    forget_duels(conn, "list_items", "SELECT id FROM list_items WHERE entry_id = ?", (entry["id"],))
    conn.execute("DELETE FROM list_items WHERE entry_id = ?", (entry["id"],))
    conn.execute("UPDATE OR IGNORE favorite_characters SET entry_id = ? WHERE entry_id = ?", (keep, entry["id"]))
    forget_duels(conn, "favorite_characters", "SELECT id FROM favorite_characters WHERE entry_id = ?", (entry["id"],))
    conn.execute("DELETE FROM favorite_characters WHERE entry_id = ?", (entry["id"],))
    conn.execute("UPDATE watch_sessions SET entry_id = ? WHERE entry_id = ?", (keep, entry["id"]))
    conn.execute("UPDATE rating_history SET entry_id = ? WHERE entry_id = ?", (keep, entry["id"]))
    conn.execute(
        """UPDATE entries SET rating = COALESCE(rating, ?), comment = COALESCE(comment, ?),
                  status = CASE WHEN ? = 'watched' THEN 'watched' ELSE status END,
                  watched_at = COALESCE(watched_at, ?) WHERE id = ?""",
        (entry["rating"], entry["comment"], entry["status"], entry["watched_at"], keep),
    )
    forget_duels(conn, "entries", "?", (entry["id"],))
    conn.execute("DELETE FROM entries WHERE id = ?", (entry["id"],))


def _move_characters(conn, orphan_id: int, target_id: int):
    """Personajes de la huérfana: si la ficha nueva ya tiene al mismo, los favoritos
    pasan a ese; si no, el personaje entero se cambia de ficha."""
    for char in conn.execute("SELECT * FROM characters WHERE title_id = ?", (orphan_id,)).fetchall():
        twin = conn.execute(
            "SELECT id FROM characters WHERE title_id = ? AND tmdb_person_id = ?", (target_id, char["tmdb_person_id"])
        ).fetchone()
        if twin is None:
            conn.execute("UPDATE characters SET title_id = ? WHERE id = ?", (target_id, char["id"]))
            continue
        conn.execute(
            "UPDATE OR IGNORE favorite_characters SET character_id = ? WHERE character_id = ?", (twin["id"], char["id"])
        )
        forget_duels(
            conn, "favorite_characters", "SELECT id FROM favorite_characters WHERE character_id = ?", (char["id"],)
        )
        conn.execute("DELETE FROM favorite_characters WHERE character_id = ?", (char["id"],))


def merge_gone_title(conn, orphan_id: int, target_id: int) -> bool:
    """Fusiona la huérfana en target_id (con sus episodios ya sincronizados). False sin
    tocar nada si el emparejamiento no cubre todos los datos de usuario."""
    plan = _plan_merge(conn, orphan_id, target_id)
    if plan is None:
        return False
    season_map, episode_map = plan
    for entry in conn.execute("SELECT * FROM entries WHERE title_id = ?", (orphan_id,)).fetchall():
        _move_entry(conn, entry, target_id, season_map)
    for old_ep, new_ep in episode_map.items():
        conn.execute("UPDATE episode_watches SET episode_id = ? WHERE episode_id = ?", (new_ep, old_ep))
        conn.execute("UPDATE OR IGNORE episode_user_state SET episode_id = ? WHERE episode_id = ?", (new_ep, old_ep))
        conn.execute("UPDATE episode_comments SET episode_id = ? WHERE episode_id = ?", (new_ep, old_ep))
        conn.execute(
            """UPDATE episodes SET watched_at = (SELECT MAX(watched_at) FROM episode_watches
                                                 WHERE episode_id = episodes.id) WHERE id = ?""",
            (new_ep,),
        )
    _move_characters(conn, orphan_id, target_id)
    conn.execute("UPDATE OR IGNORE calendar_links SET title_id = ? WHERE title_id = ?", (target_id, orphan_id))
    conn.execute("UPDATE OR IGNORE user_calendar_links SET title_id = ? WHERE title_id = ?", (target_id, orphan_id))
    _purge_title(conn, orphan_id)
    return True


def handle_gone_title(conn, orphan) -> int | None:
    """La sync ha recibido un 404 de TMDB para `orphan`: busca la ficha que la sustituye
    y fusiona. Devuelve el id de la ficha nueva, o None si se queda marcada tmdb_gone."""
    for target in _candidate_targets(conn, orphan):
        try:
            sync_episodes(conn, target)
        except Exception:
            continue
        if merge_gone_title(conn, orphan["id"], target["id"]):
            logging.info(
                "merge: %s (tmdb_id=%s) fusionada en %s (tmdb_id=%s)",
                orphan["title"], orphan["tmdb_id"], target["title"], target["tmdb_id"],
            )
            return target["id"]
    conn.execute("UPDATE titles SET tmdb_gone = 1 WHERE id = ?", (orphan["id"],))
    logging.warning("merge: %s (tmdb_id=%s) ya no existe en TMDB y no hay con que fusionarla",
                    orphan["title"], orphan["tmdb_id"])
    return None
