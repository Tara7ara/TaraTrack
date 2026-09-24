"""Busqueda de anime fuera de TMDB, para altas manuales.

Red de seguridad cuando TMDB no conoce un titulo: intenta Jikan (MyAnimeList) y,
si esta caido (escupe 504/429 con frecuencia), cae a AniList. Ambas son gratis y
sin API key. Devuelven portada, año, episodios y duracion.
"""

import time
from datetime import date, datetime, timezone

import httpx

JIKAN_BASE = "https://api.jikan.moe/v4"
ANILIST_URL = "https://graphql.anilist.co"

ANILIST_SEASON_QUERY = """
query ($season: MediaSeason, $year: Int, $page: Int, $formats: [MediaFormat]) {
  Page(page: $page, perPage: 50) {
    pageInfo { hasNextPage }
    media(season: $season, seasonYear: $year, type: ANIME, format_in: $formats, sort: POPULARITY_DESC) {
      id
      title { romaji english }
      coverImage { large }
      format
      genres
      studios(isMain: true) { nodes { name } }
      tags { name rank isGeneralSpoiler }
      averageScore
      siteUrl
      nextAiringEpisode { airingAt }
      startDate { day month year }
      relations {
        edges {
          relationType(version: 2)
          node { id type }
        }
      }
      recommendations(sort: RATING_DESC, perPage: 10) {
        nodes { rating mediaRecommendation { id } }
      }
    }
  }
}
"""


ANILIST_MATCH_QUERY = """
query ($search: String) {
  Media(search: $search, type: ANIME) {
    id
    title { romaji english }
    genres
    studios(isMain: true) { nodes { name } }
    tags { name rank isGeneralSpoiler }
    relations {
      edges {
        relationType(version: 2)
        node { id type }
      }
    }
    recommendations(sort: RATING_DESC, perPage: 25) {
      nodes { rating mediaRecommendation { id } }
    }
  }
}
"""


# Recomendaciones de la comunidad guardadas por título (anilist_cross_rec_ids). El
# calendario de temporada se queda en 10: pide 50 animes por página y con 25 supera el
# límite de complejidad de AniList.
ANILIST_RECS_PER_TITLE = 25

ANILIST_RECS_BY_ID_QUERY = """
query ($ids: [Int], $perRec: Int) {
  Page(page: 1, perPage: 50) {
    media(id_in: $ids, type: ANIME) {
      id
      recommendations(sort: RATING_DESC, perPage: $perRec) {
        nodes { rating mediaRecommendation { id } }
      }
    }
  }
}
"""


def get_anilist_recommendations(ids: list[int], chunk_size: int = 10) -> dict[int, tuple[list[int], list[int]]]:
    """Recomendaciones de la comunidad por id de AniList ya conocido, en lotes - para
    refrescar anilist_cross_rec_ids sin repetir la busqueda por texto de cada titulo
    (que podria emparejar distinto). Lotes pequeños por el limite de complejidad.
    Devuelve {anilist_id: (ids_recomendados, votos)}, alineados."""
    out = {}
    for start in range(0, len(ids), chunk_size):
        chunk = ids[start:start + chunk_size]
        for attempt in range(4):
            resp = httpx.post(
                ANILIST_URL,
                json={"query": ANILIST_RECS_BY_ID_QUERY,
                      "variables": {"ids": chunk, "perRec": ANILIST_RECS_PER_TITLE}},
                timeout=20,
            )
            if resp.status_code != 429:
                break
            time.sleep(float(resp.headers.get("Retry-After", 3 * (attempt + 1))))
        resp.raise_for_status()
        for m in ((resp.json().get("data") or {}).get("Page") or {}).get("media") or []:
            out[m["id"]] = (_extract_relations(m)[1], _extract_rec_votes(m))
        time.sleep(0.7)
    return out


def _extract_tags(media: dict) -> list[tuple[str, int]]:
    """Tags relevantes de AniList (rank >= 60, sin spoilers, máximo 8 por título).
    Devuelve (nombre, rank): el rank pondera cada tag en la predicción."""
    relevant = [
        t for t in (media.get("tags") or [])
        if t.get("rank", 0) >= 60 and not t.get("isGeneralSpoiler")
    ]
    relevant.sort(key=lambda t: t.get("rank", 0), reverse=True)
    return [(t["name"], t["rank"]) for t in relevant[:8]]


