"""app.repo.season_cache - extraido de repo.py en el split de modulos (ronda 2026-08-21).
Ver app/repo/__init__.py para el mapa completo de que vive en cada fichero -
el resto del proyecto sigue usando `from app import repo; repo.funcion(...)`
exactamente igual que antes, este split es puramente interno."""
import json


def get_cached_season(conn, season: str, year: int):
    """Datos cacheados de una temporada/año del calendario (AniList), y cuantas horas
    tienen - (None, None) si nunca se cacheo esta temporada/año todavia. Guardado como
    JSON de golpe (no una tabla relacional por item) porque es cache desechable, no
    algo sobre lo que hacer SQL - y los tags llevan tuplas (nombre, rank) que JSON ya
    serializa bien como listas de 2, sin necesitar columnas propias.
    Pedido por Tara (2026-08-13): "el calendario de anilist se cae mucho" - antes cada
    visita hacia 2-3 peticiones en vivo a AniList, un solo hipo tumbaba la pagina
    entera. Con esto la pagina no depende de que AniList responda AHORA MISMO."""
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
