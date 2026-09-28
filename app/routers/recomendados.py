"""app.routers.recomendados - recomendados y BAN."""
from datetime import date

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from app import repo
from app.db import get_connection
from app.routers.calendario import _current_season
from app.web import templates

router = APIRouter()


# Géneros demasiado genéricos para una fila propia: casi todo lo recomendado es animación.
_GENEROS_SIN_FILA = {"Animación", "Kids", "Familia", "Película de TV", "News", "Talk", "Reality", "Soap"}


def _generos_favoritos(conn, user_id: int, n: int = 3) -> list[str]:
    """Tus géneros más fuertes: los que más se repiten en lo que has puntuado con 8 o más."""
    counts: dict[str, int] = {}
    for (genres,) in conn.execute(
        """SELECT titles.genres FROM entries JOIN titles ON titles.id = entries.title_id
           WHERE entries.user_id = ? AND entries.rating >= 8 AND titles.genres IS NOT NULL""",
        (user_id,),
    ):
        for g in genres.split(","):
            g = g.strip()
            if g and g not in _GENEROS_SIN_FILA:
                counts[g] = counts.get(g, 0) + 1
    return [g for g, _ in sorted(counts.items(), key=lambda kv: -kv[1])][:n * 2]


def _anime_temporada(conn, user_id: int, limit: int = 20) -> list[dict]:
    """Anime de la temporada actual que no está en tu lista, del que más te gustará al que
    menos (misma predicción que el calendario de temporada). Sale de la caché del
    calendario: aquí no se llama a AniList."""
    season, year = _current_season(), date.today().year
    items, _age = repo.get_cached_season(conn, season, year)
    if not items:
        return []
    en_lista = repo.library_status_for_cards(conn, user_id, items)
    profile = repo.build_taste_profile(conn, user_id)
    out = []
    for it in items:
        if en_lista.get(it["anilist_id"]):
            continue
        detail = repo.predict_score_detail(it, profile)
        out.append({**it, "predict": detail["score"] if detail else None})
    out.sort(key=lambda i: (i["predict"] if i["predict"] is not None else -1, i.get("score") or 0), reverse=True)
    return out[:limit]


@router.get("/recomendados", response_class=HTMLResponse)
def recomendados(request: Request, tipo: str = "", todas: str = ""):
    uid = request.state.user_id
    with get_connection() as conn:
        recs, _pages = repo.list_recommendations(conn, uid, tipo=tipo, limit=10000, with_pages=True)
        recs = repo.recommendation_extras(conn, recs)
        generos = _generos_favoritos(conn, uid) if not todas else []
        temporada = _anime_temporada(conn, uid) if not todas and tipo != "pelis" else []
    top = recs[:10]
    if todas:
        return templates.TemplateResponse(
            request, "recommendations.html",
            {"top": [], "todas": recs, "total": len(recs), "tipo": tipo, "title": "Recomendados"},
        )
    placed = {r["tmdb_id"] for r in top}
    filas = []
    while len(filas) < 6:
        cand: dict[str, list] = {}
        for r in recs:
            if r["tmdb_id"] not in placed:
                for seed in r["seeds"]:
                    cand.setdefault(seed, []).append(r)
        if not cand:
            break
        seed, items = max(cand.items(), key=lambda kv: len(kv[1]))
        if len(items) < 5:
            break
        items = items[:20]
        placed.update(r["tmdb_id"] for r in items)
        filas.append((seed, items))
    por_genero = []
    for g in generos:
        items = [r for r in recs if g in r["genres"]][:20]
        if len(items) >= 6:
            por_genero.append((g, items))
        if len(por_genero) == 3:
            break
    mas = [r for r in recs if r["tmdb_id"] not in placed][:30]
    return templates.TemplateResponse(
        request, "recommendations.html",
        {"top": top, "filas": filas, "por_genero": por_genero, "temporada": temporada, "mas": mas,
         "total": len(recs), "tipo": tipo, "transparent_nav": bool(top), "title": "Recomendados"},
    )


@router.post("/recomendados/rechazar", response_class=HTMLResponse)
def recomendados_rechazar(
    request: Request,
    tmdb_id: int = Form(...),
    type: str = Form(...),
    title: str = Form(...),
    poster_path: str = Form(""),
    seed_title: str = Form(""),
):
    """El BAN de una tarjeta: no vuelve a salir en recomendados. Guarda tambien la
    semilla ("porque te gusto X") que la genero, para poder aprender que semillas
    recomiendan mal (ver repo._seed_ban_counts). Se puede deshacer desde /ajustes.
    Devuelve vacio para que htmx quite la tarjeta."""
    with get_connection() as conn:
        repo.reject_recommendation(
            conn, tmdb_id, type, title, poster_path.strip() or None, request.state.user_id,
            seed_title.strip() or None,
        )
    return HTMLResponse("")
