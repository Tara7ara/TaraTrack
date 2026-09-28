import logging
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import repo, tmdb  # noqa: E402
from app.db import get_connection, init_db  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(message)s")
# httpx registra cada URL en INFO, y la de TMDB lleva la api_key: fuera de la salida.
logging.getLogger("httpx").setLevel(logging.WARNING)
WORKERS = 8


def main():
    init_db()
    with get_connection() as conn:
        titles = conn.execute(
            "SELECT id, tmdb_id, type FROM titles WHERE tmdb_id > 0 AND backdrop_path IS NULL AND logo_path IS NULL"
        ).fetchall()

    def fetch(row):
        try:
            return row, tmdb.get_extra(row["tmdb_id"], row["type"])
        except Exception:
            return row, None

    with ThreadPoolExecutor(WORKERS) as pool:
        results = list(pool.map(fetch, titles))
    with get_connection() as conn:
        for row, extra in results:
            if extra:
                conn.execute("UPDATE titles SET backdrop_path = ?, logo_path = ? WHERE id = ?",
                             (extra["backdrop_path"], extra["logo_path"], row["id"]))
    logging.info("titulos: %d pedidos, %d con datos", len(titles), sum(1 for _, e in results if e))

    with get_connection() as conn:
        eps = conn.execute(
            """SELECT DISTINCT episodes.id, titles.tmdb_id, episodes.season_number, episodes.episode_number
               FROM episode_user_state s JOIN episodes ON episodes.id = s.episode_id
               JOIN titles ON titles.id = episodes.title_id
               WHERE s.is_favorite = 1 AND episodes.still_path IS NULL AND titles.tmdb_id > 0"""
        ).fetchall()
    stills = []
    for ep in eps:
        try:
            stills.append((tmdb.get_episode_still(ep["tmdb_id"], ep["season_number"], ep["episode_number"]), ep["id"]))
        except Exception:
            pass
    with get_connection() as conn:
        conn.executemany("UPDATE episodes SET still_path = ? WHERE id = ?", [s for s in stills if s[0]])
    logging.info("episodios favoritos: %d pedidos, %d con imagen", len(eps), sum(1 for s in stills if s[0]))

    with get_connection() as conn:
        recs = [(r["tmdb_id"], r["type"]) for r in conn.execute("SELECT DISTINCT tmdb_id, type FROM recommendations_cache")]
        n = repo.refresh_tmdb_extra(conn, recs)
    logging.info("recomendados: %d con fondo/estado nuevos", n)


if __name__ == "__main__":
    main()
