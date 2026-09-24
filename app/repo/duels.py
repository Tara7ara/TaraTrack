"""app.repo.duels - duelos A/B con Glicko y Elo semilla desde la nota. El resto del
proyecto usa `from app import repo; repo.funcion(...)`."""
import bisect
import math
import random

from app.repo._shared import (
    _IS_ANIME_SQL,
)


def _duel_counts(conn, table: str, ids: list[int]) -> dict:
    """Cuantos duelos ha jugado cada id de `table` hasta ahora (0 si ninguno) - usado
    tanto para elegir el proximo par (random_duel_pair) como para medir cobertura
    (duel_coverage), antes duplicado en las dos funciones."""
    counts = {i: 0 for i in ids}
    if not ids:
        return counts
    placeholders = ",".join("?" * len(ids))
    for r in conn.execute(
        f"""SELECT id, count(*) AS c FROM (
               SELECT winner_id AS id FROM duels WHERE table_name = ? AND winner_id IN ({placeholders})
               UNION ALL
               SELECT loser_id AS id FROM duels WHERE table_name = ? AND loser_id IN ({placeholders})
           ) GROUP BY id""",
        [table, *ids, table, *ids],
    ):
        counts[r["id"]] = r["c"]
    return counts




def duel_coverage(conn, table: str, ids: list[int], min_duels: int = 5):
    """Cuántos duelos faltan como mínimo para un ranking asentado (5 por elemento).
    Cada duelo resta como mucho 1 al déficit de dos elementos a la vez, así que
    techo(déficit total / 2) es una cota inferior."""
    if len(ids) < 2:
        return {"done": True, "covered": 0, "total": len(ids), "remaining": 0, "min_duels": min_duels}
    counts = _duel_counts(conn, table, ids)
    covered = sum(1 for i in ids if counts[i] >= min_duels)
    total_deficit = sum(max(0, min_duels - counts[i]) for i in ids)
    remaining = -(-total_deficit // 2)  # techo sin importar float
    return {
        "done": remaining == 0, "covered": covered, "total": len(ids),
        "remaining": remaining, "min_duels": min_duels,
    }




# --- Glicko ---------------------------------------------------------------
# Cada elemento tiene rating y RD (cuánta confianza hay en él): RD arranca en 350 y
# baja con cada duelo, así que los primeros duelos mueven mucho y los de un elemento
# asentado solo afinan. Igual para las tres tablas: solo depende de los duelos.
GLICKO_Q = math.log(10) / 400


GLICKO_RD_INIT = 350.0


GLICKO_RD_MIN = 50.0   # suelo del RD: la opinión siempre puede cambiar


# Emparejamiento por cuánto aporta cada duelo (ver random_duel_pair).
DUEL_ANCHORS = 30      # items de partida evaluados por peticion (no los N^2 pares)
DUEL_COOLDOWN = 3      # los participantes de los ultimos N duelos descansan

# Elo semilla de entries desde la nota (única tabla con nota): percentil de la nota
# entre todas las de anime del usuario, pasado a Elo con la fórmula inversa. Misma
# nota, mismo punto de partida. Con escala 200 el rango queda en ~1150-1850; con la
# clásica de 400 se disparaba.
RATING_SEED_SCALE = 200




def _glicko_g(rd: float) -> float:
    return 1 / math.sqrt(1 + 3 * GLICKO_Q**2 * rd**2 / math.pi**2)




def _glicko_expected(rating: float, rival_rating: float, rival_rd: float) -> float:
    return 1 / (1 + 10 ** (-_glicko_g(rival_rd) * (rating - rival_rating) / 400))




def _glicko_update(rating: float, rd: float, rival_rating: float, rival_rd: float, score: float):
    """Nuevo (rating, rd) de UN participante tras un duelo. score: 1.0 gano, 0.0
    perdio, 0.5 empate - se llama dos veces por duelo (una por lado) con el score
    ya invertido para el rival."""
    g = _glicko_g(rival_rd)
    e = _glicko_expected(rating, rival_rating, rival_rd)
    d2 = 1 / (GLICKO_Q**2 * g**2 * e * (1 - e))
    new_rating = rating + GLICKO_Q / (1 / rd**2 + 1 / d2) * g * (score - e)
    new_rd = max(GLICKO_RD_MIN, math.sqrt(1 / (1 / rd**2 + 1 / d2)))
    return new_rating, new_rd




def reseed_undueled_entries_elo(conn, user_id):
    """Recalcula el Elo semilla de todas las entries de anime de este usuario que aún
    no han jugado ningún duelo. En lote porque el percentil depende de todas las notas:
    si solo se recalculara la entry tocada, dos notas iguales podrían acabar con
    semillas distintas. Las que ya han jugado no se tocan."""
    rows = conn.execute(
        f"""SELECT entries.id, entries.rating FROM entries JOIN titles ON titles.id = entries.title_id
           WHERE entries.rating IS NOT NULL AND entries.user_id = ? AND {_IS_ANIME_SQL}""",
        (user_id,),
    ).fetchall()
    if len(rows) < 4:
        return
    ratings_sorted = sorted(r["rating"] for r in rows)
    n = len(ratings_sorted)
    dueled_ids = {
        r["id"] for r in conn.execute(
            """SELECT winner_id AS id FROM duels
                 JOIN entries ON entries.id = duels.winner_id
               WHERE duels.table_name = 'entries' AND entries.user_id = ?
               UNION
               SELECT loser_id AS id FROM duels
                 JOIN entries ON entries.id = duels.loser_id
               WHERE duels.table_name = 'entries' AND entries.user_id = ?""",
            (user_id, user_id),
        )
    }
    for row in rows:
        if row["id"] in dueled_ids:
            continue
        lo = bisect.bisect_left(ratings_sorted, row["rating"])
        hi = bisect.bisect_right(ratings_sorted, row["rating"])
        percentile = min(max(((lo + hi) / 2 + 0.5) / n, 0.02), 0.98)
        seed = 1500 + RATING_SEED_SCALE * math.log10(percentile / (1 - percentile))
        conn.execute("UPDATE entries SET elo = ?, rd = ? WHERE id = ?", (seed, GLICKO_RD_INIT, row["id"]))




def elo_confidence_label(rd: float) -> str:
    """Traduce el RD a algo legible junto al Elo - "ningun numero sin su porque",
    mismo criterio que ya usa el resto de la app (afinidad, cobertura de duelos)."""
    if rd >= 250:
        return "poco fiable"
    if rd >= 120:
        return "orientativo"
    return "fiable"




def reset_elo(conn, table: str, ids: list[int], user_id: int | None = None):
    """Reinicia Elo/RD de estos ids y borra sus duelos y sus fotos de Elo. En
    `entries` vuelve a sembrar desde la nota (reseed_undueled_entries_elo, después de
    borrar los duelos; `user_id` obligatorio en ese caso); las otras tablas vuelven a
    1500. Siempre acotado a los ids que pasa el llamador."""
    if not ids:
        return
    placeholders = ",".join("?" * len(ids))
    if table != "entries":
        conn.execute(f"UPDATE {table} SET elo = 1500, rd = ? WHERE id IN ({placeholders})", [GLICKO_RD_INIT, *ids])
    conn.execute(
        f"DELETE FROM duels WHERE table_name = ? AND (winner_id IN ({placeholders}) OR loser_id IN ({placeholders}))",
        [table, *ids, *ids],
    )
    conn.execute(f"DELETE FROM elo_snapshots WHERE table_name = ? AND item_id IN ({placeholders})", [table, *ids])
    if table == "entries":
        reseed_undueled_entries_elo(conn, user_id)




def list_watched_ids(conn, user_id):
    """Entries vistas de anime de este usuario: el pool del duelo general.

    'watched' se pone con el primer episodio, así que se exige al menos una temporada
    completa (con episodios vistos y ninguno ya emitido pendiente en esa temporada).
    Dos excepciones: las películas (no tienen episodios) y las entries marcadas como
    vistas sin ningún episodio sincronizado, donde no hay forma de distinguir y se
    confía en el estado."""
    return [
        r["id"] for r in conn.execute(
            f"""SELECT entries.id FROM entries JOIN titles ON titles.id = entries.title_id
               WHERE entries.status = 'watched' AND entries.user_id = ? AND {_IS_ANIME_SQL}
                 AND (
                     titles.type != 'show'
                     OR NOT EXISTS (SELECT 1 FROM episodes WHERE episodes.title_id = titles.id)
                     OR EXISTS (
                         SELECT 1 FROM episodes AS ep
                         WHERE ep.title_id = titles.id
                         GROUP BY ep.season_number
                         HAVING sum(CASE WHEN EXISTS(SELECT 1 FROM episode_watches WHERE episode_watches.episode_id = ep.id AND episode_watches.user_id = entries.user_id)
                                         THEN 1 ELSE 0 END) > 0
                            AND sum(CASE WHEN NOT EXISTS(SELECT 1 FROM episode_watches WHERE episode_watches.episode_id = ep.id AND episode_watches.user_id = entries.user_id)
                                          AND ep.air_date IS NOT NULL
                                          AND date(ep.air_date) <= date('now') THEN 1 ELSE 0 END) = 0
                     )
                 )""",
            (user_id,),
        ).fetchall()
    ]




def list_all_watched_ids(conn, user_id):
    """Todo lo visto por este usuario, sin filtrar por género: el pool de respaldo
    cuando no hay anime suficiente. No exige temporada completa."""
    return [
        r["id"] for r in conn.execute(
            "SELECT id FROM entries WHERE user_id = ? AND status = 'watched'", (user_id,)
        ).fetchall()
    ]


def duel_pool_for_user(conn, user_id) -> tuple[list[int], bool]:
    """Pool del duelo general: anime si hay al menos 2; si no, todo lo visto.
    Devuelve (ids, es_pool_de_anime), que decide el rótulo de la plantilla."""
    ids = list_watched_ids(conn, user_id)
    if len(ids) >= 2:
        return ids, True
    return list_all_watched_ids(conn, user_id), False


def get_entries_by_ids(conn, ids: list[int]):
    if not ids:
        return []
    placeholders = ",".join("?" * len(ids))
    return conn.execute(
        f"""SELECT entries.id AS id, titles.title AS title, titles.poster_path AS image,
                   titles.year AS subtitle, titles.tmdb_id, titles.type
           FROM entries JOIN titles ON titles.id = entries.title_id
           WHERE entries.id IN ({placeholders})""",
        ids,
    ).fetchall()




def _duel_history(conn, table: str, ids: list[int]):
    """(veces que se ha jugado cada par, ids que han jugado en los ultimos duelos) -
    solo duelos entre items de este pool, asi que ya queda acotado al usuario."""
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(
        f"""SELECT winner_id, loser_id FROM duels
           WHERE table_name = ? AND winner_id IN ({placeholders}) AND loser_id IN ({placeholders})
           ORDER BY id DESC""",
        [table, *ids, *ids],
    ).fetchall()
    met = {}
    for r in rows:
        key = frozenset((r["winner_id"], r["loser_id"]))
        met[key] = met.get(key, 0) + 1
    # Pools pequeños (una lista de 5) no pueden permitirse dejar a 6 en descanso
    cooldown = min(DUEL_COOLDOWN, len(ids) // 4)
    recent = {i for r in rows[:cooldown] for i in (r["winner_id"], r["loser_id"])}
    return met, recent


def _duel_utility(elo_a, rd_a, elo_b, rd_b, times_met):
    """Cuanto aporta el duelo A/B: maximo cuando es un 50/50 de verdad (p*(1-p)),
    crece con la incertidumbre de los dos (RD) y se hunde si ya se han enfrentado."""
    p = _glicko_expected(elo_a, elo_b, math.hypot(rd_a, rd_b))
    # hypot y no la suma de cuadrados: con cuadrados un unico item a RD 350 era el
    # "mejor rival" de todos y salia en la mitad de los duelos
    return p * (1 - p) * math.hypot(rd_a, rd_b) / (1 + times_met) ** 2


def random_duel_pair(conn, table: str, ids: list[int]):
    """Par para el duelo A/B, o None si no da para uno.

    Cada par candidato se puntúa con _duel_utility (lo igualado que está x
    incertidumbre x penalización por repetir). Para no evaluar todos los pares se
    sortean DUEL_ANCHORS elementos de partida, sesgados hacia RD alto, se busca el
    mejor rival de cada uno y se sortea entre esos pares con peso = utilidad. Los
    participantes de los últimos DUEL_COOLDOWN duelos descansan."""
    if len(ids) < 2:
        return None
    placeholders = ",".join("?" * len(ids))
    rows = {
        r["id"]: (r["elo"], r["rd"])
        for r in conn.execute(f"SELECT id, elo, rd FROM {table} WHERE id IN ({placeholders})", ids)
    }
    met, recent = _duel_history(conn, table, ids)
    pool = [i for i in ids if i not in recent]
    if len(pool) < 2:
        pool = list(ids)

    anchors = sorted(pool, key=lambda i: rows[i][1] * random.random(), reverse=True)[:DUEL_ANCHORS]
    best = {}
    for a in anchors:
        elo_a, rd_a = rows[a]
        rival, score = max(
            (
                (b, _duel_utility(elo_a, rd_a, *rows[b], met.get(frozenset((a, b)), 0)))
                for b in pool if b != a
            ),
            key=lambda x: x[1],
        )
        best[frozenset((a, rival))] = score

    pairs = list(best)
    weights = [best[k] for k in pairs]
    pair = list(random.choices(pairs, weights=weights if sum(weights) > 0 else None)[0])
    random.shuffle(pair)
    return pair




_DUEL_OWNERSHIP_SQL = {
    "entries": "SELECT count(*) FROM entries WHERE id IN (?, ?) AND user_id = ?",
    "list_items": """SELECT count(*) FROM list_items JOIN lists ON lists.id = list_items.list_id
                      WHERE list_items.id IN (?, ?) AND lists.user_id = ?""",
    "favorite_characters": """SELECT count(*) FROM favorite_characters
                               JOIN entries ON entries.id = favorite_characters.entry_id
                               WHERE favorite_characters.id IN (?, ?) AND entries.user_id = ?""",
}


def _duel_ids_owned(conn, table: str, a_id: int, b_id: int, user_id: int) -> bool:
    """Comprueba que los dos ids del voto pertenecen a este usuario: el formulario
    solo propone pares propios, pero un POST manipulado podría votar sobre otra cuenta."""
    sql = _DUEL_OWNERSHIP_SQL[table]
    return conn.execute(sql, (a_id, b_id, user_id)).fetchone()[0] == 2


def record_duel(conn, table: str, a_id: int, b_id: int, user_id: int, result: float = 1.0):
    """Registra un duelo "¿A o B?". `result` es desde el punto de vista de a_id: 1.0
    gana a_id, 0.0 gana b_id, 0.5 empate. Actualiza rating y RD con Glicko y guarda el
    duelo en `duels`. El Elo no pisa `position`: el orden manual y el orden por duelos
    conviven y se elige cuál se enseña (lists.order_mode / ajuste de waifus).

    `user_id` obligatorio: ver _duel_ids_owned."""
    if not _duel_ids_owned(conn, table, a_id, b_id, user_id):
        return
    rows = {
        r["id"]: (r["elo"], r["rd"])
        for r in conn.execute(f"SELECT id, elo, rd FROM {table} WHERE id IN (?, ?)", (a_id, b_id))
    }
    if len(rows) != 2:
        return
    elo_a, rd_a = rows[a_id]
    elo_b, rd_b = rows[b_id]
    new_elo_a, new_rd_a = _glicko_update(elo_a, rd_a, elo_b, rd_b, result)
    new_elo_b, new_rd_b = _glicko_update(elo_b, rd_b, elo_a, rd_a, 1 - result)
    conn.execute(f"UPDATE {table} SET elo = ?, rd = ? WHERE id = ?", (new_elo_a, new_rd_a, a_id))
    conn.execute(f"UPDATE {table} SET elo = ?, rd = ? WHERE id = ?", (new_elo_b, new_rd_b, b_id))
    conn.execute(
        "INSERT INTO duels (table_name, winner_id, loser_id, result) VALUES (?, ?, ?, ?)",
        (table, a_id, b_id, result),
    )




def get_elo_deltas(conn, table: str, ids: list[int]):
    """Elo y puestos ganados o perdidos desde la última vez que se vio esta lista o
    /waifus: compara con la foto anterior de `elo_snapshots` y deja una nueva. None
    para un id sin foto previa."""
    if not ids:
        return {}
    placeholders = ",".join("?" * len(ids))
    current_elo = {r["id"]: r["elo"] for r in conn.execute(f"SELECT id, elo FROM {table} WHERE id IN ({placeholders})", ids)}
    ranked = sorted(ids, key=lambda i: current_elo[i], reverse=True)
    current_rank = {item_id: i + 1 for i, item_id in enumerate(ranked)}
    previous = {
        r["item_id"]: (r["elo"], r["rank"])
        for r in conn.execute(
            f"SELECT item_id, elo, rank FROM elo_snapshots WHERE table_name = ? AND item_id IN ({placeholders})",
            [table, *ids],
        )
    }
    deltas = {}
    for i in ids:
        if i in previous:
            prev_elo, prev_rank = previous[i]
            deltas[i] = (round(current_elo[i] - prev_elo), prev_rank - current_rank[i])
        else:
            deltas[i] = None
        conn.execute(
            """INSERT INTO elo_snapshots (table_name, item_id, elo, rank, snapshot_at)
               VALUES (?, ?, ?, ?, datetime('now'))
               ON CONFLICT(table_name, item_id) DO UPDATE SET
                   elo = excluded.elo, rank = excluded.rank, snapshot_at = excluded.snapshot_at""",
            (table, i, current_elo[i], current_rank[i]),
        )
    return deltas