def _extract_relations(media: dict) -> tuple[list[int], list[int]]:
    """Precuelas/misma franquicia (PREQUEL o PARENT, solo ANIME) y recomendaciones
    cruzadas de AniList - compartido entre match_anilist_basic (titulos ya en la
    biblioteca, pendientes o vistos) y get_seasonal_anime (candidatos del calendario),
    antes duplicado en las dos funciones."""
    prequel_ids = [
        e["node"]["id"] for e in (media.get("relations") or {}).get("edges", [])
        if e.get("relationType") in ("PREQUEL", "PARENT") and (e.get("node") or {}).get("type") == "ANIME"
    ]
    cross_rec_ids = [
        n["mediaRecommendation"]["id"] for n in (media.get("recommendations") or {}).get("nodes", [])
        if n.get("mediaRecommendation")
    ]
    return prequel_ids, cross_rec_ids


def _extract_rec_votes(media: dict) -> list[int]:
    """Votos netos de la comunidad de cada recomendacion, alineados con los
    cross_rec_ids de _extract_relations (mismo filtro, mismo orden). AniList puede
    devolver votos netos negativos o 0; se guardan tal cual."""
    return [
        n.get("rating") or 0 for n in (media.get("recommendations") or {}).get("nodes", [])
        if n.get("mediaRecommendation")
    ]


def match_anilist_basic(title: str) -> dict | None:
    """Id, géneros, estudio, tags y relaciones de AniList para un título de la
    biblioteca, emparejado por texto. AniList limita a ~90 peticiones/minuto, así que
    reintenta respetando Retry-After."""
    for attempt in range(4):
        resp = httpx.post(
            ANILIST_URL, json={"query": ANILIST_MATCH_QUERY, "variables": {"search": title}}, timeout=10
        )
        if resp.status_code != 429:
            break
        time.sleep(float(resp.headers.get("Retry-After", 3 * (attempt + 1))))
    resp.raise_for_status()
    media = (resp.json().get("data") or {}).get("Media")
    if not media:
        return None
    studios = [s["name"] for s in (media.get("studios") or {}).get("nodes", [])]
    prequel_ids, cross_rec_ids = _extract_relations(media)
    title = media.get("title") or {}
    return {
        "anilist_id": media["id"],
        "genres": media.get("genres") or [],
        "studio": studios[0] if studios else None,
        # Romaji/inglés para el subtítulo de la ficha: sirve para buscar el anime en
        # webs que no conocen el título en español.
        "title_romaji": title.get("romaji"),
        "title_english": title.get("english"),
        # Solo nombres aqui - esto va al perfil (titulos YA puntuados), el rank de
        # relevancia solo hace falta del lado del candidato (get_seasonal_anime).
        "tags": [name for name, _rank in _extract_tags(media)],
        # Guardado para predecir también sobre pendientes sin volver a consultar AniList.
        "prequel_ids": prequel_ids,
        "cross_rec_ids": cross_rec_ids,
        "cross_rec_votes": _extract_rec_votes(media),
    }


ANILIST_TITLES_QUERY = """
query ($ids: [Int], $page: Int) {
  Page(page: $page, perPage: 50) {
    pageInfo { hasNextPage }
    media(id_in: $ids, type: ANIME) {
      id
      title { romaji english }
      isAdult
    }
  }
}
"""


def get_anilist_titles(ids: list[int]) -> dict[int, dict]:
    """Titulo romaji/ingles de varios anime por id de AniList, en lotes de 50 - para
    poder emparejar con TMDB las recomendaciones de la comunidad (repo.recommendations),
    que solo se guardan como ids. Mismo reintento ante 429 que match_anilist_basic."""
    out = {}
    for start in range(0, len(ids), 50):
        chunk = ids[start:start + 50]
        for attempt in range(4):
            resp = httpx.post(
                ANILIST_URL,
                json={"query": ANILIST_TITLES_QUERY, "variables": {"ids": chunk, "page": 1}},
                timeout=15,
            )
            if resp.status_code != 429:
                break
            time.sleep(float(resp.headers.get("Retry-After", 3 * (attempt + 1))))
        resp.raise_for_status()
        for m in ((resp.json().get("data") or {}).get("Page") or {}).get("media") or []:
            title = m.get("title") or {}
            out[m["id"]] = {
                "romaji": title.get("romaji"),
                "english": title.get("english"),
                "is_adult": bool(m.get("isAdult")),
            }
    return out


