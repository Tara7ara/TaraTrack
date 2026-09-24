"""app.routers.recomendados - recomendados y BAN."""

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from app import repo
from app.db import get_connection
from app.web import templates

router = APIRouter()




@router.get("/recomendados", response_class=HTMLResponse)
def recomendados(request: Request, tipo: str = "", tanda: int = 0):
    with get_connection() as conn:
        recs, pages = repo.list_recommendations(
            conn, request.state.user_id, tipo=tipo, tanda=tanda, with_pages=True
        )
    return templates.TemplateResponse(
        request, "recommendations.html",
        {"recs": recs, "tipo": tipo, "tanda": tanda % pages, "pages": pages},
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
