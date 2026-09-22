"""app.repo.stats - extraido de repo.py en el split de modulos (ronda 2026-08-21).
Ver app/repo/__init__.py para el mapa completo de que vive en cada fichero -
el resto del proyecto sigue usando `from app import repo; repo.funcion(...)`
exactamente igual que antes, este split es puramente interno.

Multiusuario Fase 2 (2026-09-17): toda funcion de aqui filtra por user_id - antes
las estadisticas mezclaban el consumo de TODOS los usuarios de la instancia. Las
referencias a episodes.watched_at/is_favorite (columnas compartidas, ya no
actualizadas desde el motor de episodios) se sustituyen por episode_watches/
episode_user_state, ambas con user_id."""
from datetime import datetime, timezone

from app.repo._shared import (
    _GENRE_ES,
)


def list_favorite_episodes(conn, user_id):
    """Episodios marcados con estrella por ESTE usuario (episode_user_state), agrupados
    por titulo - en /estadisticas solo se veia el CONTADOR (fav_episodes de get_stats),
    sin forma de ver CUALES eran (El usuario, notas.txt: "poder ver los eps favoritos que
    tengo... que no se cuales son")."""
    return conn.execute(
        """SELECT episodes.season_number, episodes.episode_number, episodes.name,
                  titles.title, titles.tmdb_id, titles.type
           FROM episode_user_state
           JOIN episodes ON episodes.id = episode_user_state.episode_id
           JOIN titles ON titles.id = episodes.title_id
           WHERE episode_user_state.user_id = ? AND episode_user_state.is_favorite = 1
           ORDER BY titles.title COLLATE NOCASE, episodes.season_number, episodes.episode_number""",
        (user_id,),
    ).fetchall()




def get_stats(conn, user_id):
    """Numeros para /estadisticas de ESTE usuario. Todo consultas locales, nada de APIs."""
    totals = conn.execute(
        """SELECT
             (SELECT count(*) FROM entries JOIN titles ON titles.id = entries.title_id
               WHERE entries.status = 'watched' AND titles.type = 'show' AND entries.user_id = ?) AS shows_watched,
             (SELECT count(*) FROM entries JOIN titles ON titles.id = entries.title_id
               WHERE entries.status = 'watched' AND titles.type = 'movie' AND entries.user_id = ?) AS movies_watched,
             (SELECT count(*) FROM entries WHERE status = 'pending' AND user_id = ?) AS pending,
             (SELECT count(*) FROM episode_watches WHERE user_id = ?) AS episodes_watched,
             (SELECT count(*) FROM entries WHERE rating IS NOT NULL AND user_id = ?) AS rated,
             (SELECT round(avg(rating), 2) FROM entries WHERE rating IS NOT NULL AND user_id = ?) AS avg_rating,
             (SELECT count(*) FROM favorite_characters JOIN entries ON entries.id = favorite_characters.entry_id
               WHERE entries.user_id = ?) AS waifus,
             (SELECT count(*) FROM episode_user_state WHERE user_id = ? AND is_favorite = 1) AS fav_episodes""",
        (user_id,) * 8,
    ).fetchone()

    # Tiempo total: episodios vistos x minutos/ep de su serie + duracion de las pelis vistas.
    # Cada visionado real (incluidas las re-vistas de un episodio suelto, 2026-08-21)
    # suma su propio tiempo - episode_watches tiene una fila por vez, no por episodio
    # unico.
    minutes = conn.execute(
        """SELECT
             COALESCE((SELECT sum(COALESCE(titles.runtime_minutes, 22))
               FROM episode_watches
               JOIN episodes ON episodes.id = episode_watches.episode_id
               JOIN titles ON titles.id = episodes.title_id
               WHERE episode_watches.user_id = ?), 0)
             + COALESCE((SELECT sum(COALESCE(titles.runtime_minutes, 100))
               FROM entries JOIN titles ON titles.id = entries.title_id
               WHERE entries.status = 'watched' AND titles.type = 'movie' AND entries.user_id = ?), 0) AS total""",
        (user_id, user_id),
    ).fetchone()["total"]

    histogram = {
        row["bucket"]: row["c"]
        for row in conn.execute(
            """SELECT cast(round(rating) as integer) AS bucket, count(*) AS c
               FROM entries WHERE rating IS NOT NULL AND user_id = ? GROUP BY bucket""",
            (user_id,),
        )
    }

    genre_counts = {}
    for row in conn.execute(
        """SELECT titles.genres FROM entries JOIN titles ON titles.id = entries.title_id
           WHERE entries.status = 'watched' AND entries.user_id = ?
             AND titles.genres IS NOT NULL AND titles.genres != ''""",
        (user_id,),
    ):
        for genre in row["genres"].split(","):
            genre = _GENRE_ES.get(genre.strip(), genre.strip())
            genre_counts[genre] = genre_counts.get(genre, 0) + 1
    top_genres = sorted(genre_counts.items(), key=lambda g: g[1], reverse=True)[:10]

    # Excluye episodios de titulos marcados como habito (Shin Chan, Pokemon...) - El usuario,
    # tras ver un pico real de 7317 en un mes: eran 5528 episodios de series de habito
    # sincronizadas de golpe un mismo dia, no visionado real ese mes. Mismo criterio que
    # ya usa el motor de afinidad para excluir habito del eje apetito (ver
    # _aggregate_affinity) - aqui aplicado al grafico "Episodios por mes" por el mismo
    # motivo: refleja mejor CUANDO viste algo de verdad, no cuando se sincronizo.
    monthly = conn.execute(
        """SELECT substr(episode_watches.watched_at, 1, 7) AS month, count(*) AS c
           FROM episode_watches
           JOIN episodes ON episodes.id = episode_watches.episode_id
           JOIN entries ON entries.title_id = episodes.title_id AND entries.user_id = episode_watches.user_id
           WHERE episode_watches.user_id = ? AND (entries.is_habit IS NULL OR entries.is_habit != 1)
           GROUP BY month ORDER BY month DESC LIMIT 12""",
        (user_id,),
    ).fetchall()

    return {
        "totals": totals,
        "total_minutes": minutes,
        "histogram": histogram,
        "top_genres": top_genres,
        "monthly": list(reversed(monthly)),
    }