def get_seasonal_anime(season: str, year: int) -> list[dict]:
    """Todo el anime de una temporada según AniList (TV, TV_SHORT y ONA; películas y
    especiales no tienen día de emisión). Hasta 3 páginas de 50."""
    results = []
    for page in range(1, 4):
        resp = httpx.post(
            ANILIST_URL,
            json={
                "query": ANILIST_SEASON_QUERY,
                "variables": {
                    "season": season, "year": year, "page": page,
                    "formats": ["TV", "TV_SHORT", "ONA"],
                },
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = (resp.json().get("data") or {}).get("Page") or {}
        for m in data.get("media", []):
            title = m.get("title") or {}
            airing_at = (m.get("nextAiringEpisode") or {}).get("airingAt")
            start = m.get("startDate") or {}
            score = m.get("averageScore")
            studios = [s["name"] for s in (m.get("studios") or {}).get("nodes", [])]
            prequel_ids, cross_rec_ids = _extract_relations(m)
            results.append(
                {
                    "anilist_id": m.get("id"),
                    "title": title.get("english") or title.get("romaji"),
                    # Romaji aparte: TMDB a menudo solo indexa el romaji de los animes
                    # sin licencia occidental.
                    "title_romaji": title.get("romaji"),
                    "tags": _extract_tags(m),
                    "image": (m.get("coverImage") or {}).get("large"),
                    "format": m.get("format"),
                    "genres": m.get("genres") or [],
                    "studio": studios[0] if studios else None,
                    "score": score / 10 if score else None,
                    "site_url": m.get("siteUrl"),
                    "prequel_ids": prequel_ids,
                    "cross_rec_ids": cross_rec_ids,
                    # 0=lunes...6=domingo. Una serie terminada no tiene
                    # nextAiringEpisode: se usa el día de la semana del estreno.
                    "weekday": (
                        datetime.fromtimestamp(airing_at, tz=timezone.utc).weekday() if airing_at
                        else date(start["year"], start["month"], start["day"]).weekday()
                        if start.get("year") and start.get("month") and start.get("day")
                        else None
                    ),
                }
            )
        if not data.get("pageInfo", {}).get("hasNextPage"):
            break
    return results


ANILIST_QUERY = """
query ($search: String) {
  Media(search: $search, type: ANIME) {
    title { romaji }
    coverImage { extraLarge }
    seasonYear
    episodes
    duration
    averageScore
  }
}
"""

ANILIST_CHARACTERS_QUERY = """
query ($search: String, $id: Int) {
  Media(search: $search, id: $id, type: ANIME) {
    characters(sort: [ROLE, RELEVANCE], perPage: 25) {
      edges {
        role
        node { id name { full } image { large } }
      }
    }
  }
}
"""

# Busqueda global de personajes por nombre: para los que no entran en el top 25 del
# titulo (Black Clover tiene 500 en AniList) - se añaden a mano desde la ficha.
ANILIST_CHARACTER_SEARCH_QUERY = """
query ($search: String) {
  Page(perPage: 12) {
    characters(search: $search) {
      id
      name { full }
      image { large }
      media(perPage: 2, type: ANIME) { nodes { title { romaji english } } }
    }
  }
}
"""


def search_characters(query: str) -> tuple[list[dict], bool]:
    """Personajes de cualquier anime que casen con el nombre, con sus series (para
    distinguir a los tropecientos 'Nero' que existen). AniList primero; si no conoce
    el nombre (p.ej. 'Secre Swallowtail', que alli se llama 'Kuro Nero'), MyAnimeList
    via Jikan (mas lento y mas caido - Jikan da 504 con frecuencia).
    Devuelve (resultados, jikan_caido) para que la UI distinga "no existe ese
    personaje" de "prueba en un rato, MyAnimeList no responde ahora mismo" -
    id positivo = AniList, negativo = MAL (ver repo.add_character_manual)."""
    try:
        results = _search_anilist_characters(query)
    except Exception:
        results = []
    if results:
        return results, False
    try:
        return _search_jikan_characters(query), False
    except Exception:
        return [], True


def _search_anilist_characters(query: str) -> list[dict]:
    resp = httpx.post(
        ANILIST_URL,
        json={"query": ANILIST_CHARACTER_SEARCH_QUERY, "variables": {"search": query}},
        timeout=10,
    )
    resp.raise_for_status()
    page = ((resp.json().get("data") or {}).get("Page") or {})
    return [
        {
            "id": c["id"],
            "name": (c.get("name") or {}).get("full"),
            "image_url": (c.get("image") or {}).get("large"),
            # romaji + ingles de cada anime (dedupe conservando orden): el ingles es el
            # que suele casar con los titulos TMDB de la biblioteca.
            "media": list(dict.fromkeys(
                t for n in (c.get("media") or {}).get("nodes", []) if n.get("title")
                for t in (n["title"].get("romaji"), n["title"].get("english")) if t
            )),
        }
        for c in page.get("characters", [])
        if c.get("id")
    ]


def _search_jikan_characters(query: str, limit: int = 5) -> list[dict]:
    for attempt in range(3):
        resp = httpx.get(f"{JIKAN_BASE}/characters", params={"q": query, "limit": limit}, timeout=10)
        if resp.status_code < 500 and resp.status_code != 429:
            break
        time.sleep(1.5 * (attempt + 1))
    resp.raise_for_status()
    results = []
    for item in resp.json().get("data", [])[:limit]:
        # El anime de cada personaje va en una llamada aparte (y Jikan limita a ~3/s).
        media = []
        try:
            r2 = httpx.get(f"{JIKAN_BASE}/characters/{item['mal_id']}/anime", timeout=10)
            if r2.status_code == 200:
                media = [a["anime"]["title"] for a in r2.json().get("data", [])[:3] if a.get("anime")]
        except Exception:
            pass
        time.sleep(0.4)
        results.append(
            {
                "id": -item["mal_id"],
                "name": item.get("name"),
                "image_url": (item.get("images", {}).get("jpg", {}) or {}).get("image_url"),
                "media": media,
            }
        )
    return results


def get_characters(query: str | None = None, anilist_id: int | None = None) -> list[dict]:
    """Personajes del anime (con su imagen de personaje, no del actor real), via AniList,
    por id si ya se conoce (exacto) o por busqueda de titulo. Lista vacia si AniList no
    lo conoce - el caller decide que hacer."""
    variables = {"id": anilist_id} if anilist_id else {"search": query}
    resp = httpx.post(
        ANILIST_URL,
        json={"query": ANILIST_CHARACTERS_QUERY, "variables": variables},
        timeout=10,
    )
    if resp.status_code == 404:  # AniList responde 404 cuando la busqueda no encuentra nada
        return []
    resp.raise_for_status()
    media = (resp.json().get("data") or {}).get("Media")
    if not media:
        return []
    return [
        {
            "id": edge["node"]["id"],
            "name": (edge["node"].get("name") or {}).get("full"),
            "image_url": (edge["node"].get("image") or {}).get("large"),
            "role": edge.get("role"),
        }
        for edge in (media.get("characters") or {}).get("edges", [])
        if edge.get("node")
    ]


def search_anime(query: str) -> dict | None:
    """Mejor resultado para el titulo dado, o None si ninguna fuente lo conoce."""
    for source in (_search_jikan, _search_anilist):
        try:
            match = source(query)
        except Exception:
            continue
        if match:
            return match
    return None


def _search_jikan(query: str) -> dict | None:
    for attempt in range(2):
        resp = httpx.get(f"{JIKAN_BASE}/anime", params={"q": query, "limit": 1}, timeout=10)
        if resp.status_code < 500 and resp.status_code != 429:
            break
        time.sleep(1 + attempt)
    resp.raise_for_status()
    results = resp.json().get("data", [])
    if not results:
        return None
    item = results[0]
    return {
        "title": item.get("title"),
        "year": item.get("year"),
        "poster_url": (item.get("images", {}).get("jpg", {}) or {}).get("large_image_url"),
        "episode_count": item.get("episodes"),
        "runtime_minutes": _parse_duration(item.get("duration")),
        "score": item.get("score"),
    }


def _search_anilist(query: str) -> dict | None:
    resp = httpx.post(
        ANILIST_URL,
        json={"query": ANILIST_QUERY, "variables": {"search": query}},
        timeout=10,
    )
    resp.raise_for_status()
    media = (resp.json().get("data") or {}).get("Media")
    if not media:
        return None
    score = media.get("averageScore")
    return {
        "title": (media.get("title") or {}).get("romaji"),
        "year": media.get("seasonYear"),
        "poster_url": (media.get("coverImage") or {}).get("extraLarge"),
        "episode_count": media.get("episodes"),
        "runtime_minutes": media.get("duration"),
        "score": score / 10 if score else None,
    }


def _parse_duration(duration: str | None) -> int | None:
    """Jikan devuelve '24 min per ep' o '1 hr 52 min' - extrae los minutos totales."""
    if not duration:
        return None
    minutes, hours = 0, 0
    parts = duration.split()
    for i, token in enumerate(parts):
        if token.startswith("hr") and i > 0 and parts[i - 1].isdigit():
            hours = int(parts[i - 1])
        if token.startswith("min") and i > 0 and parts[i - 1].isdigit():
            minutes = int(parts[i - 1])
    total = hours * 60 + minutes
    return total or None
