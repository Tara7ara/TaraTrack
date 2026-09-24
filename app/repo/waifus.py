"""app.repo.waifus - personajes favoritos y su orden. El resto del proyecto usa
`from app import repo; repo.funcion(...)`."""

from app.repo._shared import (
    get_setting,
    set_setting,
)
from app.repo.lists import _move_in_ranking


def _waifus_by_position(conn, user_id):
    """Orden manual (position) de este usuario, el que usan las flechas.
    favorite_characters no tiene user_id propio: la propiedad sale del entry_id."""
    return conn.execute(
        """SELECT favorite_characters.id AS fav_id FROM favorite_characters
           JOIN entries ON entries.id = favorite_characters.entry_id
           WHERE entries.user_id = ?
           ORDER BY favorite_characters.position NULLS LAST, favorite_characters.id""",
        (user_id,),
    ).fetchall()




def list_waifus(conn, user_id):
    """Personajes con estrella de este usuario, en orden manual o por duelos según
    'waifus_order_mode:<user_id>'."""
    mode = get_setting(conn, f"waifus_order_mode:{user_id}", "manual")
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
           WHERE entries.user_id = ?
           ORDER BY {order_sql}""",
        (user_id,),
    ).fetchall()




def move_waifu(conn, fav_id: int, direction: str, user_id: int):
    """Las flechas siempre mueven el orden manual, igual que en las listas (ver move_list_item)."""
    ids = [w["fav_id"] for w in _waifus_by_position(conn, user_id)]
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




def set_waifus_order_mode(conn, user_id, mode: str):
    if mode in ("manual", "duelo"):
        set_setting(conn, f"waifus_order_mode:{user_id}", mode)
