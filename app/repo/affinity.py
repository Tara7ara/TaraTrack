"""app.repo.affinity - índice de afinidad, predicción "% que te gustará" y backfill de
AniList. El resto del proyecto usa `from app import repo; repo.funcion(...)`."""
import bisect
import json
import math
import random
import statistics
import time
from datetime import date, datetime, timezone

from app import anime, tmdb
from app.repo._shared import (
    _IS_ANIME_SQL,
    get_setting,
    set_setting,
)

# Tras esto muchos intentos fallidos SEGUIDOS (a lo largo de varias pasadas del
# boton, no en la misma), se deja de reintentar un titulo - casi siempre es una
# recopilatoria/especial sin entrada propia en AniList, no un fallo transitorio de
# red. 5 y no menos: un 429 de AniList ya reintentado sin exito (ver
# anime.match_anilist_basic) cuenta igual que un "no existe", y varias
# pasadas en dias distintos dan margen a que fuera solo mala suerte de rate-limit.
MAX_ANILIST_MATCH_ATTEMPTS = 5


# El atajo "No me acuerdo, 5 y ya" de /puntuar deja 5.0 con comentario "-": vacía la
# cola, pero no es una opinión sobre el título. El motor de afinidad la trata como sin
# nota; el resto de la app (vistas, estadísticas, duelos) la ve tal cual.
def _real_rating_sql(table: str) -> str:
    return (
        f"CASE WHEN {table}.rating = 5.0 AND TRIM(COALESCE({table}.comment, '')) = '-' "
        f"THEN NULL ELSE {table}.rating END"
    )



_ANILIST_BACKFILL_FILTER_SQL = """
    entries.status IN ('watched', 'pending')
    AND (titles.anilist_tags IS NULL OR titles.anilist_title_romaji IS NULL)
    AND titles.anilist_match_attempts < {max_attempts}
""".format(max_attempts=MAX_ANILIST_MATCH_ATTEMPTS)




def backfill_anilist_profile(conn):
    """Rellena los datos de AniList (id, géneros, estudio, tags, precuelas,
    recomendaciones, títulos romaji/inglés) del anime visto o pendiente que aún no los
    tiene. Botón manual en /calendario/anual: la primera vez son cientos de peticiones.

    Busca primero por `original_title` (TMDB sin traducir) y luego por `title`, porque
    AniList no indexa títulos en español; si falta `original_title`, se pide a TMDB
    y se guarda. Cada fallo suma 1 a `anilist_match_attempts` y, al llegar a
    MAX_ANILIST_MATCH_ATTEMPTS, el título deja de reintentarse."""
    rows = conn.execute(
        f"""SELECT titles.id, titles.tmdb_id, titles.type, titles.title, titles.original_title
           FROM titles JOIN entries ON entries.title_id = titles.id
           WHERE {_ANILIST_BACKFILL_FILTER_SQL} AND {_IS_ANIME_SQL}"""
    ).fetchall()
    if not rows:
        return 0

    # Secuencial con pausa, no ThreadPoolExecutor: AniList limita a ~90/min (a veces
    # menos, "degradado") y 8 en paralelo lo salta enseguida (429 silencioso, visto en
    # vivo - 13/122 la primera vez). match_anilist_basic ya reintenta con Retry-After
    # si aun asi topa el limite. Commit DESPUES DE CADA titulo, no al final: ~150
    # titulos con pausas puede tardar minutos y superar el timeout del proxy (visto en
    # vivo - un 504 a los 90s tiro TODO el progreso a la basura porque no habia commit
    # intermedio, no solo lo que quedaba por hacer)."""
    n = 0
    for row in rows:
        original_title = row["original_title"]
        if not original_title and row["tmdb_id"] > 0:
            try:
                original_title = tmdb.get_details(row["tmdb_id"], row["type"]).get("original_title")
            except Exception:
                original_title = None
            if original_title:
                conn.execute("UPDATE titles SET original_title = ? WHERE id = ?", (original_title, row["id"]))
                conn.commit()

        match = None
        # dict.fromkeys en vez de un set: quita duplicados (titulo == original_title
        # en peliculas/series no-japonesas) sin perder el orden de intento.
        for candidate in dict.fromkeys(t for t in (original_title, row["title"]) if t):
            try:
                match = anime.match_anilist_basic(candidate)
            except Exception:
                match = None
            if match:
                break
            time.sleep(0.7)
        if match:
            conn.execute(
                """UPDATE titles SET anilist_id = ?, anilist_genres = ?, anilist_studio = ?,
                   anilist_tags = ?, anilist_prequel_ids = ?, anilist_cross_rec_ids = ?,
                   anilist_cross_rec_votes = ?,
                   anilist_title_romaji = ?, anilist_title_english = ? WHERE id = ?""",
                (
                    match["anilist_id"], ",".join(match.get("genres") or []), match.get("studio"),
                    ",".join(match.get("tags") or []),
                    ",".join(str(i) for i in match.get("prequel_ids") or []),
                    ",".join(str(i) for i in match.get("cross_rec_ids") or []),
                    ",".join(str(v) for v in match.get("cross_rec_votes") or []),
                    match.get("title_romaji"), match.get("title_english"),
                    row["id"],
                ),
            )
            conn.commit()
            n += 1
        else:
            conn.execute(
                "UPDATE titles SET anilist_match_attempts = anilist_match_attempts + 1 WHERE id = ?",
                (row["id"],),
            )
            conn.commit()
        time.sleep(0.7)
    return n




def snapshot_profile_progress(conn, user_id):
    """Foto diaria de cuánto perfil de gustos hay construido para este usuario.
    INSERT OR IGNORE por (usuario, fecha): solo cuenta la primera llamada del día.
    Se llama desde /pendientes, sin necesidad de un cron."""
    covered = conn.execute(
        """SELECT count(*) FROM titles JOIN entries ON entries.title_id = titles.id
           WHERE entries.rating IS NOT NULL AND entries.user_id = ? AND titles.anilist_id IS NOT NULL""",
        (user_id,),
    ).fetchone()[0]
    total_rated = conn.execute(
        f"""SELECT count(*) FROM titles JOIN entries ON entries.title_id = titles.id
           WHERE entries.rating IS NOT NULL AND entries.user_id = ? AND {_IS_ANIME_SQL}""",
        (user_id,),
    ).fetchone()[0]
    today = datetime.now(timezone.utc).date().isoformat()
    conn.execute(
        "INSERT OR IGNORE INTO profile_history (user_id, date, covered, total_rated) VALUES (?, ?, ?, ?)",
        (user_id, today, covered, total_rated),
    )




