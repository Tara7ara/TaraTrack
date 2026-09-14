"""app.repo.waifus - extraido de repo.py en el split de modulos (ronda 2026-08-21).
Ver app/repo/__init__.py para el mapa completo de que vive en cada fichero -
el resto del proyecto sigue usando `from app import repo; repo.funcion(...)`
exactamente igual que antes, este split es puramente interno."""

from app.repo._shared import (
    get_setting,
    set_setting,
)
from app.repo.lists import _move_in_ranking


def _waifus_by_position(conn):
    """El orden MANUAL puro (position) - lo usan las flechas, ver _list_items_by_position."""
    return conn.execute(
        "SELECT id AS fav_id FROM favorite_characters ORDER BY position NULLS LAST, id"
    ).fetchall()




def list_waifus(conn):
    """Todos los personajes con estrella, de cualquier titulo. Orden mostrado: manual o
    por duelos segun el ajuste global 'waifus_order_mode' (ver record_duel)."""
    mode = get_setting(conn, "waifus_order_mode", "manual")
    order_sql = (
        "favorite_characters.elo DESC" if mode == "duelo"
        else "favorite_characters.position NULLS LAST, favorite_characters.id"
    )
    return conn.execute(
        f"""SELECT favorite_characters.id AS fav_id, characters.name, characters.character_name,
                  characters.profile_path, titles.title AS show_title, titles.tmdb_id, titles.type,
                  favorite_characters.elo AS elo, favorite_characters.rd AS rd
           FROM favorite_characters
           JOIN characters ON characters.id = favorite_characters.character_id
           JOIN entries ON entries.id = favorite_characters.entry_id
           JOIN titles ON titles.id = entries.title_id
           ORDER BY {order_sql}"""
    ).fetchall()




def move_waifu(conn, fav_id: int, direction: str):
    """Las flechas siempre mueven el orden manual, igual que en las listas (ver move_list_item)."""
    ids = [w["fav_id"] for w in _waifus_by_position(conn)]
    _move_in_ranking(conn, "favorite_characters", ids, fav_id, direction)




def get_waifus_by_ids(conn, ids: list[int]):
    if not ids:
        return []
    placeholders = ",".join("?" * len(ids))
    return conn.execute(
        f"""SELECT favorite_characters.id AS id,
                   COALESCE(characters.character_name, characters.name) AS title,
                   characters.profile_path AS image, titles.title AS subtitle,
                   titles.tmdb_id, titles.type
           FROM favorite_characters
           JOIN characters ON characters.id = favorite_characters.character_id
           JOIN entries ON entries.id = favorite_characters.entry_id
           JOIN titles ON titles.id = entries.title_id
           WHERE favorite_characters.id IN ({placeholders})""",
        ids,
    ).fetchall()




def set_waifus_order_mode(conn, mode: str):
    if mode in ("manual", "duelo"):
        set_setting(conn, "waifus_order_mode", mode)
