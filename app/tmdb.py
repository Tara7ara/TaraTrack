from concurrent.futures import ThreadPoolExecutor

import httpx

from app import config

API_BASE = "https://api.themoviedb.org/3"
IMAGE_BASE = "https://image.tmdb.org/t/p/w500"
API_KEY = config.TMDB_API_KEY

# Cliente compartido con conexiones keep-alive: evita renegociar TLS en cada llamada
# (la app hace muchas peticiones sueltas a TMDB - search, detalle, recomendaciones...).
_client = httpx.Client(timeout=10)

# Para llamadas independientes que no dependen unas de otras (2 busquedas de /buscar,
# 8 semillas de /recomendados): TMDB esta en internet, no en la LAN, asi que el cuello
# de botella real es la latencia de red - lanzarlas en paralelo con hilos (I/O-bound,
# el GIL se libera mientras se espera la respuesta) corta el tiempo total de ~Nx a ~1x.
_pool = ThreadPoolExecutor(max_workers=8)


def _get(path: str, **params):
    params["api_key"] = API_KEY
    # Titulos/sinopsis en español donde TMDB los tenga (mas cercano a como busca Tara).
    params.setdefault("language", "es-ES")
    resp = _client.get(f"{API_BASE}{path}", params=params)
    resp.raise_for_status()
    return resp.json()


def _looks_like_junk(r: dict) -> bool:
    """TMDB tiene entradas basura (agregadores/placeholders tipo "Black Clover
    Seasons 2") sin año, sin nota y con popularidad ridicula (~0.3) - un titulo real,
    aunque sea nuevo, casi siempre tiene al menos uno de los tres."""
    return r["year"] is None and not r["vote_average"] and r["popularity"] < 1


def search(query: str) -> list[dict]:
    """Busca en movies y tv a la vez (en paralelo), devuelve una lista normalizada
    sin la basura de agregadores de TMDB."""
    futures = [
        _pool.submit(_get, endpoint, query=query)
        for endpoint in ("/search/tv", "/search/movie")
    ]
    results = []
    for media_type, future in zip(("show", "movie"), futures):
        for item in future.result().get("results", []):
            results.append(_normalize(item, media_type))
    results = [r for r in results if not _looks_like_junk(r)]
    results.sort(key=lambda r: r["popularity"], reverse=True)
    return results


def _normalize(item: dict, media_type: str) -> dict:
    return {
        "tmdb_id": item["id"],
        "type": media_type,
        "title": item.get("name") or item.get("title"),
        "year": _year(item.get("first_air_date") or item.get("release_date")),
        "poster_path": item.get("poster_path"),
        "poster_url": f"{IMAGE_BASE}{item['poster_path']}" if item.get("poster_path") else None,
        "overview": item.get("overview", ""),
        "popularity": item.get("popularity", 0),
        "vote_average": item.get("vote_average"),
        # TMDB solo marca contenido pornografico explicito, no "maduro" en general -
        # es la unica señal automatica disponible sin mantener una lista a mano.
        "adult": bool(item.get("adult")),
    }