def get_profile_history(conn, user_id, limit: int = 30):
    """Historial de snapshots de ESTE usuario, mas reciente primero - para el grafico
    de barras de /ajustes."""
    return conn.execute(
        "SELECT * FROM profile_history WHERE user_id = ? ORDER BY date DESC LIMIT ?", (user_id, limit)
    ).fetchall()




def count_anilist_backfill_pending(conn) -> int:
    """Cuántos títulos le quedan al botón "Actualizar perfil de gustos". Mismo filtro
    que backfill_anilist_profile, incluido el tope de intentos, para que el contador
    no se quede pegado."""
    return conn.execute(
        f"""SELECT count(*) FROM titles JOIN entries ON entries.title_id = titles.id
           WHERE {_ANILIST_BACKFILL_FILTER_SQL} AND {_IS_ANIME_SQL}"""
    ).fetchone()[0]





# Índice de afinidad. Dos ejes por atributo (género/tag/estudio): APETITO (cuánto lo
# buscas: volumen, lift, ritmo, día 1, rewatch, curación) y CALIDAD (cuánto te llega:
# techo, suelo, disfrute, media, corrección Elo). Se combinan con media geométrica y
# castigo al desfase, no con suma ponderada, para desinflar lo que ves por costumbre.
#
# El cálculo completo vive en recompute_taste_profile (sync de fondo y botón manual);
# las páginas solo leen taste_profile vía build_taste_profile.

FRANCHISE_EPISODE_CAP = 300  # tope de "episodios vistos" contados por franquicia


RELIABLE_DATE_MIN = "1980-01-01"  # mismo corte que get_available_years - descarta


# fechas placeholder del import de Trakt (epoch 1970 y similares)

APPETITE_WEIGHTS = {
    "volumen": 0.28, "lift": 0.20, "ritmo": 0.18, "dia1": 0.14, "rewatch": 0.12, "curacion": 0.08,
}


QUALITY_WEIGHTS = {
    "techo": 0.50, "suelo": 0.18, "disfrute": 0.14, "media": 0.12, "elo": 0.06,
}


AFFINITY_APPETITE_EXP = 0.6


AFFINITY_QUALITY_EXP = 0.4


AFFINITY_GAP_THRESHOLD = 25.0  # desfase (apetito-calidad) a partir del cual empieza a penalizar


AFFINITY_GAP_FACTOR = 0.5      # cuanto castiga cada punto de desfase por encima del umbral


AFFINITY_CONFIDENCE_K = 3      # suavizado de confianza(A) por numero de titulos, y de cada


# señal de calidad hacia el neutro (50) - el eje apetito no se suaviza asi: son conteos/
# ratios de consumo, ya naturalmente moderados por volumen de datos al percentilizar.

PREDICT_SIGNAL_WEIGHTS = {
    "precuela": 5.0, "cross_rec": 2.0, "estudio": 2.0, "tags": 3.5, "genero": 1.0,
}


# Confianza mínima para el marco de "Candidato a obra maestra". Un título con solo
# señal de género (~7% de confianza) puede llegar al 100% por coincidir en un género;
# 10 lo excluye y deja pasar estudio (~15%), tags (~26%) o precuela (~37%).
MASTERPIECE_MIN_CONFIDENCE = 10



_AFFINITY_CONFIG_DEFAULTS = {
    "appetite_weights": APPETITE_WEIGHTS,
    "quality_weights": QUALITY_WEIGHTS,
    "appetite_exp": AFFINITY_APPETITE_EXP,
    "quality_exp": AFFINITY_QUALITY_EXP,
    "gap_threshold": AFFINITY_GAP_THRESHOLD,
    "gap_factor": AFFINITY_GAP_FACTOR,
    "predict_weights": PREDICT_SIGNAL_WEIGHTS,
}




def get_affinity_config(conn, user_id):
    """Pesos del índice de afinidad de este usuario: las constantes de arriba por
    defecto, con override opcional en app_settings (una clave por usuario), editable
    desde /ajustes."""
    cfg = {k: (dict(v) if isinstance(v, dict) else v) for k, v in _AFFINITY_CONFIG_DEFAULTS.items()}
    raw = get_setting(conn, f"affinity_config:{user_id}")
    if not raw:
        return cfg
    try:
        saved = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return cfg
    for key, default in cfg.items():
        if key not in saved:
            continue
        if isinstance(default, dict):
            for sub_key in default:
                if sub_key in saved[key]:
                    try:
                        default[sub_key] = float(saved[key][sub_key])
                    except (TypeError, ValueError):
                        pass
        else:
            try:
                cfg[key] = float(saved[key])
            except (TypeError, ValueError):
                pass
    return cfg




def set_affinity_config(conn, user_id, cfg: dict):
    set_setting(conn, f"affinity_config:{user_id}", json.dumps(cfg))




def reset_affinity_config(conn, user_id):
    conn.execute("DELETE FROM app_settings WHERE key = ?", (f"affinity_config:{user_id}",))




def _split_ids(text) -> list[int]:
    return [int(i) for i in (text or "").split(",") if i]




def _title_attrs(t) -> list[tuple[str, str]]:
    """Lista (tipo, nombre) de atributos AniList de un titulo - genero(s), tag(s) y
    estudio, la misma unidad que se agrega en cada señal del indice de afinidad."""
    attrs = [("genre", g) for g in (t["anilist_genres"] or "").split(",") if g]
    attrs += [("tag", tg) for tg in (t["anilist_tags"] or "").split(",") if tg]
    if t["anilist_studio"]:
        attrs.append(("studio", t["anilist_studio"]))
    return attrs




def _percentile(values: list[float], p: int) -> float:
    """Percentil p (1-99) de una lista de notas. Con menos de 4 valores el percentil
    no tiene apoyo estadistico, se usa la media de los 3 valores mas extremos en
    la direccion de p (top 3 para p>=50 como el techo, bottom 3 para p<50 como el
    suelo)."""
    if len(values) < 4:
        extreme = sorted(values, reverse=(p >= 50))[:3]
        return sum(extreme) / len(extreme)
    return statistics.quantiles(values, n=100, method="inclusive")[p - 1]




def _percentile_rank(raw: dict) -> dict:
    """Normaliza cada señal a percentil comparando el atributo con el resto de
    atributos del usuario. Rango promedio en empates."""
    if not raw:
        return {}
    if len(raw) == 1:
        return {k: 50.0 for k in raw}
    items = sorted(raw.items(), key=lambda kv: kv[1])
    n = len(items)
    result, i = {}, 0
    while i < n:
        j = i
        while j + 1 < n and items[j + 1][1] == items[i][1]:
            j += 1
        pct = (i + j) / 2 / (n - 1) * 100
        for k, _ in items[i:j + 1]:
            result[k] = pct
        i = j + 1
    return result




