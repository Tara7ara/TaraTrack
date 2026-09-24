#!/usr/bin/env python3
"""Importa el export de Trakt (JSON) a la BBDD. Idempotente: no duplica si se repite.

Uso: TRAKT_EXPORT_DIR=/ruta/a/los/json python3 scripts/seed_from_trakt.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import repo  # noqa: E402
from app.db import get_connection  # noqa: E402

SOURCE_DIR = os.environ.get("TRAKT_EXPORT_DIR", "/tmp/trakt-import")


def load(fname):
    path = os.path.join(SOURCE_DIR, fname)
    if not os.path.exists(path):
        print(f"  (no encontrado: {fname}, se salta)")
        return []
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def ensure_title_safe(conn, tmdb_id, media_type, fallback_title, fallback_year, imdb_id):
    """Como repo.ensure_title, pero si TMDB no tiene el id (o no hay id), da de alta a mano."""
    if tmdb_id:
        existing = repo.get_title(conn, tmdb_id)
        if existing:
            return existing
        try:
            return repo.ensure_title(conn, tmdb_id, media_type)
        except Exception as e:
            print(f"  aviso: TMDB fallo para '{fallback_title}' ({tmdb_id}): {e} - alta manual")
    return repo.ensure_manual_title(conn, media_type, fallback_title, fallback_year, imdb_id)


def seed_watchlist(conn):
    items = load("lists-watchlist.json")
    added = 0
    for item in items:
        media_type = item["type"]
        obj = item[media_type]
        title_row = ensure_title_safe(
            conn, obj["ids"].get("tmdb"), media_type, obj["title"], obj.get("year"), obj["ids"].get("imdb")
        )
        existing_entry = conn.execute(
            "SELECT id FROM entries WHERE title_id = ?", (title_row["id"],)
        ).fetchone()
        if not existing_entry:
            conn.execute("INSERT INTO entries (title_id, status) VALUES (?, 'pending')", (title_row["id"],))
            added += 1
    print(f"Watchlist: {added} nuevas pendientes (de {len(items)} en el export)")


def seed_watched(conn, files, media_type):
    total = 0
    added = 0
    for fname in files:
        items = load(fname)
        total += len(items)
        for item in items:
            obj = item[media_type]
            title_row = ensure_title_safe(
                conn, obj["ids"].get("tmdb"), media_type, obj["title"], obj.get("year"), obj["ids"].get("imdb")
            )
            existing_entry = conn.execute(
                "SELECT id FROM entries WHERE title_id = ?", (title_row["id"],)
            ).fetchone()
            if not existing_entry:
                conn.execute(
                    "INSERT INTO entries (title_id, status, watched_at) VALUES (?, 'watched', ?)",
                    (title_row["id"], item.get("last_watched_at")),
                )
                added += 1
    print(f"{media_type}: {added} nuevas vistas (de {total} en el export)")


def seed_ratings(conn, fname, media_type):
    items = load(fname)
    updated = 0
    for item in items:
        obj = item[media_type]
        tmdb_id = obj["ids"].get("tmdb")
        if not tmdb_id:
            continue
        title_row = repo.get_title(conn, tmdb_id)
        if not title_row:
            continue
        entry = conn.execute("SELECT * FROM entries WHERE title_id = ?", (title_row["id"],)).fetchone()
        if entry and entry["rating"] is None:
            conn.execute("UPDATE entries SET rating = ? WHERE id = ?", (item["rating"], entry["id"]))
            updated += 1
    print(f"Ratings ({media_type}): {updated} notas pre-rellenadas desde Trakt")


def main():
    with get_connection() as conn:
        seed_watchlist(conn)
        seed_watched(conn, ["watched-shows-1.json", "watched-shows-2.json", "watched-shows-3.json"], "show")
        seed_watched(conn, ["watched-movies.json"], "movie")
        seed_ratings(conn, "ratings-shows.json", "show")
        seed_ratings(conn, "ratings-movies.json", "movie")

        totals = conn.execute(
            """SELECT
                (SELECT count(*) FROM titles) AS titulos,
                (SELECT count(*) FROM entries WHERE status='pending') AS pendientes,
                (SELECT count(*) FROM entries WHERE status='watched') AS vistas,
                (SELECT count(*) FROM entries WHERE rating IS NOT NULL) AS con_nota
            """
        ).fetchone()
        print(
            f"\nTotales en BBDD: {totals['titulos']} titulos, {totals['pendientes']} pendientes, "
            f"{totals['vistas']} vistas, {totals['con_nota']} con nota"
        )


if __name__ == "__main__":
    main()
