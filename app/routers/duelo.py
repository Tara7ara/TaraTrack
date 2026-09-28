"""app.routers.duelo - duelo general A/B."""

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import repo
from app.db import get_connection
from app.web import templates

router = APIRouter()


@router.get("/duelo", response_class=HTMLResponse)
def duelo_general(request: Request):
    with get_connection() as conn:
        ids, es_anime = repo.duel_pool_for_user(conn, request.state.user_id)
        pair_ids = repo.random_duel_pair(conn, "entries", ids)
        pair = repo.get_entries_by_ids(conn, pair_ids) if pair_ids else []
        coverage = repo.duel_coverage(conn, "entries", ids)
        ranking = [
            {"title": e["title"], "image": e["image"], "elo": e["elo"], "href": f"/titulo/{e['tmdb_id']}/{e['type']}"}
            for e in sorted(repo.get_entries_by_ids(conn, ids), key=lambda e: e["elo"], reverse=True)[:8]
        ] if ids else []
    return templates.TemplateResponse(
        request,
        "duel.html",
        {
            "pair": pair, "coverage": coverage,
            "duelo_titulo": "Duelo",
            "duelo_url": "/duelo", "duelo_volver": "/vistas?orden=elo",
            "duelo_ambito": "el anime visto" if es_anime else "lo que has visto",
            "duelo_scope": "", "volver_txt": "Ranking completo en Vistas",
            "ranking": ranking,
        },
    )


@router.post("/duelo", response_class=HTMLResponse)
def duelo_general_votar(request: Request, a_id: int = Form(...), b_id: int = Form(...), resultado: float = Form(1.0)):
    with get_connection() as conn:
        repo.record_duel(conn, "entries", a_id, b_id, request.state.user_id, resultado)
    return RedirectResponse("/duelo", status_code=303)


@router.post("/duelo/elo/reiniciar", response_class=HTMLResponse)
def duelo_general_elo_reiniciar(request: Request):
    """Reinicia el Elo de toda la biblioteca vista y borra su historial de duelos (no
    toca listas ni waifus). Vuelve a /vistas con `elo_reset` para enseñar un aviso."""
    with get_connection() as conn:
        ids, _ = repo.duel_pool_for_user(conn, request.state.user_id)
        repo.reset_elo(conn, "entries", ids, request.state.user_id)
    return RedirectResponse("/vistas?elo_reset=1", status_code=303)