def _franchise_roots(known_ids: set, prequel_map: dict) -> dict:
    """Agrupa titulos en franquicias via sus precuelas (anilist_prequel_ids) con
    union-find sobre un grafo NO DIRIGIDO: AniList solo da el enlace PREQUEL desde el
    lado "de arriba" (nunca SEQUEL desde el lado viejo), asi que unir a y b sin
    importar en que direccion se guardo el enlace es necesario para que temporada 1 y
    temporada 3 caigan en la misma franquicia aunque solo 3->2 y 2->1 esten
    guardados."""
    parent = {aid: aid for aid in known_ids}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for aid, pids in prequel_map.items():
        if aid not in parent:
            continue
        for pid in pids:
            if pid in parent:
                union(aid, pid)
    return {aid: find(aid) for aid in known_ids}




def _load_affinity_raw(conn, user_id):
    """Carga en memoria, en una sola pasada de SQL, todo lo que necesita el índice de
    afinidad de este usuario: así el backtesting (recalcular el perfil una vez por
    título puntuado) reagrega en Python sin volver a la BBDD."""
    titles = conn.execute(
        f"""SELECT entries.id AS entry_id, {_real_rating_sql("entries")} AS rating, entries.cat_disfrute,
                  entries.elo, entries.status, entries.is_habit,
                  titles.id AS title_id, titles.type, titles.show_status,
                  titles.anilist_id, titles.anilist_genres, titles.anilist_tags,
                  titles.anilist_studio, titles.anilist_prequel_ids, titles.anilist_cross_rec_ids
           FROM entries JOIN titles ON titles.id = entries.title_id
           WHERE titles.anilist_id IS NOT NULL AND entries.user_id = ?""",
        (user_id,),
    ).fetchall()

    # episodes.watched_at/is_favorite ya no se actualizan (viven en
    # episode_watches/episode_user_state, ambas con user_id) - aqui se filtran a los
    # marcados de ESTE usuario en concreto.
    episodes = conn.execute(
        """SELECT episodes.title_id, episodes.season_number, episodes.episode_number,
                  (SELECT max(watched_at) FROM episode_watches
                     WHERE episode_watches.episode_id = episodes.id AND episode_watches.user_id = ?) AS watched_at,
                  episodes.air_date,
                  (SELECT max(is_favorite) FROM episode_user_state
                     WHERE episode_user_state.episode_id = episodes.id AND episode_user_state.user_id = ?) AS is_favorite
           FROM episodes JOIN titles ON titles.id = episodes.title_id
           WHERE titles.anilist_id IS NOT NULL
             AND EXISTS(SELECT 1 FROM episode_watches
                        WHERE episode_watches.episode_id = episodes.id AND episode_watches.user_id = ?)""",
        (user_id, user_id, user_id),
    ).fetchall()

    rewatched_entries = {
        r["entry_id"] for r in conn.execute(
            """SELECT DISTINCT watch_sessions.entry_id
               FROM watch_sessions JOIN entries ON entries.id = watch_sessions.entry_id
               JOIN titles ON titles.id = entries.title_id
               WHERE titles.anilist_id IS NOT NULL AND entries.user_id = ?""",
            (user_id,),
        )
    }
    listed_entries = {
        r["entry_id"] for r in conn.execute(
            """SELECT DISTINCT list_items.entry_id
               FROM list_items JOIN entries ON entries.id = list_items.entry_id
               JOIN titles ON titles.id = entries.title_id
               WHERE titles.anilist_id IS NOT NULL AND entries.user_id = ?""",
            (user_id,),
        )
    }
    fav_char_counts = {
        r["entry_id"]: r["c"] for r in conn.execute(
            """SELECT favorite_characters.entry_id, count(*) AS c
               FROM favorite_characters JOIN entries ON entries.id = favorite_characters.entry_id
               JOIN titles ON titles.id = entries.title_id
               WHERE titles.anilist_id IS NOT NULL AND entries.user_id = ? GROUP BY favorite_characters.entry_id""",
            (user_id,),
        )
    }

    last_season_rating = {}
    for r in conn.execute(
        f"""SELECT season_ratings.entry_id, season_ratings.season_number, season_ratings.rating
           FROM season_ratings JOIN entries ON entries.id = season_ratings.entry_id
           JOIN titles ON titles.id = entries.title_id
           WHERE titles.anilist_id IS NOT NULL AND entries.user_id = ?
             AND ({_real_rating_sql("season_ratings")}) IS NOT NULL""",
        (user_id,),
    ):
        prev = last_season_rating.get(r["entry_id"])
        if prev is None or r["season_number"] > prev[0]:
            last_season_rating[r["entry_id"]] = (r["season_number"], r["rating"])

    # Duelos: el pool de /duelo ya se filtra por usuario (list_watched_ids) para las
    # PAREJAS que se enseñan, pero aqui interesa "cuantos duelos lleva jugados CADA
    # entry de este usuario" - entries.user_id acota los item_id relevantes sin
    # necesidad de que la tabla `duels` en si tenga columna de usuario (item_id ya
    # identifica una entry que pertenece a un unico usuario).
    duel_counts = {}
    for r in conn.execute(
        """SELECT winner_id AS id FROM duels
             JOIN entries ON entries.id = duels.winner_id
           WHERE duels.table_name = 'entries' AND entries.user_id = ?
           UNION ALL
           SELECT loser_id AS id FROM duels
             JOIN entries ON entries.id = duels.loser_id
           WHERE duels.table_name = 'entries' AND entries.user_id = ?""",
        (user_id, user_id),
    ):
        duel_counts[r["id"]] = duel_counts.get(r["id"], 0) + 1

    return {
        "titles": titles,
        "episodes": episodes,
        "rewatched_entries": rewatched_entries,
        "listed_entries": listed_entries,
        "fav_char_counts": fav_char_counts,
        "last_season_rating": last_season_rating,
        "duel_counts": duel_counts,
    }




def _reliable(date_str) -> bool:
    return bool(date_str) and date_str >= RELIABLE_DATE_MIN




def _compute_tag_idf(raw) -> dict:
    """IDF de cada tag en la biblioteca (backfilleada, vista o pendiente): un tag en
    el 70% de los titulos informa poco y pesa poco, uno del 4% informa mucho -
    resuelve los "tags parasito" (Action, Comedy...) sin n-gramas ni matrices de
    co-ocurrencia, que con ~900 titulos no tendrian soporte estadistico real."""
    total = len(raw["titles"])
    if not total:
        return {}
    counts = {}
    for t in raw["titles"]:
        for tag in (t["anilist_tags"] or "").split(","):
            if tag:
                counts[tag] = counts.get(tag, 0) + 1
    return {tag: math.log(total / n) for tag, n in counts.items() if n}




