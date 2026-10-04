"""app.repo.recommendations - recomendados (TMDB + comunidad de AniList) y BAN. El
resto del proyecto usa `from app import repo; repo.funcion(...)`."""
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor

from app import anime, tmdb
from app.db import get_connection
from app.matching import _best_plausible_match, _tmdb_search_flexible
from app.repo._shared import get_setting, set_setting
from app.repo.titles import TIPOS
from app.repo.users import list_users

RECS_EMPTY_RETRY_SECONDS = 30 * 60

SEED_COUNT = 25


# Techo de hilos FIJO, independiente del numero de semillas - con 25 semillas no tiene
# sentido abrir 25 conexiones simultaneas a TMDB (antes max_workers=len(seeds)).
MAX_RECS_WORKERS = 10


# Las semillas con tantos BAN acumulados (recomendaciones suyas rechazadas) pesan
# menos: recomiendan mal para este usuario.
SEED_BAN_THRESHOLD = 3


SEED_BAN_PENALTY = 0.3


# Tope de items cuya semilla PRINCIPAL es la misma, para que una franquicia con nota alta
# no llene la lista entera de recomendados.
MAX_PER_SEED = 6


# Candidatos de la comunidad de AniList (los mas votados por tus semillas) que se
# intentan emparejar con TMDB en cada tanda - el emparejamiento es lo caro (una
# busqueda TMDB por candidato), la puntuacion en si es local.
ANILIST_CANDIDATES = 60


def _seed_ban_counts(conn, user_id: int):
    """Cuántos BAN salieron de cada semilla ("porque te gustó X"), por usuario."""
    rows = conn.execute(
        """SELECT seed_title, count(*) AS c FROM rejected_recommendations
           WHERE seed_title IS NOT NULL AND user_id = ? GROUP BY seed_title""",
        (user_id,),
    ).fetchall()
    return {row["seed_title"]: row["c"] for row in rows}


def _seed_weight(rating, seed_title, ban_counts):
    """Ponderar por la nota de la semilla en vez de contar todas igual: un 10 debe
    arrastrar mas peso que un 8.5. Favoritos sin nota propia (aun no puntuados) se
    tratan como el umbral minimo de semilla (0.85 = nota 8.5)."""
    weight = (rating / 10) if rating else 0.85
    if ban_counts.get(seed_title, 0) >= SEED_BAN_THRESHOLD:
        weight *= SEED_BAN_PENALTY
    return weight


def _anilist_community_candidates(conn, user_id, ban_counts):
    """Recomendaciones de la comunidad de AniList (anilist_cross_rec_ids, ya cacheadas)
    sumadas sobre todas las semillas anime. Son locales, así que no hace falta
    muestrear como con TMDB; para anime ordenan mucho mejor que el grafo de TMDB.
    Devuelve [(anilist_id, score, [(peso, semilla), ...])] ordenado por score, sin lo
    que el usuario ya tiene."""
    seeds = conn.execute(
        """SELECT titles.title, titles.anilist_cross_rec_ids, entries.rating
           FROM entries JOIN titles ON titles.id = entries.title_id
           WHERE entries.user_id = ? AND titles.anilist_cross_rec_ids IS NOT NULL
             AND titles.anilist_cross_rec_ids != ''
             AND (entries.rating >= 8.5 OR EXISTS(
                  SELECT 1 FROM list_items JOIN lists ON lists.id = list_items.list_id
                  WHERE list_items.entry_id = entries.id AND lists.is_default = 1))""",
        (user_id,),
    ).fetchall()
    known = {
        row["anilist_id"] for row in conn.execute(
            """SELECT titles.anilist_id FROM entries JOIN titles ON titles.id = entries.title_id
               WHERE entries.user_id = ? AND titles.anilist_id IS NOT NULL""",
            (user_id,),
        )
    }
    scores, seeds_by_cand = {}, {}
    for seed in seeds:
        weight = _seed_weight(seed["rating"], seed["title"], ban_counts)
        for raw_id in seed["anilist_cross_rec_ids"].split(","):
            if not raw_id:
                continue
            cand = int(raw_id)
            if cand in known:
                continue
            scores[cand] = scores.get(cand, 0.0) + weight
            seeds_by_cand.setdefault(cand, []).append((weight, seed["title"]))
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    return [(cand, score, sorted(seeds_by_cand[cand], reverse=True)) for cand, score in ranked]


