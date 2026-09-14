"""app.repo.lists - extraido de repo.py en el split de modulos (ronda 2026-08-21).
Ver app/repo/__init__.py para el mapa completo de que vive en cada fichero -
el resto del proyecto sigue usando `from app import repo; repo.funcion(...)`
exactamente igual que antes, este split es puramente interno."""





def get_default_list(conn):
    return conn.execute("SELECT * FROM lists WHERE is_default = 1").fetchone()




def is_entry_favorite(conn, entry_id: int) -> bool:
    default_list = get_default_list(conn)
    return bool(conn.execute(
        "SELECT 1 FROM list_items WHERE list_id = ? AND entry_id = ?", (default_list["id"], entry_id)
    ).fetchone())




def list_lists(conn, preview_count=10):
    """Listas con sus primeras portadas (en su orden manual) para la vista previa."""
    lists = conn.execute(
        """SELECT lists.*, (SELECT count(*) FROM list_items WHERE list_items.list_id = lists.id) AS total
           FROM lists ORDER BY is_default DESC, name"""
    ).fetchall()
    return [
        {
            **dict(l),
            "posters": [r["poster_path"] for r in list_items_in_list(conn, l["id"])[:preview_count]],
        }
        for l in lists
    ]




def create_list(conn, name: str):
    name = name.strip()
    existing = conn.execute("SELECT * FROM lists WHERE name = ?", (name,)).fetchone()
    if existing:
        return existing
    conn.execute("INSERT INTO lists (name) VALUES (?)", (name,))
    return conn.execute("SELECT * FROM lists WHERE name = ?", (name,)).fetchone()




def add_entry_to_list(conn, list_id: int, entry_id: int):
    """Añade sin quitar (a diferencia del toggle): para el buscador de la propia lista,
    donde darle dos veces a 'Añadir' no debe sacar el titulo de la lista."""
    conn.execute(
        "INSERT OR IGNORE INTO list_items (list_id, entry_id) VALUES (?, ?)", (list_id, entry_id)
    )




def toggle_list_item(conn, list_id: int, entry_id: int) -> bool:
    """Anade/quita una entrada de una lista. Devuelve True si quedo anadida, False si se quito."""
    existing = conn.execute(
        "SELECT id FROM list_items WHERE list_id = ? AND entry_id = ?", (list_id, entry_id)
    ).fetchone()
    if existing:
        conn.execute("DELETE FROM list_items WHERE id = ?", (existing["id"],))
        return False
    conn.execute("INSERT INTO list_items (list_id, entry_id) VALUES (?, ?)", (list_id, entry_id))
    return True




def _list_items_by_position(conn, list_id: int):
    """El orden MANUAL puro (position), sin importar que se este enseñando el de
    duelos - lo usan las flechas para no corromper el orden manual mientras se ve
    ordenado por Elo."""
    return conn.execute(
        "SELECT id AS item_id FROM list_items WHERE list_id = ? ORDER BY position NULLS LAST, added_at DESC",
        (list_id,),
    ).fetchall()




def list_items_in_list(conn, list_id: int):
    """Orden mostrado: manual (position) o por duelos (Elo), segun lists.order_mode -
    los dos ordenes conviven aparte (ver record_duel), este solo decide cual se enseña."""
    list_row = conn.execute("SELECT order_mode FROM lists WHERE id = ?", (list_id,)).fetchone()
    order_sql = (
        "list_items.elo DESC" if list_row and list_row["order_mode"] == "duelo"
        else "list_items.position NULLS LAST, list_items.added_at DESC"
    )
    return conn.execute(
        f"""SELECT entries.*, titles.title, titles.year, titles.poster_path, titles.type, titles.tmdb_id,
                  list_items.id AS item_id, list_items.elo AS elo, list_items.rd AS rd
           FROM list_items
           JOIN entries ON entries.id = list_items.entry_id
           JOIN titles ON titles.id = entries.title_id
           WHERE list_items.list_id = ?
           ORDER BY {order_sql}""",
        (list_id,),
    ).fetchall()




def _move_in_ranking(conn, table: str, ordered_ids: list[int], target_id: int, direction: str):
    """Sube o baja una fila intercambiandola con su vecina, en cualquier tabla con columna
    position (list_items, favorite_characters). Normaliza primero position a 0..n-1 para
    que las filas antiguas sin position tambien se muevan."""
    for pos, row_id in enumerate(ordered_ids):
        conn.execute(f"UPDATE {table} SET position = ? WHERE id = ?", (pos, row_id))
    index = next((i for i, row_id in enumerate(ordered_ids) if row_id == target_id), None)
    if index is None:
        return
    other = index - 1 if direction == "subir" else index + 1
    if not (0 <= other < len(ordered_ids)):
        return
    conn.execute(f"UPDATE {table} SET position = ? WHERE id = ?", (other, target_id))
    conn.execute(f"UPDATE {table} SET position = ? WHERE id = ?", (index, ordered_ids[other]))




def move_list_item(conn, list_id: int, item_id: int, direction: str):
    """Las flechas SIEMPRE reordenan el orden manual (position), aunque ahora mismo se
    este enseñando el orden por duelos - si no, mover algo en modo duelo reescribiria
    el orden manual con el de Elo sin que Tara lo pidiera."""
    ids = [item["item_id"] for item in _list_items_by_position(conn, list_id)]
    _move_in_ranking(conn, "list_items", ids, item_id, direction)




def get_list_items_by_ids(conn, list_id: int, ids: list[int]):
    if not ids:
        return []
    placeholders = ",".join("?" * len(ids))
    return conn.execute(
        f"""SELECT list_items.id AS id, titles.title AS title, titles.poster_path AS image,
                   titles.year AS subtitle, titles.tmdb_id, titles.type
           FROM list_items JOIN entries ON entries.id = list_items.entry_id
           JOIN titles ON titles.id = entries.title_id
           WHERE list_items.list_id = ? AND list_items.id IN ({placeholders})""",
        [list_id, *ids],
    ).fetchall()




def rename_list(conn, list_id: int, name: str):
    name = name.strip()
    if name:
        conn.execute("UPDATE lists SET name = ? WHERE id = ? AND is_default = 0", (name, list_id))




def delete_list(conn, list_id: int) -> bool:
    """Borra una lista y sus items. Favoritos (is_default) no se puede borrar."""
    row = conn.execute("SELECT is_default FROM lists WHERE id = ?", (list_id,)).fetchone()
    if not row or row["is_default"]:
        return False
    conn.execute("DELETE FROM list_items WHERE list_id = ?", (list_id,))
    conn.execute("DELETE FROM lists WHERE id = ?", (list_id,))
    return True




def set_list_order_mode(conn, list_id: int, mode: str):
    if mode in ("manual", "duelo"):
        conn.execute("UPDATE lists SET order_mode = ? WHERE id = ?", (mode, list_id))