def _aggregate_affinity(raw, cfg, exclude_entry_id=None):
    """Núcleo del índice: agrega las señales de apetito y calidad por atributo, las
    pasa a percentil, suaviza calidad hacia el neutro según la confianza y combina con
    media geométrica y castigo al desfase.

    `exclude_entry_id` saca ese título de todos los conteos (leave-one-out del
    backtesting), para que no se prediga a sí mismo con su propia nota."""
    titles = [t for t in raw["titles"] if t["entry_id"] != exclude_entry_id]
    title_by_id = {t["title_id"]: t for t in titles}
    known_ids = {t["anilist_id"] for t in titles}
    prequel_map = {t["anilist_id"]: _split_ids(t["anilist_prequel_ids"]) for t in titles}
    roots = _franchise_roots(known_ids, prequel_map)
    anilist_by_title = {t["title_id"]: t["anilist_id"] for t in titles}

    # Lo marcado como hábito sale del eje apetito pero sigue contando en calidad: la
    # nota es información válida; el volumen, no, como señal de deseo.
    titles_appetite = [t for t in titles if not t["is_habit"]]
    appetite_title_ids = {t["title_id"] for t in titles_appetite}

    episodes_by_title = {}
    for e in raw["episodes"]:
        if e["title_id"] in appetite_title_ids:
            episodes_by_title.setdefault(e["title_id"], []).append(e)

    # "unidades vistas" para volumen/lift: episodios reales de series + 1 por pelicula
    # vista (las pelis no tienen filas en `episodes`, perderian toda señal de consumo
    # si no se cuentan aparte).
    watched_units_by_title = {tid: len(eps) for tid, eps in episodes_by_title.items()}
    for t in titles_appetite:
        if t["type"] == "movie" and t["status"] == "watched" and t["title_id"] not in watched_units_by_title:
            watched_units_by_title[t["title_id"]] = 1

    title_ids_by_attr, franchise_attrs = {}, {}
    for t in titles:
        attrs = _title_attrs(t)
        root = roots.get(t["anilist_id"], t["anilist_id"])
        franchise_attrs.setdefault(root, set()).update(attrs)
        for key in attrs:
            title_ids_by_attr.setdefault(key, []).append(t["title_id"])

    units_by_franchise = {}
    for tid, units in watched_units_by_title.items():
        aid = anilist_by_title.get(tid)
        if aid is None:
            continue
        root = roots.get(aid, aid)
        units_by_franchise[root] = units_by_franchise.get(root, 0) + units

    # --- APETITO ---
    volumen_raw = {}
    for root, attrs in franchise_attrs.items():
        contrib = math.log(1 + min(units_by_franchise.get(root, 0), FRANCHISE_EPISODE_CAP))
        for key in attrs:
            volumen_raw[key] = volumen_raw.get(key, 0.0) + contrib

    total_units = sum(watched_units_by_title.values())
    total_titles = len(titles)
    units_by_attr = {}
    for tid, units in watched_units_by_title.items():
        t = title_by_id.get(tid)
        if not t:
            continue
        for key in _title_attrs(t):
            units_by_attr[key] = units_by_attr.get(key, 0) + units
    lift_raw = {}
    for key, n_attr_titles in title_ids_by_attr.items():
        cuota_catalogo = len(n_attr_titles) / total_titles if total_titles else 0
        cuota_consumo = units_by_attr.get(key, 0) / total_units if total_units else 0
        if cuota_catalogo:
            lift_raw[key] = cuota_consumo / cuota_catalogo

    deltas_by_attr = {}
    for tid, eps in episodes_by_title.items():
        t = title_by_id.get(tid)
        if not t:
            continue
        ordered = sorted(
            (e for e in eps if _reliable(e["watched_at"])),
            key=lambda e: (e["season_number"], e["episode_number"]),
        )
        if len(ordered) < 2:
            continue
        attrs = _title_attrs(t)
        for a, b in zip(ordered, ordered[1:]):
            try:
                d1 = datetime.fromisoformat(a["watched_at"].replace("Z", "+00:00"))
                d2 = datetime.fromisoformat(b["watched_at"].replace("Z", "+00:00"))
            except ValueError:
                continue
            delta_days = abs((d2 - d1).days)
            for key in attrs:
                deltas_by_attr.setdefault(key, []).append(delta_days)
    # Invertido (menos dias entre episodios = mas apetito) - se percentiliza despues,
    # el signo es lo unico que importa aqui.
    ritmo_raw = {key: -statistics.median(deltas) for key, deltas in deltas_by_attr.items()}

    # Día 1: solo en títulos que siguen en emisión (show_status = 'Returning
    # Series'). Uno ya terminado queda sin señal, nunca en negativo.
    dia1_hits, dia1_total = {}, {}
    for tid, eps in episodes_by_title.items():
        t = title_by_id.get(tid)
        if not t or t["show_status"] != "Returning Series":
            continue
        attrs = _title_attrs(t)
        for e in eps:
            if not _reliable(e["watched_at"]) or not _reliable((e["air_date"] or "")[:10]):
                continue
            try:
                watched_d = datetime.fromisoformat(e["watched_at"].replace("Z", "+00:00")).date()
                air_d = date.fromisoformat(e["air_date"][:10])
            except ValueError:
                continue
            within = abs((watched_d - air_d).days) <= 7
            for key in attrs:
                dia1_total[key] = dia1_total.get(key, 0) + 1
                if within:
                    dia1_hits[key] = dia1_hits.get(key, 0) + 1
    dia1_raw = {k: dia1_hits.get(k, 0) / n for k, n in dia1_total.items() if n >= 3}

    watched_by_attr, rewatched_by_attr = {}, {}
    for t in titles_appetite:
        if t["status"] != "watched":
            continue
        for key in _title_attrs(t):
            watched_by_attr.setdefault(key, set()).add(t["entry_id"])
            if t["entry_id"] in raw["rewatched_entries"]:
                rewatched_by_attr.setdefault(key, set()).add(t["entry_id"])
    rewatch_raw = {
        key: len(rewatched_by_attr.get(key, set())) / len(entries)
        for key, entries in watched_by_attr.items() if len(entries) >= 2
    }

    curacion_raw = {}
    for t in titles_appetite:
        score = int(t["entry_id"] in raw["listed_entries"])
        score += raw["fav_char_counts"].get(t["entry_id"], 0)
        score += sum(1 for e in episodes_by_title.get(t["title_id"], []) if e["is_favorite"])
        if not score:
            continue
        for key in _title_attrs(t):
            curacion_raw[key] = curacion_raw.get(key, 0) + score

    # --- CALIDAD ---
    rated_by_attr, disfrute_by_attr = {}, {}
    for t in titles:
        if t["rating"] is None:
            continue
        for key in _title_attrs(t):
            rated_by_attr.setdefault(key, []).append(t["rating"])
        if t["cat_disfrute"] is not None:
            for key in _title_attrs(t):
                disfrute_by_attr.setdefault(key, []).append(t["cat_disfrute"])
    techo_raw = {k: _percentile(v, 85) for k, v in rated_by_attr.items()}
    suelo_raw = {k: _percentile(v, 25) for k, v in rated_by_attr.items()}
    media_raw = {k: sum(v) / len(v) for k, v in rated_by_attr.items()}
    disfrute_raw = {k: sum(v) / len(v) for k, v in disfrute_by_attr.items()}

    elo_candidates = [
        t for t in titles
        if t["status"] == "watched" and t["rating"] is not None
        and raw["duel_counts"].get(t["entry_id"], 0) >= 5
    ]
    elo_raw = {}
    if len(elo_candidates) >= 4:
        n = len(elo_candidates)
        by_rating_rank = {
            t["entry_id"]: i for i, t in enumerate(sorted(elo_candidates, key=lambda x: -x["rating"]))
        }
        by_elo_rank = {
            t["entry_id"]: i for i, t in enumerate(sorted(elo_candidates, key=lambda x: -(x["elo"] or 1500)))
        }
        diffs_by_attr = {}
        for t in elo_candidates:
            diff = (by_rating_rank[t["entry_id"]] - by_elo_rank[t["entry_id"]]) / n
            for key in _title_attrs(t):
                diffs_by_attr.setdefault(key, []).append(diff)
        elo_raw = {k: sum(v) / len(v) for k, v in diffs_by_attr.items()}

    # --- normalizar todo a percentil (0-100) comparando cada atributo con el resto ---
    appetite_signals = {
        "volumen": _percentile_rank(volumen_raw), "lift": _percentile_rank(lift_raw),
        "ritmo": _percentile_rank(ritmo_raw), "dia1": _percentile_rank(dia1_raw),
        "rewatch": _percentile_rank(rewatch_raw), "curacion": _percentile_rank(curacion_raw),
    }
    quality_signals_pct = {
        "techo": _percentile_rank(techo_raw), "suelo": _percentile_rank(suelo_raw),
        "disfrute": _percentile_rank(disfrute_raw), "media": _percentile_rank(media_raw),
        "elo": _percentile_rank(elo_raw),
    }
    quality_support_n = {
        "techo": {k: len(v) for k, v in rated_by_attr.items()},
        "suelo": {k: len(v) for k, v in rated_by_attr.items()},
        "media": {k: len(v) for k, v in rated_by_attr.items()},
        "disfrute": {k: len(v) for k, v in disfrute_by_attr.items()},
        "elo": {k: len(v) for k, v in (diffs_by_attr if elo_raw else {}).items()},
    }
    # Suavizado bayesiano SOLO en calidad (son promedios/percentiles de notas, donde
    # el shrinkage clasico hacia el neutro tiene sentido); apetito son conteos/ratios
    # de consumo, ya moderados por volumen de datos al percentilizar.
    quality_signals = {}
    for sig, pct_dict in quality_signals_pct.items():
        support = quality_support_n.get(sig, {})
        smoothed = {}
        for key, pct in pct_dict.items():
            n = support.get(key, 0)
            w = n / (n + AFFINITY_CONFIDENCE_K) if n else 0.0
            smoothed[key] = pct * w + 50.0 * (1 - w)
        quality_signals[sig] = smoothed

    def _combine(signals: dict, weights: dict) -> dict:
        all_keys = set()
        for d in signals.values():
            all_keys.update(d.keys())
        out = {}
        for key in all_keys:
            parts = [(signals[sig][key], w) for sig, w in weights.items() if key in signals[sig]]
            if not parts:
                continue
            total_w = sum(w for _, w in parts)
            out[key] = sum(v * w for v, w in parts) / total_w
        return out

    apetito = _combine(appetite_signals, cfg["appetite_weights"])
    calidad = _combine(quality_signals, cfg["quality_weights"])

    all_attrs = set(apetito) | set(calidad) | set(title_ids_by_attr)
    result = {}
    for key in all_attrs:
        n_titles = len(title_ids_by_attr.get(key, []))
        ap = apetito.get(key, 50.0)
        ca = calidad.get(key, 50.0)
        base = (ap ** AFFINITY_APPETITE_EXP) * (ca ** AFFINITY_QUALITY_EXP)
        gap = max(0.0, ap - ca - cfg["gap_threshold"])
        confianza = n_titles / (n_titles + AFFINITY_CONFIDENCE_K) if n_titles else 0.0
        # Con pocos títulos la afinidad se encoge hacia el neutro (50), no hacia 0:
        # "pocos datos de este tag" no significa "este tag no te gusta".
        afinidad = base * (1 - cfg["gap_factor"] * gap / 100)
        afinidad = afinidad * confianza + 50.0 * (1 - confianza)
        result[key] = {
            "sig_volumen": volumen_raw.get(key), "sig_lift": lift_raw.get(key),
            "sig_ritmo": ritmo_raw.get(key), "sig_dia1": dia1_raw.get(key),
            "sig_rewatch": rewatch_raw.get(key), "sig_curacion": curacion_raw.get(key),
            "sig_techo": techo_raw.get(key), "sig_suelo": suelo_raw.get(key),
            "sig_disfrute": disfrute_raw.get(key), "sig_media": media_raw.get(key),
            "sig_elo": elo_raw.get(key),
            "apetito": ap, "calidad": ca, "inercia": ap - ca,
            "afinidad": max(0.0, min(100.0, afinidad)), "confianza": confianza,
            "n_titulos": n_titles,
            "n_episodios": sum(len(episodes_by_title.get(tid, [])) for tid in title_ids_by_attr.get(key, [])),
        }
    return result




