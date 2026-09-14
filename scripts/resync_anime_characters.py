#!/usr/bin/env python3
"""Pasa los personajes cacheados con la logica antigua (actores reales de TMDB) a
personajes de anime de AniList, para todos los titulos anime de la BBDD de una vez.

- Solo toca titulos que parezcan anime (genero Animation/Animación o alta manual) y que
  aun tengan reparto de TMDB (ningun personaje con imagen 'al_*' de AniList).
- Los personajes favoritos se conservan re-enlazandolos por nombre cuando coinciden.
- Va despacio a proposito (AniList limita a ~90 peticiones/min).

Uso: python3 scripts/resync_anime_characters.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import repo  # noqa: E402
from app.db import get_connection  # noqa: E402


def main():
    with get_connection() as conn:
        candidates = conn.execute(
            """SELECT DISTINCT titles.* FROM titles
               JOIN characters ON characters.title_id = titles.id
               WHERE NOT EXISTS (SELECT 1 FROM characters c2 WHERE c2.title_id = titles.id
                                 AND c2.profile_path LIKE '/static/profiles/al_%')"""
        ).fetchall()

    done = skipped = 0
    for title_row in candidates:
        if not repo._is_anime(title_row):
            skipped += 1
            continue
        try:
            with get_connection() as conn:
                # Nombres de los favoritos actuales para re-enlazarlos tras el resync.
                favs = conn.execute(
                    """SELECT favorite_characters.entry_id, characters.name,
                              characters.character_name, favorite_characters.position
                       FROM favorite_characters
                       JOIN characters ON characters.id = favorite_characters.character_id
                       WHERE characters.title_id = ?""",
                    (title_row["id"],),
                ).fetchall()
                repo.resync_characters(conn, title_row)
                for fav in favs:
                    match = conn.execute(
                        """SELECT id FROM characters WHERE title_id = ?
                           AND (name = ? OR name = ? OR character_name = ? OR character_name = ?)""",
                        (title_row["id"], fav["name"], fav["character_name"] or "",
                         fav["name"], fav["character_name"] or ""),
                    ).fetchone()
                    if match:
                        conn.execute(
                            """INSERT OR IGNORE INTO favorite_characters
                               (entry_id, character_id, position) VALUES (?, ?, ?)""",
                            (fav["entry_id"], match["id"], fav["position"]),
                        )
            done += 1
            print(f"  OK {title_row['title']}")
        except Exception as e:
            print(f"  fallo {title_row['title']}: {e}")
        time.sleep(0.8)

    print(f"\nResync de personajes: {done} titulos actualizados, {skipped} no-anime sin tocar")


if __name__ == "__main__":
    main()
