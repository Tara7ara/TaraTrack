"""app.repo.recommendations - extraido de repo.py en el split de modulos (ronda 2026-08-21).
Ver app/repo/__init__.py para el mapa completo de que vive en cada fichero -
el resto del proyecto sigue usando `from app import repo; repo.funcion(...)`
exactamente igual que antes, este split es puramente interno."""
import logging
import random
from concurrent.futures import ThreadPoolExecutor

from app import tmdb
from app.db import get_connection
from app.repo.titles import TIPOS

SEED_COUNT = 25


# Techo de hilos FIJO, independiente del numero de semillas - con 25 semillas no tiene
# sentido abrir 25 conexiones simultaneas a TMDB (antes max_workers=len(seeds)).
MAX_RECS_WORKERS = 10


# Semillas con este numero de BAN acumulados (recomendaciones suyas rechazadas) pesan
# menos en el ranking - son mal recomendadoras para Tara, no todas las semillas valen igual.
SEED_BAN_THRESHOLD = 3


SEED_BAN_PENALTY = 0.3


# Tope de items cuya semilla PRINCIPAL es la misma, para que una franquicia con nota alta
# no llene la lista entera de recomendados.
MAX_PER_SEED = 6




def _seed_ban_counts(conn):
    """Cuantos BAN vinieron de cada semilla ('porque te gusto X') - la señal negativa
    que antes se guardaba pero nunca se usaba para nada."""
    rows = conn.execute(
        """SELECT seed_title, count(*) AS c FROM rejected_recommendations
           WHERE seed_title IS NOT NULL GROUP BY seed_title"""
    ).fetchall()
    return {row["seed_title"]: row["c"] for row in rows}