def _profile_dict_from_rows(rows: dict, raw, cfg, exclude_entry_id=None) -> dict:
    """Construye el dict que espera predict_score a partir de las filas ya agregadas
    (de _aggregate_affinity o leidas de la tabla taste_profile)."""
    genre_avg, tag_avg, studio_avg = {}, {}, {}
    target_by_type = {"genre": genre_avg, "tag": tag_avg, "studio": studio_avg}
    for (attr_type, attr_name), row in rows.items():
        target = target_by_type.get(attr_type)
        if target is not None:
            target[attr_name] = row["afinidad"]

    rating_by_id, last_rating_by_id = {}, {}
    for t in raw["titles"]:
        if t["entry_id"] == exclude_entry_id or t["rating"] is None:
            continue
        rating_by_id[t["anilist_id"]] = t["rating"]
        last = raw["last_season_rating"].get(t["entry_id"])
        last_rating_by_id[t["anilist_id"]] = last[1] if last else t["rating"]

    return {
        "genre_avg": genre_avg, "tag_avg": tag_avg, "studio_avg": studio_avg,
        "tag_idf": _compute_tag_idf(raw), "rating_by_id": rating_by_id,
        "last_rating_by_id": last_rating_by_id, "predict_weights": cfg["predict_weights"],
        "score_distribution": [],
    }