def get_available_years(conn, user_id) -> list[int]:
    """Años con actividad de visionado real de ESTE usuario - descarta fechas
    placeholder (epoch 1970, de historial importado sin fecha real)."""
    rows = conn.execute(
        """SELECT DISTINCT y FROM (
             SELECT substr(watched_at, 1, 4) AS y FROM episode_watches WHERE user_id = ?
             UNION
             SELECT substr(watched_at, 1, 4) AS y FROM entries WHERE watched_at IS NOT NULL AND user_id = ?
           ) WHERE y >= '1980' ORDER BY y DESC""",
        (user_id, user_id),
    ).fetchall()
    # Bug real (AGY, 2026-09-18): int(r["y"]) sin proteger revienta /resumen entero
    # con un 500 si algun watched_at viene malformado (fecha escrita a mano invalida
    # via /entrada/{id}/fecha-visionado, import viejo...) y sus primeros 4 caracteres
    # no son un año de verdad, aunque pasen el filtro de comparacion de texto ">= '1980'".
    return [int(r["y"]) for r in rows if r["y"] and r["y"].isdigit()]




def get_year_stats(conn, user_id, year: int):
    """Resumen estilo "wrapped" de un año concreto para ESTE usuario: todo por FECHA
    DE VISIONADO real (no por cuando se añadió el titulo a la app) - genero mas visto,
    mes mas activo y mejores notas puestas ese año.

    Antes filtraba con substr(watched_at,1,4) = 'YYYY', que SQLite no puede resolver
    con un indice (tiene que evaluar la funcion fila a fila) - con rangos de fecha
    (>= inicio del año Y < inicio del siguiente) el planner puede usar el indice de
    watched_at directamente en vez de barrer toda la tabla."""
    start, end = f"{year}-01-01", f"{year + 1}-01-01"
    totals = conn.execute(
        """SELECT
             (SELECT count(*) FROM episode_watches WHERE user_id = ? AND watched_at >= ? AND watched_at < ?) AS episodes_watched,
             (SELECT count(*) FROM entries JOIN titles ON titles.id = entries.title_id
               WHERE entries.status = 'watched' AND titles.type = 'movie' AND entries.user_id = ?
                 AND entries.watched_at >= ? AND entries.watched_at < ?) AS movies_watched,
             (SELECT count(DISTINCT episodes.title_id) FROM episode_watches
               JOIN episodes ON episodes.id = episode_watches.episode_id
               WHERE episode_watches.user_id = ? AND episode_watches.watched_at >= ? AND episode_watches.watched_at < ?) AS shows_active""",
        (user_id, start, end, user_id, start, end, user_id, start, end),
    ).fetchone()

    minutes = conn.execute(
        """SELECT
             COALESCE((SELECT sum(COALESCE(titles.runtime_minutes, 22))
               FROM episode_watches
               JOIN episodes ON episodes.id = episode_watches.episode_id
               JOIN titles ON titles.id = episodes.title_id
               WHERE episode_watches.user_id = ? AND episode_watches.watched_at >= ? AND episode_watches.watched_at < ?), 0)
             + COALESCE((SELECT sum(COALESCE(titles.runtime_minutes, 100))
               FROM entries JOIN titles ON titles.id = entries.title_id
               WHERE entries.status = 'watched' AND titles.type = 'movie' AND entries.user_id = ?
                 AND entries.watched_at >= ? AND entries.watched_at < ?), 0) AS total""",
        (user_id, start, end, user_id, start, end),
    ).fetchone()["total"]

    genre_counts = {}
    for row in conn.execute(
        """SELECT titles.genres FROM episode_watches
           JOIN episodes ON episodes.id = episode_watches.episode_id
           JOIN titles ON titles.id = episodes.title_id
           WHERE episode_watches.user_id = ? AND episode_watches.watched_at >= ? AND episode_watches.watched_at < ?
             AND titles.genres IS NOT NULL AND titles.genres != ''
           GROUP BY episodes.title_id
           UNION ALL
           SELECT titles.genres FROM entries JOIN titles ON titles.id = entries.title_id
           WHERE entries.status = 'watched' AND titles.type = 'movie' AND entries.user_id = ?
             AND entries.watched_at >= ? AND entries.watched_at < ?
             AND titles.genres IS NOT NULL AND titles.genres != ''""",
        (user_id, start, end, user_id, start, end),
    ):
        for genre in row["genres"].split(","):
            genre = _GENRE_ES.get(genre.strip(), genre.strip())
            genre_counts[genre] = genre_counts.get(genre, 0) + 1
    top_genres = sorted(genre_counts.items(), key=lambda g: g[1], reverse=True)[:5]

    monthly = conn.execute(
        """SELECT substr(watched_at, 6, 2) AS month, count(*) AS c
           FROM episode_watches WHERE user_id = ? AND watched_at >= ? AND watched_at < ?
           GROUP BY month ORDER BY month""",
        (user_id, start, end),
    ).fetchall()
    busiest_month = max(monthly, key=lambda r: r["c"], default=None)

    top_rated = conn.execute(
        """SELECT titles.title, titles.poster_path, entries.rating
           FROM entries JOIN titles ON titles.id = entries.title_id
           WHERE entries.rating IS NOT NULL AND entries.user_id = ?
             AND entries.watched_at >= ? AND entries.watched_at < ?
           ORDER BY entries.rating DESC LIMIT 5""",
        (user_id, start, end),
    ).fetchall()

    return {
        "year": year,
        "totals": totals,
        "total_minutes": minutes,
        "top_genres": top_genres,
        "monthly": monthly,
        "busiest_month": busiest_month,
        "top_rated": top_rated,
    }