def get_details(tmdb_id: int, media_type: str) -> dict:
    endpoint = "/tv" if media_type == "show" else "/movie"
    details_future = _pool.submit(_get, f"{endpoint}/{tmdb_id}")
    external_future = _pool.submit(_get, f"{endpoint}/{tmdb_id}/external_ids")
    data = details_future.result()
    external = external_future.result()
    result = {
        "tmdb_id": data["id"],
        "type": media_type,
        "title": data.get("name") or data.get("title"),
        "year": _year(data.get("first_air_date") or data.get("release_date")),
        "release_date": data.get("first_air_date") or data.get("release_date"),
        "poster_path": data.get("poster_path"),
        "poster_url": f"{IMAGE_BASE}{data['poster_path']}" if data.get("poster_path") else None,
        "overview": data.get("overview", ""),
        "genres": [g["name"] for g in data.get("genres", [])],
        "imdb_id": external.get("imdb_id"),
        "vote_average": data.get("vote_average"),
        "adult": bool(data.get("adult")),
        "original_language": data.get("original_language"),
        # Titulo SIN traducir (name/title original de TMDB, no el es-ES) - AniList no
        # indexa titulos en español, asi que buscar "Caballeros de Sidonia" ahi no
        # encuentra nunca "Knights of Sidonia" (ver repo.backfill_anilist_profile).
        "original_title": data.get("original_name") or data.get("original_title"),
    }
    if media_type == "show":
        # episode_run_time viene vacio en muchas series modernas; el runtime del
        # ultimo episodio emitido es mas fiable como "minutos por episodio".
        episode_run_time = data.get("episode_run_time") or []
        last_ep = data.get("last_episode_to_air") or {}
        result["runtime_minutes"] = (
            (episode_run_time[0] if episode_run_time else None) or last_ep.get("runtime")
        )
        result["episode_count"] = data.get("number_of_episodes")
        next_ep = data.get("next_episode_to_air")
        result["show_status"] = data.get("status")
        result["seasons"] = [
            s["season_number"] for s in data.get("seasons", []) if s["season_number"] > 0
        ]
        if next_ep:
            result["next_episode_air_date"] = next_ep.get("air_date")
            result["next_episode_label"] = (
                f"T{next_ep.get('season_number')}E{next_ep.get('episode_number')} — "
                f"{next_ep.get('name') or 'Proximo episodio'}"
            )
        else:
            result["next_episode_air_date"] = None
            result["next_episode_label"] = None
    else:
        result["runtime_minutes"] = data.get("runtime")
        result["episode_count"] = None
    return result


def get_recommendations(tmdb_id: int, media_type: str) -> list[dict]:
    endpoint = "/tv" if media_type == "show" else "/movie"
    data = _get(f"{endpoint}/{tmdb_id}/recommendations")
    results = [_normalize(item, media_type) for item in data.get("results", [])]
    return [r for r in results if not _looks_like_junk(r)]


def get_season_episodes(tmdb_id: int, season_number: int) -> list[dict]:
    data = _get(f"/tv/{tmdb_id}/season/{season_number}")
    return [
        {
            "season_number": season_number,
            "episode_number": ep["episode_number"],
            "name": ep.get("name"),
            "air_date": ep.get("air_date"),
        }
        for ep in data.get("episodes", [])
    ]


PROFILE_BASE = "https://image.tmdb.org/t/p/w185"


def get_trailer(tmdb_id: int, media_type: str) -> str | None:
    """URL de YouTube del trailer oficial, o None si no hay ninguno. Prueba en
    español primero (consistente con el resto de la app) y si no hay nada cae al
    ingles - muchos trailers solo estan subidos en el idioma original."""
    endpoint = "/tv" if media_type == "show" else "/movie"
    for params in ({}, {"language": "en-US"}):
        try:
            data = _get(f"{endpoint}/{tmdb_id}/videos", **params)
        except Exception:
            continue
        videos = [
            v for v in data.get("results", [])
            if v.get("site") == "YouTube" and v.get("type") == "Trailer"
        ]
        if not videos:
            continue
        best = next((v for v in videos if v.get("official")), videos[0])
        return f"https://www.youtube.com/watch?v={best['key']}"
    return None


def get_credits(tmdb_id: int, media_type: str, limit: int = 15) -> list[dict]:
    endpoint = "/tv" if media_type == "show" else "/movie"
    data = _get(f"{endpoint}/{tmdb_id}/credits")
    cast = data.get("cast", [])[:limit]
    return [
        {
            "tmdb_person_id": c["id"],
            "name": c.get("name"),
            "character_name": c.get("character"),
            "profile_path": f"{PROFILE_BASE}{c['profile_path']}" if c.get("profile_path") else None,
        }
        for c in cast
    ]


def download_poster(poster_url: str, dest_path: str) -> bool:
    """Descarga la portada a disco. Devuelve True si se guardo, False si no habia portada."""
    if not poster_url:
        return False
    resp = _client.get(poster_url)
    resp.raise_for_status()
    with open(dest_path, "wb") as f:
        f.write(resp.content)
    return True


def _year(date_str: str | None) -> int | None:
    if not date_str:
        return None
    try:
        return int(date_str[:4])
    except ValueError:
        return None
