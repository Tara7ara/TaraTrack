"""app.repo.characters - extraido de repo.py en el split de modulos (ronda 2026-08-21).
Ver app/repo/__init__.py para el mapa completo de que vive en cada fichero -
el resto del proyecto sigue usando `from app import repo; repo.funcion(...)`
exactamente igual que antes, este split es puramente interno."""
import os
from concurrent.futures import ThreadPoolExecutor

from app import anime, tmdb
from app.repo._shared import (
    PROFILES_DIR,
    _is_anime,
)


def sync_characters(conn, title_row):
    """Cachea los personajes de un titulo (no repite si ya existen). Para anime tira de
    AniList (imagen del personaje, no del actor real); si no, reparto de TMDB."""
    if title_row["tmdb_id"] < 0 and not title_row["title"]:
        return
    existing = conn.execute(
        "SELECT count(*) AS c FROM characters WHERE title_id = ?", (title_row["id"],)
    ).fetchone()["c"]
    if existing:
        return

    os.makedirs(PROFILES_DIR, exist_ok=True)
    cast = []
    if _is_anime(title_row):
        try:
            cast = [
                {
                    "tmdb_person_id": c["id"],
                    "name": c["name"],
                    "character_name": c["name"],
                    "profile_path": c["image_url"],
                    "file_prefix": "al_",
                }
                for c in anime.get_characters(title_row["title"])
            ]
        except Exception:
            cast = []
    if not cast and title_row["tmdb_id"] > 0:
        cast = tmdb.get_credits(title_row["tmdb_id"], title_row["type"])

    def _download(person):
        if not person.get("profile_path"):
            return None
        filename = f"{person.get('file_prefix', '')}{person['tmdb_person_id']}.jpg"
        try:
            if tmdb.download_poster(person["profile_path"], os.path.join(PROFILES_DIR, filename)):
                return f"/static/profiles/{filename}"
        except Exception:
            pass
        return None

    # Hasta 15-25 fotos por titulo, una a una tardaba segundos en la primera visita
    # a la ficha - descargarlas en paralelo (I/O puro, sin tocar la BBDD en los hilos).
    with ThreadPoolExecutor(max_workers=8) as pool:
        profile_paths = list(pool.map(_download, cast))

    for person, profile_path in zip(cast, profile_paths):
        conn.execute(
            """INSERT OR IGNORE INTO characters (title_id, tmdb_person_id, name, character_name, profile_path)
               VALUES (?, ?, ?, ?, ?)""",
            (title_row["id"], person["tmdb_person_id"], person["name"], person["character_name"], profile_path),
        )




def add_character_manual(conn, title_row, char_id: int, name: str, image_url: str | None):
    """Añade un personaje concreto (buscado por nombre) a un titulo, para los que no
    entran en el top del sync automatico (p.ej. Nero en Black Clover, que tiene 500).
    id positivo = AniList (prefijo al_), id negativo = MyAnimeList via Jikan (mal_,
    en negativo para que nunca choque con un id de AniList en la misma tabla)."""
    profile_path = None
    if image_url:
        os.makedirs(PROFILES_DIR, exist_ok=True)
        prefix = "al_" if char_id > 0 else "mal_"
        filename = f"{prefix}{abs(char_id)}.jpg"
        try:
            if tmdb.download_poster(image_url, os.path.join(PROFILES_DIR, filename)):
                profile_path = f"/static/profiles/{filename}"
        except Exception:
            pass
    conn.execute(
        """INSERT OR IGNORE INTO characters (title_id, tmdb_person_id, name, character_name, profile_path)
           VALUES (?, ?, ?, ?, ?)""",
        (title_row["id"], char_id, name, name, profile_path),
    )




