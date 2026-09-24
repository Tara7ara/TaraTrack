"""app.repo.season_cache - caché del calendario de temporada (AniList) y ajustes del
calendario. El resto del proyecto usa `from app import repo; repo.funcion(...)`."""
import json

from app.matching import _strip_season_suffix
from app.repo._shared import get_setting, set_setting


def get_cached_season(conn, season: str, year: int):
    """Datos cacheados de una temporada/año y su antigüedad en horas; (None, None) si
    nunca se cacheó. Se guarda como JSON de una pieza porque es caché desechable. Así
    la página no depende de que AniList responda en ese momento."""
    row = conn.execute(
        "SELECT data, computed_at FROM season_cache WHERE season = ? AND year = ?", (season, year)
    ).fetchone()
    if not row:
        return None, None
    age_hours = conn.execute(
        "SELECT (julianday('now') - julianday(?)) * 24 AS h", (row["computed_at"],)
    ).fetchone()["h"]
    return json.loads(row["data"]), age_hours




def save_season_cache(conn, season: str, year: int, items: list[dict]):
    conn.execute(
        """INSERT INTO season_cache (season, year, data, computed_at)
           VALUES (?, ?, ?, datetime('now'))
           ON CONFLICT(season, year) DO UPDATE SET data = excluded.data, computed_at = excluded.computed_at""",
        (season, year, json.dumps(items)),
    )


def get_weekday_overrides(conn) -> dict:
    """Todos los dias de emision corregidos a mano, {anilist_id: weekday} - se piden
    de golpe (no uno a uno) porque calendario_anual aplica esto sobre hasta 150 items
    de la temporada en cada carga."""
    rows = conn.execute("SELECT anilist_id, weekday FROM weekday_overrides").fetchall()
    return {r["anilist_id"]: r["weekday"] for r in rows}


def get_weekday_override(conn, anilist_id: int):
    """Un solo dia corregido (o None) - para la ficha tecnica, donde solo hace falta
    el de este titulo, no la tabla entera."""
    row = conn.execute(
        "SELECT weekday FROM weekday_overrides WHERE anilist_id = ?", (anilist_id,)
    ).fetchone()
    return row["weekday"] if row else None


def set_weekday_override(conn, anilist_id: int, weekday: int):
    conn.execute(
        """INSERT INTO weekday_overrides (anilist_id, weekday) VALUES (?, ?)
           ON CONFLICT(anilist_id) DO UPDATE SET weekday = excluded.weekday""",
        (anilist_id, weekday),
    )


def clear_weekday_override(conn, anilist_id: int):
    conn.execute("DELETE FROM weekday_overrides WHERE anilist_id = ?", (anilist_id,))


def get_show_anime_calendar(conn, user_id: int, default: bool) -> bool:
    """Si el enlace al calendario de temporada aparece en /calendario. `default`
    (normalmente repo.user_has_anime) solo vale hasta que el usuario lo cambie."""
    value = get_setting(conn, f"show_anime_calendar:{user_id}")
    if value is None:
        return default
    return value == "1"


def set_show_anime_calendar(conn, user_id: int, show: bool):
    set_setting(conn, f"show_anime_calendar:{user_id}", "1" if show else "0")



def link_calendar_card(conn, anilist_id: int, tmdb_id: int, media_type: str):
    """Recuerda a que titulo de TMDB se resolvio una tarjeta del calendario (ver
    calendar_links en db.py). Solo si el titulo ya esta cacheado - abrir la ficha o
    añadirlo lo cachea siempre."""
    row = conn.execute(
        "SELECT id FROM titles WHERE tmdb_id = ? AND type = ?", (tmdb_id, media_type)
    ).fetchone()
    if row and anilist_id:
        conn.execute(
            "INSERT INTO calendar_links (anilist_id, title_id) VALUES (?, ?) "
            "ON CONFLICT(anilist_id) DO UPDATE SET title_id = excluded.title_id",
            (anilist_id, row["id"]),
        )


def _norm_title(t) -> str:
    return " ".join((t or "").casefold().split())


def library_status_for_cards(conn, user_id: int, items: list[dict]) -> dict:
    """{anilist_id: 'pending'|'watched'} de las tarjetas de temporada que ya están en
    la biblioteca de este usuario. Tres vías, de más a menos fiable: calendar_links,
    el anilist_id del título, o el título/romaji exacto contra el título de TMDB."""
    rows = conn.execute(
        """SELECT entries.status, titles.id AS title_id, titles.anilist_id,
                  titles.title, titles.original_title,
                  titles.anilist_title_romaji, titles.anilist_title_english
           FROM entries JOIN titles ON titles.id = entries.title_id
           WHERE entries.user_id = ?""",
        (user_id,),
    ).fetchall()
    by_title_id = {r["title_id"]: r["status"] for r in rows}
    by_anilist = {r["anilist_id"]: r["status"] for r in rows if r["anilist_id"]}
    by_name = {}
    for r in rows:
        for t in (r["title"], r["original_title"], r["anilist_title_romaji"], r["anilist_title_english"]):
            if t:
                by_name.setdefault(_norm_title(t), r["status"])
    links = {
        r["anilist_id"]: r["title_id"]
        for r in conn.execute("SELECT anilist_id, title_id FROM calendar_links")
    }
    out = {}
    for item in items:
        aid = item.get("anilist_id")
        status = by_title_id.get(links.get(aid)) or by_anilist.get(aid)
        if not status:
            # "Blue Box Season 2" es el mismo show que "Blue Box" (TMDB no separa
            # temporadas), y la ficha puede llamarse "La caja azul": se cruza tambien
            # sin el sufijo y contra los titulos de AniList guardados en el titulo.
            names = [item.get("title"), item.get("title_romaji")]
            names += [_strip_season_suffix(t) for t in names if t]
            for t in names:
                status = by_name.get(_norm_title(t)) if t else None
                if status:
                    break
        if status:
            out[aid] = status
    return out