def _backtest_leave_one_out(raw, cfg) -> list[tuple[float, float]]:
    """Para cada título puntuado, su score predicho con un perfil que lo excluye.
    Devuelve pares (score_crudo, nota_real_0_10). Lo usan recompute_taste_profile
    (score_distribution) y scripts/tune_affinity_weights.py."""
    pairs = []
    for t in raw["titles"]:
        if t["rating"] is None:
            continue
        loo_rows = _aggregate_affinity(raw, cfg, exclude_entry_id=t["entry_id"])
        loo_profile = _profile_dict_from_rows(loo_rows, raw, cfg, exclude_entry_id=t["entry_id"])
        raw_score, _confidence = _raw_predict_score(_cached_predict_item(t), loo_profile)
        if raw_score is not None:
            pairs.append((raw_score, t["rating"]))
    return pairs




def get_recompute_status(conn, user_id):
    """Estado del recálculo del índice de este usuario, para el indicador de /ajustes
    y /calendario/anual."""
    return {
        "running": get_setting(conn, f"affinity_recompute_running:{user_id}") == "1",
        "finished_at": get_setting(conn, f"affinity_recompute_finished_at:{user_id}"),
    }




def set_recompute_running(conn, user_id, running: bool):
    set_setting(conn, f"affinity_recompute_running:{user_id}", "1" if running else "0")
    if not running:
        set_setting(
            conn, f"affinity_recompute_finished_at:{user_id}",
            datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )




def recompute_taste_profile(conn, user_id):
    """Recálculo completo del índice de afinidad de este usuario (pesado). Solo se
    lanza desde la sync de fondo y el botón "Actualizar perfil de gustos"; las páginas
    leen taste_profile con build_taste_profile."""
    cfg = get_affinity_config(conn, user_id)
    raw = _load_affinity_raw(conn, user_id)
    rows = _aggregate_affinity(raw, cfg)

    conn.execute("DELETE FROM taste_profile WHERE user_id = ?", (user_id,))
    for (attr_type, attr_name), r in rows.items():
        conn.execute(
            """INSERT INTO taste_profile
               (user_id, attr_type, attr_name, sig_volumen, sig_lift, sig_ritmo, sig_dia1,
                sig_rewatch, sig_curacion, sig_techo, sig_suelo, sig_disfrute,
                sig_media, sig_elo, apetito, calidad, inercia, afinidad, confianza,
                n_titulos, n_episodios)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                user_id, attr_type, attr_name, r["sig_volumen"], r["sig_lift"], r["sig_ritmo"],
                r["sig_dia1"], r["sig_rewatch"], r["sig_curacion"], r["sig_techo"],
                r["sig_suelo"], r["sig_disfrute"], r["sig_media"], r["sig_elo"],
                r["apetito"], r["calidad"], r["inercia"], r["afinidad"], r["confianza"],
                r["n_titulos"], r["n_episodios"],
            ),
        )
    # Commit intermedio: el backtesting de abajo tarda del orden de un minuto y no
    # toca la BBDD para nada (opera solo sobre `raw`, ya cargado en memoria) - sin
    # este commit, la transaccion seguiria abierta todo ese rato y bloquearia
    # cualquier otra escritura (p.ej. guardar los pesos en /ajustes) con
    # "database is locked", igual que sync_library evita con una conexion por titulo.
    conn.commit()

    pairs = _backtest_leave_one_out(raw, cfg)
    scores = sorted(s for s, _rating in pairs)

    conn.execute("DELETE FROM score_distribution WHERE user_id = ?", (user_id,))
    conn.executemany(
        "INSERT INTO score_distribution (user_id, raw_score) VALUES (?, ?)", [(user_id, s) for s in scores]
    )
    _save_accuracy(conn, user_id, pairs, scores)
    conn.commit()
    return len(rows), len(scores)


ACCURACY_MASTERPIECE_RATING = 9.5  # a partir de aqui una nota cuenta como "obra maestra"
ACCURACY_HISTORY_DAYS = 90         # puntos (uno por dia) que se guardan del historico
ACCURACY_BOOTSTRAP_ROUNDS = 200    # remuestreos para el margen de ruido del acierto


def _spearman_band(pairs, rounds=ACCURACY_BOOTSTRAP_ROUNDS):
    """Margen de ruido del acierto (percentiles 5-95 de un bootstrap): cuanto se
    moveria el numero solo por azar con estas mismas notas. Un cambio del motor que
    no saque el acierto de esta franja no se distingue del ruido. Semilla fija, asi
    el margen no baila entre recalculos con los mismos datos."""
    rng = random.Random(0)
    values = []
    for _ in range(rounds):
        sample = [pairs[rng.randrange(len(pairs))] for _ in pairs]
        rho = _spearman([s for s, _ in sample], [r for _, r in sample])
        if rho is not None:
            values.append(rho)
    if len(values) < 10:
        return None, None
    values.sort()
    return values[int(len(values) * 0.05)], values[int(len(values) * 0.95) - 1]


def _spearman(xs, ys):
    """Correlacion de rangos (con empates promediados): 0 = azar, 1 = mismo orden."""
    def ranks(values):
        order = sorted(range(len(values)), key=lambda i: values[i])
        out = [0.0] * len(values)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
                j += 1
            for k in range(i, j + 1):
                out[order[k]] = (i + j) / 2
            i = j + 1
        return out
    if len(xs) < 3:
        return None
    rx, ry = ranks(xs), ranks(ys)
    mx, my = sum(rx) / len(rx), sum(ry) / len(ry)
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    var = (sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry)) ** 0.5
    return cov / var if var else None


def _save_accuracy(conn, user_id, pairs, scores):
    """Cuánto acierta el motor, con el mismo leave-one-out que puebla
    score_distribution (cada título predicho sin ver su propia nota). Un punto por día
    en app_settings, para /afinidad/calibracion."""
    if len(pairs) < 3:
        return
    pct = [bisect.bisect_right(scores, s) / len(scores) * 100 for s, _rating in pairs]
    top = [p for p, (_s, rating) in zip(pct, pairs) if rating >= ACCURACY_MASTERPIECE_RATING]
    rho = _spearman([s for s, _ in pairs], [r for _, r in pairs])
    lo, hi = _spearman_band(pairs)
    point = {
        "date": date.today().isoformat(),
        "n": len(pairs),
        "spearman": round(rho, 3) if rho is not None else None,
        "spearman_lo": round(lo, 3) if lo is not None else None,
        "spearman_hi": round(hi, 3) if hi is not None else None,
        "masterpieces": len(top),
        "masterpieces_hit": sum(1 for p in top if p >= 50),
    }
    key = f"affinity_accuracy:{user_id}"
    history = [h for h in json.loads(get_setting(conn, key) or "[]") if h.get("date") != point["date"]]
    history.append(point)
    set_setting(conn, key, json.dumps(history[-ACCURACY_HISTORY_DAYS:]))


def get_accuracy_history(conn, user_id):
    """Historico de _save_accuracy, del mas antiguo al mas reciente."""
    try:
        return json.loads(get_setting(conn, f"affinity_accuracy:{user_id}") or "[]")
    except ValueError:
        return []




def build_taste_profile(conn, user_id):
    """Lee el indice de afinidad de ESTE usuario YA PRECALCULADO (tabla taste_profile,
    ver recompute_taste_profile) - el pipeline completo es demasiado pesado para una
    peticion HTTP, esto son solo lecturas rapidas. Si la tabla esta vacia para este
    usuario (recien creada la cuenta, antes del primer ciclo de sync), devuelve un
    perfil sin señal: predict_score cae a None en vez de fallar."""
    cfg = get_affinity_config(conn, user_id)
    rows = conn.execute("SELECT * FROM taste_profile WHERE user_id = ?", (user_id,)).fetchall()
    genre_avg, tag_avg, studio_avg = {}, {}, {}
    genre_conf, tag_conf, studio_conf = {}, {}, {}
    targets = {
        "genre": (genre_avg, genre_conf), "tag": (tag_avg, tag_conf), "studio": (studio_avg, studio_conf),
    }
    for r in rows:
        target = targets.get(r["attr_type"])
        if target:
            avg_d, conf_d = target
            avg_d[r["attr_name"]] = r["afinidad"]
            conf_d[r["attr_name"]] = r["confianza"]

    ratings = conn.execute(
        f"""SELECT entries.id AS entry_id, titles.anilist_id, {_real_rating_sql("entries")} AS rating
           FROM entries JOIN titles ON titles.id = entries.title_id
           WHERE ({_real_rating_sql("entries")}) IS NOT NULL AND entries.user_id = ?
             AND titles.anilist_id IS NOT NULL""",
        (user_id,),
    ).fetchall()
    last_season = {}
    for r in conn.execute(
        f"""SELECT season_ratings.entry_id, season_ratings.season_number, season_ratings.rating
           FROM season_ratings JOIN entries ON entries.id = season_ratings.entry_id
           WHERE ({_real_rating_sql("season_ratings")}) IS NOT NULL AND entries.user_id = ?""",
        (user_id,),
    ):
        prev = last_season.get(r["entry_id"])
        if prev is None or r["season_number"] > prev[0]:
            last_season[r["entry_id"]] = (r["season_number"], r["rating"])

    rating_by_id, last_rating_by_id, all_ratings = {}, {}, []
    for r in ratings:
        rating_by_id[r["anilist_id"]] = r["rating"]
        last = last_season.get(r["entry_id"])
        last_rating_by_id[r["anilist_id"]] = last[1] if last else r["rating"]
        all_ratings.append(r["rating"])

    # IDF de tags sobre el CATALOGO que este usuario trackea (visto o pendiente), no
    # sobre toda la tabla titles (que incluye lo que cualquier otro usuario cachee) -
    # mismo criterio que _compute_tag_idf ya usaba sobre raw["titles"].
    tag_counts, tag_total = {}, 0
    for r in conn.execute(
        """SELECT DISTINCT titles.id, titles.anilist_tags FROM titles
           JOIN entries ON entries.title_id = titles.id
           WHERE titles.anilist_id IS NOT NULL AND entries.user_id = ?""",
        (user_id,),
    ):
        tag_total += 1
        for tag in (r["anilist_tags"] or "").split(","):
            if tag:
                tag_counts[tag] = tag_counts.get(tag, 0) + 1
    tag_idf = {tag: math.log(tag_total / n) for tag, n in tag_counts.items() if tag_total and n}

    dist = [
        r["raw_score"] for r in conn.execute(
            "SELECT raw_score FROM score_distribution WHERE user_id = ? ORDER BY raw_score", (user_id,)
        )
    ]

    return {
        "genre_avg": genre_avg, "tag_avg": tag_avg, "studio_avg": studio_avg,
        "genre_conf": genre_conf, "tag_conf": tag_conf, "studio_conf": studio_conf,
        "tag_idf": tag_idf, "rating_by_id": rating_by_id, "last_rating_by_id": last_rating_by_id,
        "predict_weights": cfg["predict_weights"], "score_distribution": dist,
        "covered": len(ratings), "global_avg": sum(all_ratings) / len(all_ratings) if all_ratings else 7.0,
    }




def _raw_predict_score(item: dict, profile: dict):
    """Combina las señales del candidato en escala 0-100 (precuela > cross-rec =
    estudio > tags > género, ver PREDICT_SIGNAL_WEIGHTS; tags ponderados por IDF).
    Devuelve (score_crudo o None, confianza 0-1 = qué parte del peso total tenían las
    señales disponibles)."""
    w = profile["predict_weights"]
    parts = []  # (valor_0_100, peso)

    for pid in item.get("prequel_ids", []):
        if pid in profile["last_rating_by_id"]:
            parts.append((profile["last_rating_by_id"][pid] * 10, w["precuela"]))
            break

    cross_ratings = [
        profile["rating_by_id"][cid] for cid in item.get("cross_rec_ids", [])
        if cid in profile["rating_by_id"]
    ]
    if cross_ratings:
        parts.append((sum(cross_ratings) / len(cross_ratings) * 10, w["cross_rec"]))

    if item.get("studio") and item["studio"] in profile["studio_avg"]:
        parts.append((profile["studio_avg"][item["studio"]], w["estudio"]))

    tag_contribs = [
        (profile["tag_avg"][name], rank * profile["tag_idf"].get(name, 1.0))
        for name, rank in item.get("tags", [])
        if name in profile["tag_avg"]
    ]
    total_tag_w = sum(cw for _, cw in tag_contribs)
    if total_tag_w > 0:
        parts.append((sum(v * cw for v, cw in tag_contribs) / total_tag_w, w["tags"]))

    genre_ratings = [profile["genre_avg"][g] for g in item.get("genres", []) if g in profile["genre_avg"]]
    if genre_ratings:
        parts.append((sum(genre_ratings) / len(genre_ratings), w["genero"]))

    if not parts:
        return None, 0.0
    total_weight = sum(pw for _, pw in parts)
    raw_score = sum(v * pw for v, pw in parts) / total_weight
    max_weight = sum(w.values())
    return raw_score, (total_weight / max_weight if max_weight else 0.0)




def _calibrate_percentile(profile: dict, raw_score: float) -> int:
    """Pasa el score crudo a percentil de la distribución backtesteada de la propia
    biblioteca: un 95% significa "por encima del 95% de lo que has visto". Sin
    distribución todavía, devuelve el score crudo redondeado."""
    dist = profile.get("score_distribution")
    if not dist:
        return round(raw_score)
    idx = bisect.bisect_right(dist, raw_score)
    return round(idx / len(dist) * 100)




def predict_score(item: dict, profile: dict):
    """'% que te gustará', calibrado a percentil. None si no hay ninguna señal."""
    raw_score, _confidence = _raw_predict_score(item, profile)
    if raw_score is None:
        return None
    return _calibrate_percentile(profile, raw_score)




def predict_score_detail(item: dict, profile: dict):
    """Como predict_score, pero también devuelve la confianza del título."""
    raw_score, confidence = _raw_predict_score(item, profile)
    if raw_score is None:
        return None
    return {"score": _calibrate_percentile(profile, raw_score), "confidence": round(confidence * 100)}




def _cached_predict_item(title_row) -> dict:
    """Convierte una fila de titles/entries (con las columnas anilist_* YA
    cacheadas por backfill_anilist_profile) al formato que espera predict_score -
    para predecir sobre la biblioteca (pendientes) sin llamar a AniList en cada
    carga de pagina. A diferencia de un candidato en vivo del calendario, aqui no
    hay rank guardado por tag (solo se guarda para candidatos, ver anime.py) -
    se trata cada tag emparejado con el mismo peso (100), algo mas simple que la
    ponderacion fina del calendario pero sigue siendo una señal real."""
    return {
        "genres": [g for g in (title_row["anilist_genres"] or "").split(",") if g],
        "studio": title_row["anilist_studio"],
        "tags": [(t, 100) for t in (title_row["anilist_tags"] or "").split(",") if t],
        "prequel_ids": [int(i) for i in (title_row["anilist_prequel_ids"] or "").split(",") if i],
        "cross_rec_ids": [int(i) for i in (title_row["anilist_cross_rec_ids"] or "").split(",") if i],
    }




def add_predictions(conn, user_id, entries, profile=None):
    """Añade `predict` (0-100 o None) a cada entry con el perfil de este usuario y los
    campos anilist_* ya cacheados. `entries` debe traer esas columnas (ver
    list_pending). Devuelve dicts nuevos (sqlite3.Row no admite claves nuevas)."""
    profile = profile or build_taste_profile(conn, user_id)
    out = []
    for e in entries:
        d = dict(e)
        detail = predict_score_detail(_cached_predict_item(d), profile) if d.get("anilist_id") else None
        d["predict"] = detail["score"] if detail else None
        d["predict_confidence"] = detail["confidence"] if detail else None
        out.append(d)
    return out




def get_affinity_display(conn, user_id, min_titles: int = 3, limit: int = 15):
    """Desglose del índice de afinidad de este usuario para /estadisticas. Solo lee
    taste_profile. min_titles descarta atributos con muy pocos títulos."""
    rows = conn.execute(
        "SELECT * FROM taste_profile WHERE user_id = ? AND n_titulos >= ? ORDER BY afinidad DESC",
        (user_id, min_titles),
    ).fetchall()

    def _fmt(r):
        detalle = []
        if r["sig_lift"] is not None:
            detalle.append(f"{r['sig_lift']:.1f}x lo esperado")
        if r["sig_ritmo"] is not None:
            detalle.append(f"ritmo {-r['sig_ritmo']:.1f} días/episodio")
        if r["sig_rewatch"] is not None:
            detalle.append(f"{round(r['sig_rewatch'] * 100)}% con rewatch")
        if r["sig_dia1"] is not None:
            detalle.append(f"{round(r['sig_dia1'] * 100)}% visto en emisión")
        if r["sig_curacion"] is not None:
            detalle.append(f"curación {round(r['sig_curacion'])}")
        return {
            "type": r["attr_type"], "name": r["attr_name"],
            "afinidad": round(r["afinidad"]), "apetito": round(r["apetito"]),
            "calidad": round(r["calidad"]), "inercia": round(r["inercia"]),
            "confianza": round(r["confianza"] * 100),
            "techo": r["sig_techo"], "suelo": r["sig_suelo"], "media": r["sig_media"],
            "n_titulos": r["n_titulos"], "n_episodios": r["n_episodios"],
            "detalle": " · ".join(detalle),
        }

    by_type = {"genre": [], "tag": [], "studio": []}
    for r in rows:
        if r["attr_type"] in by_type:
            by_type[r["attr_type"]].append(_fmt(r))
    # Orden por |inercia|: aquí interesa la magnitud del desequilibrio entre apetito y
    # calidad, no el score combinado.
    for tipo in by_type:
        by_type[tipo].sort(key=lambda a: abs(a["inercia"]), reverse=True)

    covered = conn.execute(
        """SELECT count(*) FROM entries JOIN titles ON titles.id = entries.title_id
           WHERE entries.rating IS NOT NULL AND entries.user_id = ? AND titles.anilist_id IS NOT NULL""",
        (user_id,),
    ).fetchone()[0]

    # Top por |inercia| de las tres categorías juntas, para la barra divergente.
    TIPO_ES = {"genre": "Género", "tag": "Tag", "studio": "Estudio"}
    combinados = [_fmt(r) for r in rows if r["attr_type"] in TIPO_ES]
    top_inercia = sorted(combinados, key=lambda a: abs(a["inercia"]), reverse=True)[:12]
    for a in top_inercia:
        a["tipo_es"] = TIPO_ES[a["type"]]
    max_inercia = max((abs(a["inercia"]) for a in top_inercia), default=1) or 1

    return {
        "genres": by_type["genre"],
        "tags": by_type["tag"][:limit],
        "studios": by_type["studio"][:limit],
        "covered": covered,
        "top_inercia": top_inercia,
        "max_inercia": max_inercia,
    }