def _match_anilist_to_tmdb(names: dict):
    """Primer resultado plausible de TMDB para un anime de AniList, probando el
    titulo en ingles y luego el romaji (mismo freno que /calendario/abrir)."""
    for title in (names.get("english"), names.get("romaji")):
        if not title:
            continue
        match = _best_plausible_match(title, _tmdb_search_flexible(title))
        if match:
            return match
    return None


def refresh_recommendations_cache(user_id: int | None = None, seed_count=SEED_COUNT):
    """Recalcula los recomendados y los guarda en recommendations_cache, para que
    /recomendados sea una lectura local. Sin user_id, los de todos los usuarios (sync
    de fondo); con user_id, solo los de ese usuario."""
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
            seed_weight = _seed_weight(seed["rating"], seed["title"], ban_counts)
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

    # Comunidad de AniList: se suma al mismo ranking. Las semillas de AniList van
    # delante (ordenadas por peso), asi el tope por semilla principal y el "Porque
    # te gusto X" reflejan la mas fuerte.
    try:
        community = _anilist_community_candidates_for(user_id, ban_counts)[:ANILIST_CANDIDATES]
        names = anime.get_anilist_titles([cand for cand, _score, _seeds in community]) if community else {}
    except Exception:
        logging.exception("recomendados: fallo la parte de AniList para usuario %s", user_id)
        community, names = [], {}
    with ThreadPoolExecutor(max_workers=MAX_RECS_WORKERS) as pool:
        matches = list(pool.map(lambda c: _match_anilist_to_tmdb(names.get(c[0], {})), community))
    for (cand, score, cand_seeds), match in zip(community, matches):
        if not match or match["tmdb_id"] in known:
            continue
        item = scored.setdefault(match["tmdb_id"], {**match, "seeds": [], "score": 0.0})
        item["adult"] = bool(item.get("adult")) or names.get(cand, {}).get("is_adult", False)
        seed_titles = [title for _w, title in cand_seeds]
        item["seeds"] = seed_titles + [t for t in item["seeds"] if t not in seed_titles]
        item["score"] += score

    ranked = sorted(scored.values(), key=lambda r: (r["score"], r.get("popularity") or 0), reverse=True)

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
                    popularity, is_adult, seeds, score, genres)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    user_id, item["tmdb_id"], item["type"], item["title"], item.get("year"),
                    item.get("poster_url"), item.get("overview"), item.get("vote_average"),
                    item.get("popularity"), int(bool(item.get("adult"))),
                    json.dumps(item["seeds"], ensure_ascii=False), item["score"], ",".join(item.get("genres") or []),
                ),
            )
    logging.info(
        "recomendados: cache recalculada para usuario %s, %d titulos (%d semillas)",
        user_id, len(capped), len(seeds),
    )
    try:
        with get_connection() as conn:
            refresh_tmdb_extra(conn, [(item["tmdb_id"], item["type"]) for item in capped])
    except Exception:
        logging.exception("recomendados: fallo al traer fondos/estado de TMDB")


def _load_seeds(raw):
    """Semillas guardadas como JSON (un título con coma se partía en dos); las filas
    antiguas separadas por comas se siguen leyendo hasta el siguiente refresco."""
    if not raw:
        return []
    if raw.startswith("["):
        return json.loads(raw)
    return raw.split(",")


