"""app.repo.duels - extraido de repo.py en el split de modulos (ronda 2026-08-21).
Ver app/repo/__init__.py para el mapa completo de que vive en cada fichero -
el resto del proyecto sigue usando `from app import repo; repo.funcion(...)`
exactamente igual que antes, este split es puramente interno."""
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
    """Cuantos duelos faltan de verdad para tener un ranking asentado - no un numero
    inventado (pedido explicito del usuario: "tendria que ser funcional"). Umbral: 5 duelos
    por elemento es lo minimo para que el Elo deje de ser practicamente ruido (con 1-2
    duelos el numero no dice nada todavia). Como random_duel_pair SIEMPRE prioriza a
    los menos comparados, en la practica casi todos los proximos duelos van a restar 1
    al deficit de DOS elementos infracomparados a la vez - de ahi que techo(deficit
    total / 2) sea una cota inferior real de duelos que hacen falta, no una estimacion
    a ojo."""
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




# --- Glicko (2026-08-15) ---------------------------------------------------
# Sustituye al Elo de K fijo (K=32 siempre) para el duelo de entries/list_items/
# favorite_characters. Problema real que resuelve: con K fijo, un item recien
# añadido (0 duelos, puro azar todavia) se mueve exactamente igual que uno ya
# asentado (50 duelos) - de ahi que el usuario notara que "no siempre acaba igual
# aunque puntue lo mismo". Glicko añade una segunda cifra por item, RD (rating
# deviation - "cuanta confianza tengo en este numero"): arranca en 350 (nada
# seguro) y se estrecha con cada duelo, asi que los primeros duelos mueven MUCHO
# el rating y los de un item ya asentado (RD bajo) solo lo afinan. Funciona
# identico para las 3 tablas - solo usa los propios duelos, cero dependencia de
# tener una nota (a diferencia del rating-seed de mas abajo, que SOLO aplica a
# entries porque es la unica tabla con una nota real del usuario).
GLICKO_Q = math.log(10) / 400


GLICKO_RD_INIT = 350.0


GLICKO_RD_MIN = 50.0   # nunca baja de aqui - El usuario puede cambiar de opinion siempre


BRIDGE_DUEL_CHANCE = 0.1  # ~1 de cada 10 duelos cruza extremos del ranking a


# proposito, para que el pool no se parta en bloques que nunca se comparan entre
# si (el emparejamiento normal por "mas parecido" nunca los cruzaria solo).

