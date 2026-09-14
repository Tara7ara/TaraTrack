#!/usr/bin/env python3
"""Importa el historial episodio a episodio del export de Trakt (watched-history-*.json)
a la tabla episodes. Idempotente: si un episodio ya tiene watched_at no lo pisa, y
repetir la ejecucion no duplica filas (UNIQUE por titulo/temporada/episodio).

Es el complemento de seed_from_trakt.py (que solo importa a nivel de titulo): con esto
"Continuar viendo" arranca con el progreso real que habia en Trakt.

Uso: TRAKT_EXPORT_DIR=/ruta/a/los/json python3 scripts/import_history_from_trakt.py
"""
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import get_connection  # noqa: E402

SOURCE_DIR = os.environ.get("TRAKT_EXPORT_DIR", "/tmp/trakt-import")


def main():
    files = sorted(glob.glob(os.path.join(SOURCE_DIR, "watched-history-*.json")))
    if not files:
        print(f"No hay watched-history-*.json en {SOURCE_DIR}")
        return

    with get_connection() as conn:
        by_tmdb = {
            row["tmdb_id"]: row["id"]
            for row in conn.execute("SELECT id, tmdb_id FROM titles WHERE type = 'show'")
        }
        before = conn.execute(
            "SELECT count(*) AS c FROM episodes WHERE watched_at IS NOT NULL"
        ).fetchone()["c"]

        total = matched = 0
        for path in files:
            with open(path, encoding="utf-8") as f:
                items = json.load(f)
            for item in items:
                if item.get("type") != "episode":
                    continue
                total += 1
                tmdb_id = ((item.get("show") or {}).get("ids") or {}).get("tmdb")
                title_id = by_tmdb.get(tmdb_id)
                if not title_id:
                    continue
                matched += 1
                ep = item["episode"]
                conn.execute(
                    """INSERT INTO episodes (title_id, season_number, episode_number, name, watched_at)
                       VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT(title_id, season_number, episode_number)
                       DO UPDATE SET watched_at = COALESCE(episodes.watched_at, excluded.watched_at)""",
                    (title_id, ep["season"], ep["number"], ep.get("title"), item.get("watched_at")),
                )

        after = conn.execute(
            "SELECT count(*) AS c FROM episodes WHERE watched_at IS NOT NULL"
        ).fetchone()["c"]

    print(f"Eventos de episodio en el export: {total} ({matched} con serie conocida en la BBDD)")
    print(f"Episodios vistos en BBDD: {before} -> {after} (+{after - before})")


if __name__ == "__main__":
    main()