def list_recommendations(conn, user_id: int, tipo="", limit=40, tanda=0, with_pages=False):
    """Lee la tanda ya calculada por refresh_recommendations_cache - sin llamar a TMDB.
    Si la cache de ESTE usuario esta vacia (cuenta recien creada, o primer arranque
    antes de la primera sync de fondo), la calcula al vuelo como red de seguridad -
    con cero semillas propias (nada puntuado con 8.5+ ni en Favoritos) se queda
    vacia igualmente, y /recomendados ya tiene un estado vacio para ese caso
    ("Todavía no hay de dónde tirar").

    Siempre por score, de mejor a peor: `tanda` pasa a los siguientes `limit` (y da
    la vuelta al principio al acabarse). Antes "Otra tanda" sorteaba 40 al azar del
    pool entero, y lo mejor podia no salir. Con `with_pages` devuelve tambien
    (lista, numero_de_tandas)."""
    if conn.execute(
        "SELECT count(*) AS c FROM recommendations_cache WHERE user_id = ?", (user_id,)
    ).fetchone()["c"] == 0:
        key = f"recs_intento:{user_id}"
        last = get_setting(conn, key)
        now = time.time()
        if not last or now - float(last) > RECS_EMPTY_RETRY_SECONDS:
            set_setting(conn, key, str(now))
            conn.commit()
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
        {**dict(r), "seeds": _load_seeds(r["seeds"]), "adult": bool(r["is_adult"]),
         "genres": [g for g in (r["genres"] or "").split(",") if g]}
        for r in rows
        if r["tmdb_id"] not in banned and r["tmdb_id"] not in known
    ]
    pool.sort(key=lambda r: r["score"], reverse=True)
    pages = max(1, -(-len(pool) // limit))
    start = (tanda % pages) * limit
    shown = pool[start:start + limit]
    return (shown, pages) if with_pages else shown


def reject_recommendation(
    conn, tmdb_id: int, media_type: str, title: str, poster_path: str | None, user_id: int,
    seed_title: str | None = None,
):
    """BAN de una tarjeta de recomendados: no vuelve a salir. Guarda también la semilla
    que la generó, para que _seed_ban_counts detecte semillas que recomiendan mal."""
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
    """Deshace un BAN; filtra por usuario para no tocar los de otra cuenta."""
    conn.execute(
        "DELETE FROM rejected_recommendations WHERE id = ? AND user_id = ?", (rejected_id, user_id)
    )


def _anilist_community_candidates_for(user_id, ban_counts):
    with get_connection() as conn:
        return _anilist_community_candidates(conn, user_id, ban_counts)


TMDB_EXTRA_MAX_AGE_DAYS = 7


def refresh_tmdb_extra(conn, items, max_age_days=TMDB_EXTRA_MAX_AGE_DAYS):
    """Rellena tmdb_extra (fondo, logo, estado de emision) para titulos fuera de la
    biblioteca - la portada y las tiras de /recomendados salen de aqui sin llamar a
    TMDB al abrir la pagina. Solo pide lo que falta o tiene mas de una semana."""
    fresh = {
        (row["tmdb_id"], row["type"]) for row in conn.execute(
            # Sin in_production (filas de antes de esa columna) se vuelve a pedir.
            "SELECT tmdb_id, type FROM tmdb_extra WHERE fetched_at > datetime('now', ?) AND in_production IS NOT NULL",
            (f"-{max_age_days} days",),
        )
    }
    todo = [it for it in dict.fromkeys(items) if it not in fresh and it[0] > 0]
    if not todo:
        return 0

    def fetch(it):
        try:
            return it, tmdb.get_extra(*it)
        except Exception:
            return it, None

    with ThreadPoolExecutor(max_workers=MAX_RECS_WORKERS) as pool:
        results = list(pool.map(fetch, todo))
    for (tmdb_id, media_type), extra in results:
        if extra is None:
            continue
        conn.execute(
            """INSERT INTO tmdb_extra (tmdb_id, type, backdrop_path, logo_path, show_status,
                   next_episode_air_date, next_episode_label, in_production, fetched_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
               ON CONFLICT(tmdb_id, type) DO UPDATE SET backdrop_path = excluded.backdrop_path,
                   logo_path = excluded.logo_path, show_status = excluded.show_status,
                   next_episode_air_date = excluded.next_episode_air_date,
                   next_episode_label = excluded.next_episode_label, in_production = excluded.in_production,
                   fetched_at = excluded.fetched_at""",
            (tmdb_id, media_type, extra["backdrop_path"], extra["logo_path"], extra["show_status"],
             extra["next_episode_air_date"], extra["next_episode_label"], int(extra["in_production"])),
        )
    return len(results)


def recommendation_extras(conn, recs):
    """Añade a cada recomendado su fondo, logo y fila de estado (para la tira) desde tmdb_extra."""
    if not recs:
        return recs
    ids = [r["tmdb_id"] for r in recs]
    ph = ",".join("?" * len(ids))
    extra = {
        (row["tmdb_id"], row["type"]): row
        for row in conn.execute(f"SELECT * FROM tmdb_extra WHERE tmdb_id IN ({ph})", ids)
    }
    out = []
    for r in recs:
        e = extra.get((r["tmdb_id"], r["type"]))
        out.append({
            **r,
            "backdrop_path": e["backdrop_path"] if e else None,
            "logo_path": e["logo_path"] if e else None,
            "show_status": e["show_status"] if e else None,
            "next_episode_air_date": e["next_episode_air_date"] if e else None,
            "next_episode_label": e["next_episode_label"] if e else None,
            "in_production": e["in_production"] if e else None,
        })
    return out