# Seed de Elo para entries desde la nota real del usuario (SOLO esta tabla tiene
# nota) - percentil de la nota dentro de TODAS sus notas de anime, pasado a Elo
# con la formula inversa del propio Elo. Dos notas iguales caen siempre en el
# mismo Elo de partida, en vez del 1500 plano de antes que no distinguia nada.
# Escala calibrada con datos reales del usuario (2026-08-15, 532 notas, media 6.71):
# 200 deja el rango en ~1150-1850, parecido a lo que producirian duelos reales -
# con la escala "de libro" (400) el rango se disparaba a 700-2300, demasiado.
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
    """Recalcula el Elo semilla de TODAS las entries de anime de ESTE usuario que aun
    no han jugado ni un duelo - se llama cada vez que una nota cambia (mark_watched) y
    tambien desde reset_elo. Por que en lote y no solo la entry que cambio: el
    percentil de una nota depende de TODA la poblacion de notas (de este usuario), asi
    que si solo se recalculara la que acaba de tocarse, dos entries con la MISMA nota
    podrian acabar con Elo semilla ligeramente distinto solo por en que momento se
    puntuo cada una - justo lo que se queria evitar. Las que YA jugaron un duelo no se
    tocan (su Elo es evidencia real ganada en duelo, no una estimacion de partida).

    Multiusuario Fase 3 (2026-09-18): antes calculaba el percentil sobre TODAS las
    notas de la instancia, mezclando el gusto de cualquiera - ahora solo sobre las
    propias."""
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
    """Reinicia el Elo/RD de estos ids concretos y borra su historial de duelos y
    sus fotos de Elo (elo_snapshots) - para cuando algo deja el ranking descolocado
    (duelos de prueba, un lote raro) y el usuario prefiere empezar de cero. En `entries`
    reseed desde la nota real en vez de 1500 plano (ver reseed_undueled_entries_elo -
    se llama DESPUES de borrar sus duelos, para que estos ids vuelvan a contar como
    "sin duelar" y entren en el recalculo, `user_id` obligatorio en ese caso desde la
    Fase 3); las otras tablas no tienen nota con la que anclar, vuelven a 1500 tal
    cual. Acotado siempre a una lista de ids ya resuelta por el llamador
    (list_items.id/favorite_characters.id son globales, NO por lista - nunca vaciar
    `table` entera sin querer)."""
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
    """Entry_id vistos de ESTE usuario que son anime - el pool del duelo global
    (El usuario: "el duelo es solo de animes", 2026-08-13, corrigiendo mi primera version
    que cogia TODO lo
    visto). Mismo criterio que _is_anime()/_genero_sql("Anime"): genero Animation/
    Animacion, o sin genero cacheado (altas manuales, el catalogo importado es casi
    todo anime) - no confundir con 'Animacion' a secas, que en TMDB tambien mete
    dibujos occidentales.

    entries.status = 'watched' NO significa "acabado" en este esquema - se pone en
    cuanto se marca UN episodio (_promote_if_first_watch), asi que sin el filtro de
    abajo un anime a medias (El usuario, notas.txt: "Skip and Loafer, 2 eps, no puedo
    hacer duelo de algo que no me he acabado") entraba igual en el pool. Criterio
    real (El usuario, corrigiendo mi primer intento que exigia la serie ENTERA sin nada
    pendiente): basta con haber visto una temporada COMPLETA, aunque haya otra
    temporada mas nueva sin empezar - "si Mushoku esta en temporada 3 pero ya me he
    visto la primera, puede estar en duelos, tengo algo visto de verdad". Filtro:
    existe al menos una season_number con episodios vistos Y sin ningun episodio ya
    emitido pendiente DENTRO de esa misma temporada.

    Dos bypass necesarios, confirmados contra datos reales (una primera version sin
    ellos excluia 12 titulos reales que no deberian - Looney Tunes, iCarly, InuYasha,
    Boku no Pico...): las pelis no tienen filas en episodes (el EXISTS no encontraria
    ninguna temporada); y hay entries 'watched' de verdad (marcadas con el boton
    rapido, /vista/{tmdb_id}/{type}) que se quedaron SIN NINGUN episodio sincronizado
    (sync_episodes fallo o nunca se llamo, tolerado desde la ronda del 500 real) - sin
    ningun episodio en BBDD no hay como distinguir "temporada completa" de "a medias",
    asi que ahi se confia en el status tal cual (igual que antes de este fix) en vez
    de excluir por falta de datos que no es culpa del usuario."""
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
    """TODO lo visto de este usuario, sin filtrar por genero - la red de seguridad de
    duel_pool_for_user cuando el pool de anime sale vacio (cuentas sin anime, el usuario,
    2026-09-18: "el duelo era para anime pero para las otras personas no se como
    adaptarlo"). A diferencia de list_watched_ids no exige temporada completa - es
    el pool "normal" (no especificamente pensado para maratones de anime a medias)."""
    return [
        r["id"] for r in conn.execute(
            "SELECT id FROM entries WHERE user_id = ? AND status = 'watched'", (user_id,)
        ).fetchall()
    ]


def duel_pool_for_user(conn, user_id) -> tuple[list[int], bool]:
    """Elige el pool del duelo general: anime si el usuario tiene suficiente (>= 2,
    para poder formar un par), si no cae a todo lo visto - para que /duelo no salga
    vacio solo por no ver anime. Devuelve (ids, es_pool_de_anime) - lo segundo decide
    el titulo/rotulo que enseña la plantilla (ver routers/duelo.py)."""
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




