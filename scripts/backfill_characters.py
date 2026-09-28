import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import repo  # noqa: E402
from app.db import get_connection, init_db  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)


def main():
    init_db()
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT DISTINCT titles.* FROM titles JOIN entries ON entries.title_id = titles.id
               WHERE titles.tmdb_id > 0
                 AND NOT EXISTS (SELECT 1 FROM characters WHERE characters.title_id = titles.id)"""
        ).fetchall()
    ok = fail = 0
    for i, row in enumerate(rows, 1):
        try:
            with get_connection() as conn:
                repo.sync_characters(conn, row)
            ok += 1
        except Exception:
            fail += 1
            logging.exception("fallo con %s", row["title"])
        if repo.characters._is_anime(row):
            time.sleep(0.7)  # AniList limita a ~90 peticiones por minuto
        if i % 50 == 0:
            logging.info("%d/%d", i, len(rows))
    with get_connection() as conn:
        still = conn.execute(
            """SELECT count(DISTINCT titles.id) FROM titles JOIN entries ON entries.title_id = titles.id
               WHERE titles.tmdb_id > 0 AND NOT EXISTS (SELECT 1 FROM characters WHERE characters.title_id = titles.id)"""
        ).fetchone()[0]
    logging.info("reparto: %d titulos procesados, %d fallos, %d siguen sin personajes (TMDB/AniList no tienen)", ok, fail, still)


if __name__ == "__main__":
    main()