def match_library_title(conn, names: list[str], user_id: int):
    """Casa los nombres de anime de un personaje externo (romaji/ingles) contra los
    titulos de la biblioteca DE ESTE USUARIO. Exacto primero; si no, por prefijo (los
    subtitulos de temporada tipo 'Black Clover: ...' siguen casando con 'Black Clover').

    Bug real (AGY, 2026-09-18): sin filtrar por user_id, el JOIN contra entries podia
    devolver el entry_id de OTRO usuario que si tuviera el titulo - el boton "Añadir a
    waifus" salia aunque el titulo no estuviera en TU biblioteca."""
    for name in names:
        row = conn.execute(
            """SELECT titles.id AS title_id, titles.title, entries.id AS entry_id
               FROM titles JOIN entries ON entries.title_id = titles.id
               WHERE lower(titles.title) = lower(?) AND entries.user_id = ?""",
            (name, user_id),
        ).fetchone()
        if row:
            return row
    for name in names:
        row = conn.execute(
            """SELECT titles.id AS title_id, titles.title, entries.id AS entry_id
               FROM titles JOIN entries ON entries.title_id = titles.id
               WHERE (lower(?) LIKE lower(titles.title) || '%'
                  OR lower(titles.title) LIKE lower(?) || '%') AND entries.user_id = ?
               ORDER BY length(titles.title) DESC LIMIT 1""",
            (name, name, user_id),
        ).fetchone()
        if row:
            return row
    return None




def search_characters_local(conn, query: str, user_id: int, limit=24):
    """Personajes ya cacheados de titulos EN TU biblioteca que casen con el nombre -
    para marcar estrellas en bloque desde /waifus sin ir ficha por ficha.

    Bug real (AGY, 2026-09-18): sin filtrar por user_id, el JOIN contra entries podia
    devolver el entry_id de OTRO usuario - pulsar la estrella entonces fallaba con 404
    (el toggle si comprueba propiedad) en vez de encontrar tu propia entry, o
    directamente no encontrarla si tu no tenias el titulo."""
    like = f"%{query.strip()}%"
    return conn.execute(
        """SELECT characters.*, entries.id AS entry_id, titles.title AS show_title,
                  EXISTS(SELECT 1 FROM favorite_characters
                         WHERE favorite_characters.character_id = characters.id
                           AND favorite_characters.entry_id = entries.id) AS is_favorite
           FROM characters
           JOIN titles ON titles.id = characters.title_id
           JOIN entries ON entries.title_id = titles.id
           WHERE (characters.name LIKE ? OR characters.character_name LIKE ?) AND entries.user_id = ?
           ORDER BY characters.name LIMIT ?""",
        (like, like, user_id, limit),
    ).fetchall()




def resync_characters(conn, title_row):
    """Borra y vuelve a cachear los personajes (boton del detalle, para pasar de actores
    reales a personajes de anime en titulos cacheados con la logica antigua)."""
    conn.execute(
        """DELETE FROM favorite_characters WHERE character_id IN
           (SELECT id FROM characters WHERE title_id = ?)""",
        (title_row["id"],),
    )
    conn.execute("DELETE FROM characters WHERE title_id = ?", (title_row["id"],))
    sync_characters(conn, title_row)




def list_characters(conn, title_id: int, entry_id: int | None):
    return conn.execute(
        """SELECT characters.*,
                  EXISTS(SELECT 1 FROM favorite_characters
                         WHERE favorite_characters.character_id = characters.id
                           AND favorite_characters.entry_id = ?) AS is_favorite
           FROM characters WHERE title_id = ? ORDER BY id""",
        (entry_id or -1, title_id),
    ).fetchall()




def toggle_favorite_character(conn, entry_id: int, character_id: int) -> bool:
    existing = conn.execute(
        "SELECT id FROM favorite_characters WHERE entry_id = ? AND character_id = ?",
        (entry_id, character_id),
    ).fetchone()
    if existing:
        conn.execute("DELETE FROM favorite_characters WHERE id = ?", (existing["id"],))
        return False
    conn.execute(
        "INSERT INTO favorite_characters (entry_id, character_id) VALUES (?, ?)", (entry_id, character_id)
    )
    return True
