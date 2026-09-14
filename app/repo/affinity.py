"""app.repo.affinity - extraido de repo.py en el split de modulos (ronda 2026-08-21).
Ver app/repo/__init__.py para el mapa completo de que vive en cada fichero -
el resto del proyecto sigue usando `from app import repo; repo.funcion(...)`
exactamente igual que antes, este split es puramente interno."""
import bisect
import json
import math
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
# anime.match_anilist_basic) se cuenta igual que un "no existe" de verdad, y varias
# pasadas en dias distintos dan margen a que fuera solo mala suerte de rate-limit.
MAX_ANILIST_MATCH_ATTEMPTS = 5



_ANILIST_BACKFILL_FILTER_SQL = """
    entries.status IN ('watched', 'pending')
    AND (titles.anilist_tags IS NULL OR titles.anilist_title_romaji IS NULL)
    AND titles.anilist_match_attempts < {max_attempts}
""".format(max_attempts=MAX_ANILIST_MATCH_ATTEMPTS)




def backfill_anilist_profile(conn):
    """Rellena anilist_id/genero/estudio/tags/precuelas/recomendaciones para todo el
    anime de la biblioteca (visto O pendiente) que aun no lo tiene. Visto hace falta
    para el indice de afinidad (build_taste_profile, get_affinity_display);
    pendiente hace falta para poder predecir "% que te
    gustara" tambien sobre lo que YA tienes sin ver, no solo sobre el calendario de
    temporada (Tara, 2026-08-13: "aplicar la prediccion a mis pendientes"). Boton
    manual en /calendario/anual, no automatico - son cientos de llamadas a AniList la
    primera vez, no tiene sentido colarlo en la sync de fondo de 12h sin que Tara sepa
    que tarda. Filtra por `anilist_tags IS NULL OR anilist_title_romaji IS NULL` (no
    solo `anilist_id IS NULL`) para que un titulo ya emparejado ANTES de que se
    guardaran tags/relaciones/titulo-romaji tambien se reprocese una vez - sin esto se
    habria quedado incompleto para siempre.

    Busca primero por `original_title` (el name/title SIN traducir de TMDB) y solo si
    eso falla prueba con `title` (el es-ES cacheado) - AniList no indexa titulos en
    español, asi que un titulo cuyo unico nombre conocido fuera "Caballeros de
    Sidonia" nunca encontraba "Knights of Sidonia" por mucho que se reintentase
    (confirmado en vivo, 2026-08-14: search literal 404 con el nombre en español,
    match perfecto con el nombre original). Si `original_title` aun no esta cacheado
    (columna añadida despues de que estos titulos ya existieran), se pide a TMDB al
    vuelo y se guarda, para no depender de esperar al siguiente refresh_metadata.

    Cada fallo (los dos candidatos sin match) suma 1 a `anilist_match_attempts`; al
    llegar a MAX_ANILIST_MATCH_ATTEMPTS el titulo deja de aparecer aqui - Tara,
    2026-08-14: "que si al intentar 5 veces no lo mete, pues que no lo mete y ya",
    para no reintentar para siempre lo que genuinamente no tiene entrada en AniList
    (recopilatorias/especiales con titulos japoneses muy especificos, altas manuales
    sin AniList real)."""
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
                   anilist_title_romaji = ?, anilist_title_english = ? WHERE id = ?""",
                (
                    match["anilist_id"], ",".join(match.get("genres") or []), match.get("studio"),
                    ",".join(match.get("tags") or []),
                    ",".join(str(i) for i in match.get("prequel_ids") or []),
                    ",".join(str(i) for i in match.get("cross_rec_ids") or []),
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




def snapshot_profile_progress(conn):
    """Foto diaria de cuanto perfil de gustos hay construido (Tara, 2026-08-13:
    "que me gustaria ver como va evolucionando cada dia que use la herramienta") -
    INSERT OR IGNORE por fecha, asi que solo se guarda la primera vez que se llama
    cada dia, no importa cuantas veces se visite la app. Llamado desde /pendientes
    (la pantalla de siempre) para que quede una foto solo con usar la app con
    normalidad, sin necesitar un cron aparte."""
    covered = conn.execute(
        """SELECT count(*) FROM titles JOIN entries ON entries.title_id = titles.id
           WHERE entries.rating IS NOT NULL AND titles.anilist_id IS NOT NULL"""
    ).fetchone()[0]
    total_rated = conn.execute(
        f"""SELECT count(*) FROM titles JOIN entries ON entries.title_id = titles.id
           WHERE entries.rating IS NOT NULL AND {_IS_ANIME_SQL}"""
    ).fetchone()[0]
    today = datetime.now(timezone.utc).date().isoformat()
    conn.execute(
        "INSERT OR IGNORE INTO profile_history (date, covered, total_rated) VALUES (?, ?, ?)",
        (today, covered, total_rated),
    )




def get_profile_history(conn, limit: int = 30):
    """Historial de snapshots, mas reciente primero - para el grafico de barras de
    /ajustes."""
    return conn.execute(
        "SELECT * FROM profile_history ORDER BY date DESC LIMIT ?", (limit,)
    ).fetchall()




def count_anilist_backfill_pending(conn) -> int:
    """Cuantos titulos de la biblioteca (anime, vistos o pendientes) le falta al boton
    "Actualizar perfil de gustos" de /calendario/anual por hacer. Mismo filtro exacto
    que backfill_anilist_profile (_ANILIST_BACKFILL_FILTER_SQL), incluido el tope de
    intentos - sin esto el contador se quedaba pegado en un numero que nunca bajaba
    (Tara, 2026-08-14: "me pone que faltan 97 pero no baja nunca") con titulos que
    genuinamente no tienen match en AniList y se reintentaban para siempre."""
    return conn.execute(
        f"""SELECT count(*) FROM titles JOIN entries ON entries.title_id = titles.id
           WHERE {_ANILIST_BACKFILL_FILTER_SQL} AND {_IS_ANIME_SQL}"""
    ).fetchone()[0]





# ============================================================================
# INDICE DE AFINIDAD (encargo 2026-08-14, sustituye al perfil de "nota media"
# de mas arriba) - ver PROMPTtaratrackimplementacion.md, Bloque 2.
#
# Dos ejes por atributo (genero/tag/estudio): APETITO (cuanto lo buscas: volumen,
# lift, ritmo, dia1, rewatch, curacion) y CALIDAD (cuanto te llega cuando acierta:
# techo/suelo/disfrute/media/correccion Elo). Se combinan con media geometrica y
# castigo al desfase, NO con suma ponderada - un shonen larguisimo con nota media
# (apetito alto, calidad baja) debe desinflarse, no promediarse hacia arriba.
#
# El calculo completo (franquicias, ritmo con fechas reales, elo...) es pesado y
# vive en `recompute_taste_profile`, llamado SOLO desde la sync de fondo (cada 12h)
# y el boton manual - las paginas normales solo LEEN la tabla `taste_profile` via
# `build_taste_profile`, nunca recalculan en caliente.
# ============================================================================

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


# Confianza minima para que un titulo cuente como "Candidato a obra maestra" en el
# calendario de temporada (no solo para el numero, tambien para el marco dorado y la
# etiqueta). Un titulo con SOLO señal de genero (el mas generico y debil, peso 1.0 de
# 13.5 = ~7.4% de confianza) puede llegar a 100% si su unico genero coincide con el
# que mas le gusta a Tara - eso no es un "candidato a obra maestra" de verdad, es una
# coincidencia de un solo dato. 10 excluye limpiamente genero-solo (~7%) pero deja
# pasar estudio-solo (~15%), tags-solo (~26%) o precuela (~37%) - "soporte real" tal
# como lo pidio Tara (2026-08-14, "que no sea solo un genero suelto").
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




def get_affinity_config(conn):
    """Pesos del indice de afinidad - constantes con nombre de arriba como default,
    con override opcional guardado en app_settings (JSON, una sola clave) editable
    desde /ajustes (Fase 2 del encargo: "deben quedar en constantes con nombre, en un
    unico sitio, y ser configurables desde /ajustes")."""
    cfg = {k: (dict(v) if isinstance(v, dict) else v) for k, v in _AFFINITY_CONFIG_DEFAULTS.items()}
    raw = get_setting(conn, "affinity_config")
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




def set_affinity_config(conn, cfg: dict):
    set_setting(conn, "affinity_config", json.dumps(cfg))




def reset_affinity_config(conn):
    conn.execute("DELETE FROM app_settings WHERE key = 'affinity_config'")




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
    no tiene apoyo estadistico real, se usa la media de los 3 valores mas extremos en
    la direccion de p (top 3 para p>=50 como el techo, bottom 3 para p<50 como el
    suelo)."""
    if len(values) < 4:
        extreme = sorted(values, reverse=(p >= 50))[:3]
        return sum(extreme) / len(extreme)
    return statistics.quantiles(values, n=100, method="inclusive")[p - 1]




def _percentile_rank(raw: dict) -> dict:
    """Normaliza cada señal a percentil COMPARANDO EL ATRIBUTO CON EL RESTO DE
    ATRIBUTOS del usuario (Fase 2, paso 1) - un 6.67 no dice nada solo, "percentil 35
    en nota, percentil 98 en consumo" si. Rango promedio en empates."""
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




def _load_affinity_raw(conn):
    """Toda la materia prima del indice de afinidad en memoria, en una sola pasada de
    SQL - asi el backtesting de la Fase 4 (recalcular el perfil una vez POR TITULO
    puntuado, excluyendolo de si mismo) no golpea la base de datos cientos de veces,
    solo reagrega en Python sobre estos mismos datos."""
    titles = conn.execute(
        """SELECT entries.id AS entry_id, entries.rating, entries.cat_disfrute,
                  entries.elo, entries.status, entries.is_habit,
                  titles.id AS title_id, titles.type, titles.show_status,
                  titles.anilist_id, titles.anilist_genres, titles.anilist_tags,
                  titles.anilist_studio, titles.anilist_prequel_ids, titles.anilist_cross_rec_ids
           FROM entries JOIN titles ON titles.id = entries.title_id
           WHERE titles.anilist_id IS NOT NULL"""
    ).fetchall()

    episodes = conn.execute(
        """SELECT episodes.title_id, episodes.season_number, episodes.episode_number,
                  episodes.watched_at, episodes.air_date, episodes.is_favorite
           FROM episodes JOIN titles ON titles.id = episodes.title_id
           WHERE titles.anilist_id IS NOT NULL AND episodes.watched_at IS NOT NULL"""
    ).fetchall()

    rewatched_entries = {
        r["entry_id"] for r in conn.execute(
            """SELECT DISTINCT watch_sessions.entry_id
               FROM watch_sessions JOIN entries ON entries.id = watch_sessions.entry_id
               JOIN titles ON titles.id = entries.title_id
               WHERE titles.anilist_id IS NOT NULL"""
        )
    }
    listed_entries = {
        r["entry_id"] for r in conn.execute(
            """SELECT DISTINCT list_items.entry_id
               FROM list_items JOIN entries ON entries.id = list_items.entry_id
               JOIN titles ON titles.id = entries.title_id
               WHERE titles.anilist_id IS NOT NULL"""
        )
    }
    fav_char_counts = {
        r["entry_id"]: r["c"] for r in conn.execute(
            """SELECT favorite_characters.entry_id, count(*) AS c
               FROM favorite_characters JOIN entries ON entries.id = favorite_characters.entry_id
               JOIN titles ON titles.id = entries.title_id
               WHERE titles.anilist_id IS NOT NULL GROUP BY favorite_characters.entry_id"""
        )
    }

    last_season_rating = {}
    for r in conn.execute(
        """SELECT season_ratings.entry_id, season_ratings.season_number, season_ratings.rating
           FROM season_ratings JOIN entries ON entries.id = season_ratings.entry_id
           JOIN titles ON titles.id = entries.title_id
           WHERE titles.anilist_id IS NOT NULL AND season_ratings.rating IS NOT NULL"""
    ):
        prev = last_season_rating.get(r["entry_id"])
        if prev is None or r["season_number"] > prev[0]:
            last_season_rating[r["entry_id"]] = (r["season_number"], r["rating"])

    duel_counts = {}
    for r in conn.execute(
        "SELECT winner_id AS id FROM duels WHERE table_name = 'entries' "
        "UNION ALL SELECT loser_id AS id FROM duels WHERE table_name = 'entries'"
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
    """El nucleo del indice de afinidad: agrega las señales crudas de apetito/calidad
    por atributo, las percentiliza, suaviza calidad hacia el neutro por confianza, y
    combina con media geometrica + castigo al desfase (Fases 1-2 del encargo).

    `exclude_entry_id`, si se da, saca ese titulo de TODOS los conteos - es el
    leave-one-out del backtesting (Fase 4): sin esto, predecir un titulo con su
    propio perfil se "acordaria" de su propia nota."""
    titles = [t for t in raw["titles"] if t["entry_id"] != exclude_entry_id]
    title_by_id = {t["title_id"]: t for t in titles}
    known_ids = {t["anilist_id"] for t in titles}
    prequel_map = {t["anilist_id"]: _split_ids(t["anilist_prequel_ids"]) for t in titles}
    roots = _franchise_roots(known_ids, prequel_map)
    anilist_by_title = {t["title_id"]: t["anilist_id"] for t in titles}

    # Bloque 3: lo marcado como habito sale del eje APETITO (volumen, lift, ritmo,
    # dia1, rewatch, curacion) pero sigue contando en CALIDAD (techo/suelo/disfrute/
    # media/elo, mas abajo, siguen iterando sobre `titles` sin filtrar) - el encargo
    # es explicito: la nota de One Piece es informacion honesta sobre exigencia, lo
    # que miente es el volumen como señal de deseo.
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

    # Dia 1: solo cuenta si el titulo se seguia en emision de verdad - se aproxima con
    # show_status = 'Returning Series' (misma señal que ya usa el proyecto para "no
    # terminada"), no con la fecha de hoy. Un titulo ya terminado queda simplemente
    # SIN señal de dia1 (nunca en negativo), tal como pide el encargo.
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
        afinidad = base * (1 - cfg["gap_factor"] * gap / 100) * confianza
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
    """Fase 4/6: para cada titulo puntuado, su score crudo predicho con SU PROPIO
    perfil recalculado excluyendose a si mismo (si no, se prediria a si mismo via su
    propia nota) - devuelve pares (score_crudo, nota_real_0_10). Usado tanto para
    poblar `score_distribution` (recompute_taste_profile) como para medir el error de
    una combinacion de pesos candidata (scripts/tune_affinity_weights.py, Fase 6:
    "script que pruebe combinaciones de pesos por fuerza bruta")."""
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




def get_recompute_status(conn):
    """Estado del recalculo del indice de afinidad, para el indicador visible en
    /ajustes y /calendario/anual - lanzarlo (backfill de AniList + recompute_taste_profile,
    hasta varios minutos) redirigia al instante sin ninguna señal de que estuviera
    pasando algo (Tara, 2026-08-14: "como se si realmente esta trabajando")."""
    return {
        "running": get_setting(conn, "affinity_recompute_running") == "1",
        "finished_at": get_setting(conn, "affinity_recompute_finished_at"),
    }




def set_recompute_running(conn, running: bool):
    set_setting(conn, "affinity_recompute_running", "1" if running else "0")
    if not running:
        set_setting(
            conn, "affinity_recompute_finished_at",
            datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )




def recompute_taste_profile(conn):
    """Recalculo COMPLETO del indice de afinidad - pesado a proposito (franquicias,
    ritmo con fechas reales, correccion Elo, backtesting Fase 4), llamado solo desde
    la sync de fondo (cada 12h, ver sync_loop en main.py) y el boton manual
    "Actualizar perfil de gustos". Las paginas normales solo leen `taste_profile` via
    build_taste_profile - nunca disparan este calculo desde una peticion HTTP."""
    cfg = get_affinity_config(conn)
    raw = _load_affinity_raw(conn)
    rows = _aggregate_affinity(raw, cfg)

    conn.execute("DELETE FROM taste_profile")
    for (attr_type, attr_name), r in rows.items():
        conn.execute(
            """INSERT INTO taste_profile
               (attr_type, attr_name, sig_volumen, sig_lift, sig_ritmo, sig_dia1,
                sig_rewatch, sig_curacion, sig_techo, sig_suelo, sig_disfrute,
                sig_media, sig_elo, apetito, calidad, inercia, afinidad, confianza,
                n_titulos, n_episodios)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                attr_type, attr_name, r["sig_volumen"], r["sig_lift"], r["sig_ritmo"],
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

    conn.execute("DELETE FROM score_distribution")
    conn.executemany(
        "INSERT INTO score_distribution (raw_score) VALUES (?)", [(s,) for s in scores]
    )
    conn.commit()
    return len(rows), len(scores)




def build_taste_profile(conn):
    """Lee el indice de afinidad YA PRECALCULADO (tabla taste_profile, ver
    recompute_taste_profile) - el pipeline completo es demasiado pesado para una
    peticion HTTP, esto son solo lecturas rapidas. Si la tabla esta vacia (recien
    desplegado, antes del primer ciclo de sync), devuelve un perfil sin señal:
    predict_score cae a None en vez de fallar."""
    cfg = get_affinity_config(conn)
    rows = conn.execute("SELECT * FROM taste_profile").fetchall()
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
        """SELECT entries.id AS entry_id, titles.anilist_id, entries.rating
           FROM entries JOIN titles ON titles.id = entries.title_id
           WHERE entries.rating IS NOT NULL AND titles.anilist_id IS NOT NULL"""
    ).fetchall()
    last_season = {}
    for r in conn.execute(
        "SELECT entry_id, season_number, rating FROM season_ratings WHERE rating IS NOT NULL"
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

    tag_counts, tag_total = {}, 0
    for r in conn.execute("SELECT anilist_tags FROM titles WHERE anilist_id IS NOT NULL"):
        tag_total += 1
        for tag in (r["anilist_tags"] or "").split(","):
            if tag:
                tag_counts[tag] = tag_counts.get(tag, 0) + 1
    tag_idf = {tag: math.log(tag_total / n) for tag, n in tag_counts.items() if tag_total and n}

    dist = [r["raw_score"] for r in conn.execute("SELECT raw_score FROM score_distribution ORDER BY raw_score")]

    return {
        "genre_avg": genre_avg, "tag_avg": tag_avg, "studio_avg": studio_avg,
        "genre_conf": genre_conf, "tag_conf": tag_conf, "studio_conf": studio_conf,
        "tag_idf": tag_idf, "rating_by_id": rating_by_id, "last_rating_by_id": last_rating_by_id,
        "predict_weights": cfg["predict_weights"], "score_distribution": dist,
        "covered": len(ratings), "global_avg": sum(all_ratings) / len(all_ratings) if all_ratings else 7.0,
    }




def _raw_predict_score(item: dict, profile: dict):
    """Combina las señales del candidato en escala 0-100 (AFINIDAD, no nota media) -
    misma jerarquia de siempre (precuela > cross-rec = estudio > tags > genero), ver
    PREDICT_SIGNAL_WEIGHTS. Tags ponderados tambien por IDF (Fase 3: un tag raro pesa
    mas que uno que esta en medio catalogo). Devuelve (score_crudo o None,
    confianza_del_titulo 0-1 = cuanto peso jerarquico tenian las señales que SI
    existian para este titulo, un 90% con precuela puntuada no vale lo mismo que un
    90% a base de dos generos genericos)."""
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
    """Fase 4: el score crudo no se enseña - se mapea contra la distribucion de
    scores backtested de la propia biblioteca (score_distribution, calculada en
    recompute_taste_profile), asi un 95% significa "por encima del 95% de lo que has
    visto en tu vida", estable, no depende de si la temporada viene buena. Sin
    distribucion aun (recien desplegado) cae al score crudo redondeado."""
    dist = profile.get("score_distribution")
    if not dist:
        return round(raw_score)
    idx = bisect.bisect_right(dist, raw_score)
    return round(idx / len(dist) * 100)




def predict_score(item: dict, profile: dict):
    """'% que te gustara', ya calibrado a percentil de tu propia biblioteca. None si
    no hay NINGUNA señal - nada inventado tipo 50% a ciegas."""
    raw_score, _confidence = _raw_predict_score(item, profile)
    if raw_score is None:
        return None
    return _calibrate_percentile(profile, raw_score)




def predict_score_detail(item: dict, profile: dict):
    """Igual que predict_score pero con la confianza del titulo (Fase 3: "debe verse
    en pantalla") - para las tarjetas que quieran mostrar el desglose."""
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




def add_predictions(conn, entries, profile=None):
    """Añade `predict` (0-100 o None) a cada entry, calculado sobre los campos
    anilist_* ya cacheados en titles - para poder predecir sobre pendientes, no solo
    sobre el calendario de temporada (Tara, 2026-08-13: "aplicar la prediccion a mis
    pendientes"). `entries` debe traer las columnas anilist_* (ver list_pending).
    Devuelve una lista NUEVA de dicts (sqlite3.Row no admite asignar claves nuevas)."""
    profile = profile or build_taste_profile(conn)
    out = []
    for e in entries:
        d = dict(e)
        detail = predict_score_detail(_cached_predict_item(d), profile) if d.get("anilist_id") else None
        d["predict"] = detail["score"] if detail else None
        d["predict_confidence"] = detail["confidence"] if detail else None
        out.append(d)
    return out




def get_affinity_display(conn, min_titles: int = 3, limit: int = 15):
    """Desglose del indice de afinidad para /estadisticas (sustituye a la antigua
    "nota media por genero/tag/estudio" - ver Bloque 2 del encargo 2026-08-14, Fase 7:
    "ningun numero sin su desglose al lado"). Solo LEE `taste_profile`, ya
    precalculado por recompute_taste_profile - nunca recalcula en caliente.
    min_titles filtra el ruido de un atributo con 1-2 titulos sueltos, igual que hacia
    min_count en el desglose anterior."""
    rows = conn.execute(
        "SELECT * FROM taste_profile WHERE n_titulos >= ? ORDER BY afinidad DESC", (min_titles,)
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
    # Orden por |inercia| en vez de afinidad: para "donde tiendo a la costumbre /
    # infravaloro" lo interesante es la MAGNITUD del desequilibrio apetito-calidad,
    # no el score combinado (2026-08-15, tras el intento de dispersion - con datos
    # reales la mayoria de generos se apelotona cerca de la diagonal, ilegible como
    # grafico; en lista ordenada por lo que mas destaca si se lee de un vistazo).
    for tipo in by_type:
        by_type[tipo].sort(key=lambda a: abs(a["inercia"]), reverse=True)

    covered = conn.execute(
        """SELECT count(*) FROM entries JOIN titles ON titles.id = entries.title_id
           WHERE entries.rating IS NOT NULL AND titles.anilist_id IS NOT NULL"""
    ).fetchone()[0]

    # Grafica de un vistazo (Tara, 2026-08-15: "no veo una grafica, le falta algo"):
    # apetito y calidad ya se calculan por separado, pero la lectura util - "esto lo
    # veo por costumbre" vs "esto me gusta mas de lo que lo persigo" - es su diferencia
    # (inercia), no cada numero suelto. Barra divergente con los TOP por |inercia| de
    # las 3 categorias juntas, para no repetir la misma grafica 3 veces.
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