def refresh_recommendations_cache(seed_count=SEED_COUNT):
    """Recalcula la tanda de recomendados entera y la deja en recommendations_cache -
    la llama la sync de fondo cada 12h para que /recomendados sea una lectura local
    instantanea en vez de esperar a N llamadas a TMDB en cada visita."""
    with get_connection() as conn:
        seeds = conn.execute(
            """SELECT DISTINCT titles.tmdb_id, titles.type, titles.title, entries.rating
               FROM entries JOIN titles ON titles.id = entries.title_id
               WHERE titles.tmdb_id > 0
                 AND (entries.rating >= 8.5 OR EXISTS(
                      SELECT 1 FROM list_items JOIN lists ON lists.id = list_items.list_id
                      WHERE list_items.entry_id = entries.id AND lists.is_default = 1))
               ORDER BY RANDOM() LIMIT ?""",
            (seed_count,),
        ).fetchall()
        known = {row["tmdb_id"] for row in conn.execute("SELECT tmdb_id FROM titles")}
        known |= {row["tmdb_id"] for row in conn.execute("SELECT tmdb_id FROM rejected_recommendations")}
        ban_counts = _seed_ban_counts(conn)

    scored = {}
    with ThreadPoolExecutor(max_workers=min(MAX_RECS_WORKERS, len(seeds) or 1)) as pool:
        futures = {pool.submit(tmdb.get_recommendations, s["tmdb_id"], s["type"]): s for s in seeds}
        for future in futures:
            seed = futures[future]
            # Ponderar por la nota de la semilla en vez de contar todas igual: un 10 debe
            # arrastrar mas peso que un 8.5. Favoritos sin nota propia (aun no puntuados)
            # se tratan como el umbral minimo de semilla (0.85 = nota 8.5).
            seed_weight = (seed["rating"] / 10) if seed["rating"] else 0.85
            if ban_counts.get(seed["title"], 0) >= SEED_BAN_THRESHOLD:
                seed_weight *= SEED_BAN_PENALTY
            try:
                recs = future.result()
            except Exception:
                continue
            for rec in recs:
                if rec["tmdb_id"] in known:
                    continue
                item = scored.setdefault(rec["tmdb_id"], {**rec, "seeds": [], "score": 0.0})
                if seed["title"] not in item["seeds"]:
                    item["seeds"].append(seed["title"])
                    item["score"] += seed_weight

    ranked = sorted(scored.values(), key=lambda r: (r["score"], r["popularity"]), reverse=True)

    # Tope por semilla PRINCIPAL (la primera que la recomendo): sin esto, una franquicia
    # entera con nota alta como semilla podia ocupar media lista de recomendados.
    per_seed_count: dict[str, int] = {}
    capped = []
    for item in ranked:
        primary = item["seeds"][0]
        if per_seed_count.get(primary, 0) >= MAX_PER_SEED:
            continue
        per_seed_count[primary] = per_seed_count.get(primary, 0) + 1
        capped.append(item)

    with get_connection() as conn:
        conn.execute("DELETE FROM recommendations_cache")
        for item in capped:
            conn.execute(
                """INSERT INTO recommendations_cache
                   (tmdb_id, type, title, year, poster_url, overview, vote_average,
                    popularity, is_adult, seeds, score)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    item["tmdb_id"], item["type"], item["title"], item.get("year"),
                    item.get("poster_url"), item.get("overview"), item.get("vote_average"),
                    item.get("popularity"), int(bool(item.get("adult"))),
                    ",".join(item["seeds"]), item["score"],
                ),
            )
    logging.info("recomendados: cache recalculada, %d titulos (%d semillas)", len(capped), len(seeds))




def list_recommendations(conn, tipo="", limit=40):
    """Lee la tanda ya calculada por refresh_recommendations_cache - sin llamar a TMDB.
    Si la cache esta vacia (primer arranque, antes de la primera sync de fondo), la
    calcula al vuelo como red de seguridad para no enseñar una pagina vacia."""
    if conn.execute("SELECT count(*) AS c FROM recommendations_cache").fetchone()["c"] == 0:
        refresh_recommendations_cache()

    media_type = TIPOS.get(tipo)
    banned = {row["tmdb_id"] for row in conn.execute("SELECT tmdb_id FROM rejected_recommendations")}
    known = {row["tmdb_id"] for row in conn.execute("SELECT tmdb_id FROM titles")}
    rows = conn.execute(
        "SELECT * FROM recommendations_cache" + (" WHERE type = ?" if media_type else ""),
        (media_type,) if media_type else (),
    ).fetchall()
    pool = [
        {**dict(r), "seeds": r["seeds"].split(","), "adult": bool(r["is_adult"])}
        for r in rows
        if r["tmdb_id"] not in banned and r["tmdb_id"] not in known
    ]
    # "Otra tanda": variedad sin volver a llamar a TMDB - una muestra al azar del pool
    # cacheado (ponderado por score al ordenar despues) en vez de siempre el mismo top N.
    shown = random.sample(pool, min(limit, len(pool))) if pool else []
    shown.sort(key=lambda r: r["score"], reverse=True)
    return shown




def reject_recommendation(
    conn, tmdb_id: int, media_type: str, title: str, poster_path: str | None, seed_title: str | None = None
):
    """El BAN de una tarjeta de recomendados: no vuelve a salir. Guarda tambien la
    semilla ("porque te gusto X") que la genero - antes se tiraba esa señal, y con ella
    _seed_ban_counts puede detectar semillas que recomiendan mal para Tara."""
    conn.execute(
        """INSERT OR IGNORE INTO rejected_recommendations (tmdb_id, type, title, poster_path, seed_title)
           VALUES (?, ?, ?, ?, ?)""",
        (tmdb_id, media_type, title, poster_path, seed_title),
    )




def list_rejected_recommendations(conn):
    return conn.execute(
        "SELECT * FROM rejected_recommendations ORDER BY rejected_at DESC"
    ).fetchall()




def unreject_recommendation(conn, rejected_id: int):
    conn.execute("DELETE FROM rejected_recommendations WHERE id = ?", (rejected_id,))
