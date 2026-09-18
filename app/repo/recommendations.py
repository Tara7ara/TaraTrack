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
from app.repo.users import list_users

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




def _seed_ban_counts(conn, user_id: int):
    """Cuantos BAN vinieron de cada semilla ('porque te gusto X') - la señal negativa
    que antes se guardaba pero nunca se usaba para nada. Por usuario (2026-09-18): el
    ban de uno no debe penalizar la semilla en los recomendados de otro."""
    rows = conn.execute(
        """SELECT seed_title, count(*) AS c FROM rejected_recommendations
           WHERE seed_title IS NOT NULL AND user_id = ? GROUP BY seed_title""",
        (user_id,),
    ).fetchall()
    return {row["seed_title"]: row["c"] for row in rows}




def refresh_recommendations_cache(user_id: int | None = None, seed_count=SEED_COUNT):
    """Recalcula la tanda de recomendados y la deja en recommendations_cache - la
    llama la sync de fondo cada 12h para que /recomendados sea una lectura local
    instantanea en vez de esperar a N llamadas a TMDB en cada visita.

    Bug real (2026-09-18, Tara: "recomienda full anime a la cuenta random"): esto
    era una unica tabla GLOBAL calculada a partir de las entries de TODA la
    instancia (en la practica, casi todo Tara) - cualquier cuenta nueva veia sus
    recomendados. Ahora es por usuario, mismo criterio que recompute_taste_profile
    (repo/sync.py): sin user_id, recalcula la de TODOS los usuarios (llamada de la
    sync de fondo); con user_id, solo la de ese usuario (red de seguridad de
    list_recommendations cuando un usuario visita con la cache aun vacia)."""
    if user_id is None:
        with get_connection() as conn:
            user_ids = [row["id"] for row in list_users(conn)]
        for uid in user_ids:
            refresh_recommendations_cache(user_id=uid, seed_count=seed_count)
        return

    with get_connection() as conn:
        seeds = conn.execute(
            """SELECT DISTINCT titles.tmdb_id, titles.type, titles.title, entries.rating
               FROM entries JOIN titles ON titles.id = entries.title_id
               WHERE titles.tmdb_id > 0 AND entries.user_id = ?
                 AND (entries.rating >= 8.5 OR EXISTS(
                      SELECT 1 FROM list_items JOIN lists ON lists.id = list_items.list_id
                      WHERE list_items.entry_id = entries.id AND lists.is_default = 1))
               ORDER BY RANDOM() LIMIT ?""",
            (user_id, seed_count),
        ).fetchall()
        # Bug real (AGY, 2026-09-18): "known" comprobaba TODO el catalogo compartido
        # (titles), no lo que ESTE usuario tiene en su biblioteca - con el catalogo
        # ya lleno de las ~900 entradas de Tara, una cuenta nueva se quedaba sin
        # poder recibir NINGUNA de esas obras como recomendacion aunque ella nunca
        # las hubiera visto, solo porque Tara si las tenia. "Conocido" tiene que ser
        # "tengo una entry de esto", no "existe en el catalogo de alguien".
        known = {
            row["tmdb_id"] for row in conn.execute(
                """SELECT titles.tmdb_id FROM entries JOIN titles ON titles.id = entries.title_id
                   WHERE entries.user_id = ?""",
                (user_id,),
            )
        }
        known |= {
            row["tmdb_id"] for row in conn.execute(
                "SELECT tmdb_id FROM rejected_recommendations WHERE user_id = ?", (user_id,)
            )
        }
        ban_counts = _seed_ban_counts(conn, user_id)

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
        conn.execute("DELETE FROM recommendations_cache WHERE user_id = ?", (user_id,))
        for item in capped:
            conn.execute(
                """INSERT INTO recommendations_cache
                   (user_id, tmdb_id, type, title, year, poster_url, overview, vote_average,
                    popularity, is_adult, seeds, score)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    user_id, item["tmdb_id"], item["type"], item["title"], item.get("year"),
                    item.get("poster_url"), item.get("overview"), item.get("vote_average"),
                    item.get("popularity"), int(bool(item.get("adult"))),
                    ",".join(item["seeds"]), item["score"],
                ),
            )
    logging.info(
        "recomendados: cache recalculada para usuario %s, %d titulos (%d semillas)",
        user_id, len(capped), len(seeds),
    )




def list_recommendations(conn, user_id: int, tipo="", limit=40):
    """Lee la tanda ya calculada por refresh_recommendations_cache - sin llamar a TMDB.
    Si la cache de ESTE usuario esta vacia (cuenta recien creada, o primer arranque
    antes de la primera sync de fondo), la calcula al vuelo como red de seguridad -
    con cero semillas propias (nada puntuado con 8.5+ ni en Favoritos) se queda
    vacia igualmente, y /recomendados ya tiene un estado vacio para ese caso
    ("Todavía no hay de dónde tirar")."""
    if conn.execute(
        "SELECT count(*) AS c FROM recommendations_cache WHERE user_id = ?", (user_id,)
    ).fetchone()["c"] == 0:
        refresh_recommendations_cache(user_id=user_id)

    media_type = TIPOS.get(tipo)
    banned = {
        row["tmdb_id"] for row in conn.execute(
            "SELECT tmdb_id FROM rejected_recommendations WHERE user_id = ?", (user_id,)
        )
    }
    # Mismo fix que refresh_recommendations_cache: "conocido" es tuyo, no del catalogo
    # compartido entero.
    known = {
        row["tmdb_id"] for row in conn.execute(
            """SELECT titles.tmdb_id FROM entries JOIN titles ON titles.id = entries.title_id
               WHERE entries.user_id = ?""",
            (user_id,),
        )
    }
    params = (user_id, media_type) if media_type else (user_id,)
    rows = conn.execute(
        "SELECT * FROM recommendations_cache WHERE user_id = ?" + (" AND type = ?" if media_type else ""),
        params,
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
    conn, tmdb_id: int, media_type: str, title: str, poster_path: str | None, user_id: int,
    seed_title: str | None = None,
):
    """El BAN de una tarjeta de recomendados: no vuelve a salir. Guarda tambien la
    semilla ("porque te gusto X") que la genero - antes se tiraba esa señal, y con ella
    _seed_ban_counts puede detectar semillas que recomiendan mal para Tara.
    `user_id` obligatorio desde el multiusuario (2026-09-17): antes `tmdb_id` era
    UNIQUE en toda la instancia - sin filtrar, el ban de un usuario ocultaria esa
    recomendación para todos los demás también."""
    conn.execute(
        """INSERT OR IGNORE INTO rejected_recommendations (tmdb_id, type, title, poster_path, seed_title, user_id)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (tmdb_id, media_type, title, poster_path, seed_title, user_id),
    )




def list_rejected_recommendations(conn, user_id):
    return conn.execute(
        "SELECT * FROM rejected_recommendations WHERE user_id = ? ORDER BY rejected_at DESC", (user_id,)
    ).fetchall()




def unreject_recommendation(conn, rejected_id: int, user_id: int):
    """`user_id` obligatorio (multiusuario Fase 3, 2026-09-18) - sin filtrar, cualquier
    usuario podria deshacer el ban de otro adivinando su id."""
    conn.execute(
        "DELETE FROM rejected_recommendations WHERE id = ? AND user_id = ?", (rejected_id, user_id)
    )
