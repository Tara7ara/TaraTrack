#!/usr/bin/env python3
"""Vuelve a pedir a AniList las recomendaciones de la comunidad de todos los titulos
que ya tienen anilist_id, con el tope actual (anime.ANILIST_RECS_PER_TITLE), y
reescribe anilist_cross_rec_ids y anilist_cross_rec_votes. Solo toca esas columnas: no repite la busqueda por
texto, asi que ningun titulo puede quedar emparejado con otro anime distinto.

Hace falta cuando cambia ANILIST_RECS_PER_TITLE; el backfill normal solo procesa
titulos a los que les falta algo. Va en lotes pequeños con pausa (AniList limita a
~90 peticiones/min).

Uso: python3 scripts/refresh_anilist_recs.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import anime  # noqa: E402
from app.db import get_connection  # noqa: E402

CHUNK = 10


def main():
    with get_connection() as conn:
        ids = [row["anilist_id"] for row in conn.execute(
            "SELECT DISTINCT anilist_id FROM titles WHERE anilist_id IS NOT NULL ORDER BY anilist_id"
        )]
    print(f"{len(ids)} titulos con anilist_id")
    updated = 0
    for start in range(0, len(ids), CHUNK):
        recs = anime.get_anilist_recommendations(ids[start:start + CHUNK], chunk_size=CHUNK)
        with get_connection() as conn:
            for anilist_id, (cross, votes) in recs.items():
                if cross:
                    conn.execute(
                        "UPDATE titles SET anilist_cross_rec_ids = ?, anilist_cross_rec_votes = ? WHERE anilist_id = ?",
                        (",".join(str(i) for i in cross), ",".join(str(v) for v in votes), anilist_id),
                    )
                    updated += 1
        print(f"  {min(start + CHUNK, len(ids))}/{len(ids)}", flush=True)
    print(f"actualizados {updated}")


if __name__ == "__main__":
    main()
