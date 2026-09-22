"""app.routers.buscar - extraido de main.py en el split de modulos (ronda 2026-08-21)."""

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import anime, repo, tmdb
from app.db import get_connection
from app.matching import (
    _strip_season_suffix,
)
from app.web import templates

router = APIRouter()




@router.get("/buscar", response_class=HTMLResponse)
def buscar(request: Request, q: str = ""):
    """Un puñado de recomendados de entrada, para que /buscar no arranque en blanco
    antes de teclear nada - lectura de la cache, instantanea (ver repo.list_recommendations).
    Con ?q= (enlaces "Buscar y añadir" del calendario de temporada) el input arranca
    ya relleno y dispara la busqueda solo con "load" en el hx-trigger, sin duplicar la
    logica de busqueda de /buscar/resultados."""
    with get_connection() as conn:
        # 6 dejaba una fila corta con hueco muerto grande a la derecha en pantallas
        # anchas (El usuario, monitor QHD: "se ve vacía") - 11 a peticion explicita suya.
        recs = repo.list_recommendations(conn, request.state.user_id, limit=11)
    for r in recs:
        r["state"] = "new"
    return templates.TemplateResponse(request, "search.html", {"recs": recs, "q": q})




@router.get("/buscar/resultados", response_class=HTMLResponse)
def buscar_resultados(request: Request, q: str = ""):
    """Busqueda permisiva: TMDB en español y, si no hay nada (romaji, typos, motes,
    o TMDB dio un timeout/error puntual), se le pide a AniList el titulo canonico y
    se reintenta con el. tmdb.search() sin proteger tumbaba la ruta entera (500, sin
    resultados en pantalla) si un solo timeout de TMDB reventaba - "Black Clover" a
    veces desaparecia del buscador sin más explicación por esto."""
    # Bug real (El usuario, 2026-08-21): "al buscar una cosa se me desaparecen los
    # recomendados" - al escribir y luego borrar el buscador hasta dejarlo vacio,
    # este endpoint devolvia un partial en blanco (search_results.html no pinta
    # nada si results=[] y query=""), perdiendo para siempre los recomendados que
    # /buscar puso al entrar - htmx solo sabe pedir resultados de busqueda, nunca
    # "vuelve" sola a la vista inicial. Con q vacio, se devuelven los mismos
    # recomendados que ya se ven al entrar en la pagina.
    if not q.strip():
        with get_connection() as conn:
            recs = repo.list_recommendations(conn, request.state.user_id, limit=11)
        for r in recs:
            r["state"] = "new"
        return templates.TemplateResponse(
            request, "partials/search_results.html", {"results": recs, "query": ""}
        )
    results = []
    if q.strip():
        try:
            results = tmdb.search(q)
        except Exception:
            results = []
    if q.strip() and not results:
        stripped = _strip_season_suffix(q)
        if stripped:
            try:
                results = tmdb.search(stripped)
            except Exception:
                pass
    if q.strip() and not results:
        try:
            match = anime.search_anime(q)
            if match and match["title"] and match["title"].lower() != q.strip().lower():
                results = tmdb.search(match["title"])
        except Exception:
            pass
    with get_connection() as conn:
        states = repo.get_entry_states(conn, [r["tmdb_id"] for r in results], request.state.user_id)
    for r in results:
        r["state"] = states.get(r["tmdb_id"], "new")
    return templates.TemplateResponse(
        request, "partials/search_results.html", {"results": results, "query": q}
    )




@router.post("/alta-manual", response_class=HTMLResponse)
def alta_manual(
    request: Request,
    title: str = Form(...),
    type: str = Form("show"),
    year: str = Form(""),
    poster_url: str = Form(""),
):
    year_value = int(year) if year.strip().isdigit() else None
    with get_connection() as conn:
        title_row = repo.create_manual_entry(
            conn, type, title.strip(), year_value, poster_url.strip() or None, request.state.user_id
        )
    return RedirectResponse(f"/titulo/{title_row['tmdb_id']}/{type}", status_code=303)
