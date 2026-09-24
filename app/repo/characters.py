"""app.repo.characters - personajes por título y personajes favoritos. El resto del
proyecto usa `from app import repo; repo.funcion(...)`."""
import os
import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor

from app import anime, tmdb
from app.repo._shared import (
    PROFILES_DIR,
    _is_anime,
    get_setting,
    set_setting,
)

# Menos personajes que esto en un anime = el sync automatico no llego a traerlos
CHARACTERS_TOPUP_BELOW = 5

# Fila del reparto de TMDB (actor real): nombre del actor distinto del personaje. Las
# de AniList y las añadidas a mano guardan el mismo nombre en las dos columnas.
_TMDB_ACTOR_ROW_SQL = "name != COALESCE(character_name, '')"


def _drop_unfavorited(conn, title_id: int, only_actors: bool = False):
    """Borra personajes cacheados de un titulo menos los que esten en favoritos de
    alguien (waifus) - esos se quedan siempre."""
    extra = f" AND {_TMDB_ACTOR_ROW_SQL}" if only_actors else ""
    conn.execute(
        f"""DELETE FROM characters WHERE title_id = ?{extra}
            AND id NOT IN (SELECT character_id FROM favorite_characters)""",
        (title_id,),
    )


def _is_animation(title_row) -> bool:
    genres = title_row["genres"] or ""
    return "Animation" in genres or "Animación" in genres


def _title_variants(*titles):
    """Cada titulo tal cual y "limpio": NFKC (TMDB a veces trae radicales Kangxi como
    ⽼ en vez de 老), sin prefijo 劇場版/映画 ("pelicula") y sin el parentesis final,
    probando tambien lo que va dentro (TMDB mete ahi el titulo en ingles)."""
    for t in titles:
        if not t:
            continue
        yield t
        clean = unicodedata.normalize("NFKC", t)
        clean = re.sub(r"^(劇場版|映画)\s*", "", clean)
        inner = re.search(r"\(([^)]+)\)\s*$", clean)
        clean = re.sub(r"\s*\([^)]*\)\s*$", "", clean).strip()
        if clean:
            yield clean
        if inner:
            yield inner.group(1).strip()


def _anilist_characters(title_row) -> list[dict]:
    """Por anilist_id si el titulo ya esta emparejado; si no, por titulo original y luego
    el mostrado. Antes solo se buscaba por el mostrado, que desde el cambio a es-ES
    suele ser la traduccion ("Wistoria: varita y espada") y AniList no lo encuentra -
    asi acababan ~30 animes con los actores de voz de TMDB en vez de personajes."""
    keys = title_row.keys()
    anilist_id = title_row["anilist_id"] if "anilist_id" in keys else None
    if anilist_id:
        chars = anime.get_characters(anilist_id=anilist_id)
        if chars:
            return chars
    original = title_row["original_title"] if "original_title" in keys else None
    for candidate in dict.fromkeys(_title_variants(original, title_row["title"])):
        chars = anime.get_characters(candidate)
        if chars:
            return chars
    return []


def sync_characters(conn, title_row, force: bool = False):
    """Cachea los personajes de un titulo (no repite si ya existen). Para anime tira de
    AniList (imagen del personaje, no del actor real); si no, reparto de TMDB."""
    if title_row["tmdb_id"] < 0 and not title_row["title"]:
        return
    anime_title = _is_anime(title_row) and _is_animation(title_row)
    existing = conn.execute(
        "SELECT count(*) AS c FROM characters WHERE title_id = ?", (title_row["id"],)
    ).fetchone()["c"]
    topup_key = f"characters_topup:{title_row['id']}"
    if existing and not force:
        if anime_title and conn.execute(
            f"""SELECT 1 FROM characters WHERE title_id = ? AND {_TMDB_ACTOR_ROW_SQL}
                AND id NOT IN (SELECT character_id FROM favorite_characters) LIMIT 1""",
            (title_row["id"],),
        ).fetchone():
            # Anime cacheado con la logica vieja (reparto de TMDB = actores de voz): se
            # repara solo al abrir la ficha, sin tener que pulsar "Recargar".
            _drop_unfavorited(conn, title_row["id"], only_actors=True)
        elif not (anime_title and existing < CHARACTERS_TOPUP_BELOW
                  and not get_setting(conn, topup_key)):
            return
        # Con pocos personajes (casi siempre solo los añadidos a mano) se completan
        # sin borrar nada, una sola vez por título.

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
                for c in _anilist_characters(title_row)
            ]
        except Exception:
            cast = []
    # En animación, nunca el reparto de TMDB: son actores de voz. Mejor vacío; la
    # siguiente visita a la ficha lo reintenta.
    if not cast and title_row["tmdb_id"] > 0 and not anime_title:
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

    if cast and anime_title:
        set_setting(conn, topup_key, "1")

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
    """Casa los nombres de anime de un personaje externo (romaji/inglés) con los
    títulos de la biblioteca de este usuario: exacto primero y, si no, por prefijo
    ('Black Clover: ...' casa con 'Black Clover')."""
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
    """Personajes ya cacheados de títulos de tu biblioteca que casen con el nombre,
    para marcar estrellas en bloque desde /waifus."""
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
    """Borra y vuelve a cachear los personajes (boton "Recargar" del detalle). Los que
    estan en favoritos se conservan (antes se borraban con todo lo demas y "Recargar"
    se llevaba por delante las waifus de ese titulo); el INSERT OR IGNORE de
    sync_characters no los duplica si vuelven a venir."""
    _drop_unfavorited(conn, title_row["id"])
    sync_characters(conn, title_row, force=True)




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