def export_data(conn, user_id):
    """Volcado de lo curado a mano por ESTE usuario, MAS lo necesario para migrar a
    otra herramienta si hiciera falta algun dia - tmdb_id/imdb_id para reemparejar,
    episodios vistos con su fecha real, y rewatches. Independiente del backup de la
    BBDD (ese es un fichero sqlite, este es JSON legible a mano). `episodios_vistos`/
    `rewatches` van anidados bajo cada titulo (no en listas aparte) para poder leer un
    titulo entero sin cruzar con nada mas."""
    entries = conn.execute(
        """SELECT entries.id AS entry_id, titles.tmdb_id, titles.imdb_id, titles.title,
                  titles.type, titles.year, entries.status, entries.rating,
                  entries.comment, entries.added_at, entries.watched_at,
                  entries.cat_historia, entries.cat_animacion, entries.cat_personajes,
                  entries.cat_musica, entries.cat_disfrute
           FROM entries JOIN titles ON titles.id = entries.title_id
           WHERE entries.user_id = ?
           ORDER BY titles.title COLLATE NOCASE""",
        (user_id,),
    ).fetchall()

    episodes_by_title = {}
    for e in conn.execute(
        """SELECT titles.tmdb_id AS title_tmdb_id, episodes.season_number, episodes.episode_number,
                  episodes.name, episodes.air_date, episode_watches.watched_at
           FROM episode_watches
           JOIN episodes ON episodes.id = episode_watches.episode_id
           JOIN titles ON titles.id = episodes.title_id
           WHERE episode_watches.user_id = ?
           ORDER BY titles.tmdb_id, episodes.season_number, episodes.episode_number""",
        (user_id,),
    ):
        episodes_by_title.setdefault(e["title_tmdb_id"], []).append({
            "temporada": e["season_number"], "episodio": e["episode_number"],
            "nombre": e["name"], "emitido": e["air_date"], "visto": e["watched_at"],
        })

    rewatches_by_entry = {}
    for r in conn.execute(
        """SELECT watch_sessions.entry_id, watch_sessions.watched_at, watch_sessions.notes
           FROM watch_sessions JOIN entries ON entries.id = watch_sessions.entry_id
           WHERE entries.user_id = ? ORDER BY watch_sessions.entry_id, watch_sessions.watched_at""",
        (user_id,),
    ):
        rewatches_by_entry.setdefault(r["entry_id"], []).append(
            {"fecha": r["watched_at"], "notas": r["notes"]}
        )

    titulos = [
        {
            "tmdb_id": e["tmdb_id"], "imdb_id": e["imdb_id"], "title": e["title"],
            "type": e["type"], "year": e["year"], "status": e["status"], "rating": e["rating"],
            "comment": e["comment"], "added_at": e["added_at"], "watched_at": e["watched_at"],
            "cat_historia": e["cat_historia"], "cat_animacion": e["cat_animacion"],
            "cat_personajes": e["cat_personajes"], "cat_musica": e["cat_musica"],
            "cat_disfrute": e["cat_disfrute"],
            "episodios_vistos": episodes_by_title.get(e["tmdb_id"], []),
            "rewatches": rewatches_by_entry.get(e["entry_id"], []),
        }
        for e in entries
    ]

    lists = []
    for l in conn.execute("SELECT * FROM lists WHERE user_id = ? ORDER BY is_default DESC, name", (user_id,)):
        items = conn.execute(
            """SELECT titles.title FROM list_items
               JOIN entries ON entries.id = list_items.entry_id
               JOIN titles ON titles.id = entries.title_id
               WHERE list_items.list_id = ?
               ORDER BY list_items.position NULLS LAST, list_items.added_at""",
            (l["id"],),
        ).fetchall()
        lists.append({"name": l["name"], "titulos": [i["title"] for i in items]})

    waifus = conn.execute(
        """SELECT characters.name, characters.character_name, titles.title AS serie
           FROM favorite_characters
           JOIN characters ON characters.id = favorite_characters.character_id
           JOIN entries ON entries.id = favorite_characters.entry_id
           JOIN titles ON titles.id = entries.title_id
           WHERE entries.user_id = ?
           ORDER BY favorite_characters.position NULLS LAST""",
        (user_id,),
    ).fetchall()

    season_ratings = conn.execute(
        """SELECT titles.title, season_ratings.season_number, season_ratings.rating,
                  season_ratings.comment, season_ratings.cat_historia,
                  season_ratings.cat_animacion, season_ratings.cat_personajes,
                  season_ratings.cat_musica, season_ratings.cat_disfrute
           FROM season_ratings
           JOIN entries ON entries.id = season_ratings.entry_id
           JOIN titles ON titles.id = entries.title_id
           WHERE entries.user_id = ?
           ORDER BY titles.title COLLATE NOCASE, season_ratings.season_number""",
        (user_id,),
    ).fetchall()

    return {
        "exportado_el": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "titulos": titulos,
        "listas": lists,
        "waifus": [dict(w) for w in waifus],
        "notas_por_temporada": [dict(s) for s in season_ratings],
    }




def get_rating_discrepancies(conn, user_id, limit=25):
    """Donde ESTE usuario mas se aleja de la nota de Internet (TMDB) - lo que le
    flipa y la critica no tanto, y al reves. Misma escala 1-10 en los dos lados,
    resta directa."""
    rows = conn.execute(
        """SELECT titles.title, titles.poster_path, titles.tmdb_id, titles.type,
                  entries.rating, titles.vote_average,
                  (entries.rating - titles.vote_average) AS diff
           FROM entries JOIN titles ON titles.id = entries.title_id
           WHERE entries.rating IS NOT NULL AND titles.vote_average IS NOT NULL AND entries.user_id = ?""",
        (user_id,),
    ).fetchall()
    te_gusta_mas = sorted(rows, key=lambda r: r["diff"], reverse=True)[:limit]
    te_gusta_menos = sorted(rows, key=lambda r: r["diff"])[:limit]
    return te_gusta_mas, te_gusta_menos
