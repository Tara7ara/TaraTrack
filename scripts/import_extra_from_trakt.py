#!/usr/bin/env python3
"""Importa del export de Trakt lo que seed_from_trakt e import_history no cubrian:

- lists-favorites.json  -> lista Favoritos con el rank de Trakt como position, y las
                           notas personales ("BESTO SHONEN FOR EVER") a entries.comment
                           si no habia comentario ya.
- ratings-episodes.json -> episodios puntuados en Trakt pasan a favoritos (is_favorite).
- watched-shows/movies  -> repara fechas de visionado pisadas: si al puntuar desde la app
                           se machaco la fecha real con la del dia del despliegue, se
                           restaura la last_watched_at del export.

Idempotente: repetirlo no duplica ni pisa nada tuyo.

Uso: TRAKT_EXPORT_DIR=/ruta/json python3 scripts/import_extra_from_trakt.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import repo  # noqa: E402
from app.db import get_connection  # noqa: E402

SOURCE_DIR = os.environ.get("TRAKT_EXPORT_DIR", "/tmp/trakt-import")
# Fechas de visionado >= este dia se consideran "pisadas por la app" y se restauran del export.
DEPLOY_DATE = "2026-08-11"


def load(fname):
    path = os.path.join(SOURCE_DIR, fname)
    if not os.path.exists(path):
        print(f"  (no encontrado: {fname}, se salta)")
        return []
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def entry_by_tmdb(conn, tmdb_id):
    if not tmdb_id:
        return None
    return conn.execute(
        """SELECT entries.* FROM entries JOIN titles ON titles.id = entries.title_id
           WHERE titles.tmdb_id = ?""",
        (tmdb_id,),
    ).fetchone()


def import_favorites(conn):
    items = sorted(load("lists-favorites.json"), key=lambda i: i.get("rank") or 999)
    fav_list = repo.get_default_list(conn)
    added = notes = 0
    for item in items:
        obj = item[item["type"]]
        entry = entry_by_tmdb(conn, obj["ids"].get("tmdb"))
        if not entry:
            continue
        existing = conn.execute(
            "SELECT id FROM list_items WHERE list_id = ? AND entry_id = ?",
            (fav_list["id"], entry["id"]),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE list_items SET position = COALESCE(position, ?) WHERE id = ?",
                (item.get("rank"), existing["id"]),
            )
        else:
            conn.execute(
                "INSERT INTO list_items (list_id, entry_id, position) VALUES (?, ?, ?)",
                (fav_list["id"], entry["id"], item.get("rank")),
            )
            added += 1
        if item.get("notes") and not entry["comment"]:
            conn.execute("UPDATE entries SET comment = ? WHERE id = ?", (item["notes"], entry["id"]))
            notes += 1
    print(f"Favoritos: {added} añadidos a la lista (de {len(items)}), {notes} notas pasadas a comentario")


def import_episode_ratings(conn):
    items = load("ratings-episodes.json")
    marked = 0
    for item in items:
        show = item.get("show") or {}
        ep = item.get("episode") or {}
        title = conn.execute(
            "SELECT id FROM titles WHERE tmdb_id = ?", ((show.get("ids") or {}).get("tmdb"),)
        ).fetchone()
        if not title:
            continue
        cur = conn.execute(
            """UPDATE episodes SET is_favorite = 1
               WHERE title_id = ? AND season_number = ? AND episode_number = ?""",
            (title["id"], ep.get("season"), ep.get("number")),
        )
        marked += cur.rowcount
    print(f"Episodios con nota en Trakt: {marked} marcados como favoritos (de {len(items)})")


def restore_watched_dates(conn):
    restored = 0
    for fname, media_type in (
        ("watched-shows-1.json", "show"), ("watched-shows-2.json", "show"),
        ("watched-shows-3.json", "show"), ("watched-movies.json", "movie"),
    ):
        for item in load(fname):
            obj = item[media_type]
            export_date = item.get("last_watched_at")
            if not export_date or export_date[:10] >= DEPLOY_DATE:
                continue
            entry = entry_by_tmdb(conn, obj["ids"].get("tmdb"))
            if not entry or entry["status"] != "watched" or not entry["watched_at"]:
                continue
            if entry["watched_at"][:10] >= DEPLOY_DATE:
                conn.execute(
                    "UPDATE entries SET watched_at = ? WHERE id = ?", (export_date, entry["id"])
                )
                restored += 1
    print(f"Fechas de visionado restauradas del export: {restored}")


def main():
    with get_connection() as conn:
        import_favorites(conn)
        import_episode_ratings(conn)
        restore_watched_dates(conn)


if __name__ == "__main__":
    main()