def random_duel_pair(conn, table: str, ids: list[int]):
    """Par para el duelo A/B - None si no hay ni para un duelo. Con Glicko (ver arriba)
    la señal de "cuanto hace falta este duelo" pasa de contar duelos jugados a mirar
    RD directamente (mas preciso, y dejaria sitio a que el RD suba solo con el tiempo
    si algun dia se añade decaimiento por inactividad, cosa que hoy NO se hace).
    Ademas, 1 de cada ~10 duelos es un "puente" a proposito entre extremos del ranking
    actual en vez de "mas parecido" - sin esto, dos bloques del pool que nunca se
    emparejan entre si podrian derivar en Elos que no son comparables de verdad entre
    ellos (islas). Pedido original del usuario (2026-08-13) de priorizar lo menos cubierto
    se mantiene, solo que ahora RD ya ES esa señal, mas fiel que un conteo plano."""
    if len(ids) < 2:
        return None
    placeholders = ",".join("?" * len(ids))
    rows = {
        r["id"]: (r["elo"], r["rd"])
        for r in conn.execute(f"SELECT id, elo, rd FROM {table} WHERE id IN ({placeholders})", ids)
    }
    elos = {i: rows[i][0] for i in ids}
    rds = {i: rows[i][1] for i in ids}

    if len(ids) >= 4 and random.random() < BRIDGE_DUEL_CHANCE:
        sorted_by_elo = sorted(ids, key=lambda i: elos[i])
        mid = len(sorted_by_elo) // 2
        pair = [random.choice(sorted_by_elo[:mid]), random.choice(sorted_by_elo[mid:])]
    else:
        max_rd = max(rds[i] for i in ids)
        candidates = [i for i in ids if rds[i] == max_rd]

        if len(candidates) == 1:
            # Un unico mas-incierto no basta para un par - se completa con alguno
            # de los mas cercanos en Elo del resto del pool (top 3, no siempre el
            # mismo - ver nota de abajo sobre por que no coger siempre el mejor).
            solo = candidates[0]
            rest = sorted((i for i in ids if i != solo), key=lambda i: abs(elos[i] - elos[solo]))
            pair = [solo, random.choice(rest[:3])]
        else:
            # Antes se quedaba SIEMPRE con el par de Elo mas cercano (un unico
            # minimo, sin aleatoriedad) - con un pool estable eso hacia que "Otro
            # par" sin votar devolviera el mismo par una y otra vez, siempre (El usuario:
            # "digo otro par y no se cambia"), porque nada en el calculo cambia
            # hasta que se registra un voto de verdad. Ahora se sortea entre los 3
            # pares mas cercanos en vez de coger siempre el minimo exacto - sigue
            # priorizando "Elo parecido", pero ya no es 100% predecible.
            candidates.sort(key=lambda i: elos[i])
            scored = [
                ([a, b], abs(elos[a] - elos[b]))
                for a, b in zip(candidates, candidates[1:])
            ]
            scored.sort(key=lambda x: x[1])
            pair = random.choice(scored[:3])[0]

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
    """Gap real (AGY, 2026-09-18): las 3 rutas de voto (/duelo, /waifus/duelo,
    /lista/{id}/duelo) reciben a_id/b_id del formulario y llamaban a record_duel
    directamente - la interfaz solo propone pares propios, pero un POST manipulado
    a mano podia votar sobre entries/waifus/list_items de OTRO usuario e inflar o
    desinflar su Elo."""
    sql = _DUEL_OWNERSHIP_SQL[table]
    return conn.execute(sql, (a_id, b_id, user_id)).fetchone()[0] == 2


def record_duel(conn, table: str, a_id: int, b_id: int, user_id: int, result: float = 1.0):
    """El "¿A o B?" en vez de arrastrar flechas: con 15 elementos ya cuesta y con 60
    (las waifus) el orden manual real nunca llega a existir - unas pocas docenas de
    duelos dan un orden mas honesto que ir arrastrando a ojo.

    `result` es el resultado desde el punto de vista de a_id: 1.0 gana a_id, 0.0 gana
    b_id, 0.5 empate ("de los que realmente no sabes" - El usuario). Glicko (ver arriba) en
    vez de Elo de K fijo: cada duelo mueve mas o menos el rating segun el RD de cada
    uno (mas incierto = se mueve mas), y el propio RD baja como efecto colateral del
    duelo. Cada duelo queda registrado en `duels` (no se pierde el historial de
    votos), y el Elo NO pisa `position` - el orden manual (flechas) y el orden por
    duelos (Elo) conviven aparte, se elige cual se ENSEÑA con `lists.order_mode` / el
    ajuste global de waifus (ver list_items_in_list, list_waifus, set_list_order_mode,
    set_waifus_order_mode).

    `user_id` obligatorio (AGY, 2026-09-18): ver _duel_ids_owned - sin comprobar que
    a_id/b_id son de este usuario, un POST manipulado podia votar sobre el Elo de
    otra cuenta."""
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
    """Cuanto Elo/puestos se han ganado (o perdido) desde la ultima vez que se vio esta
    lista/waifus - "ultima sesion" pedida por el usuario sin tener que inventar un concepto
    de sesion de verdad: cada vista de /lista/{id} o /waifus compara contra la foto
    guardada en `elo_snapshots` la vez anterior, y deja una foto nueva para la proxima.
    None para un id sin foto previa (recien añadido, nada que comparar todavia)."""
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
