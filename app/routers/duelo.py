"""app.routers.duelo - extraido de main.py en el split de modulos (ronda 2026-08-21)."""

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import repo
from app.db import get_connection
from app.web import templates

router = APIRouter()




@router.get("/duelo", response_class=HTMLResponse)
def duelo_general(request: Request):
    """Duelo A/B (El usuario: "el duelo es solo de animes", 2026-08-13) - pero para quien
    no ve anime (2026-09-18: "para las otras personas no se como adaptarlo") ese
    pool sale vacio y la pantalla no serviria de nada. repo.duel_pool_for_user cae a
    TODO lo visto cuando el de anime no llega a 2 titulos, sin tocar el
    comportamiento de quien si tiene anime de sobra (El usuario). Mismo mecanismo que
    /waifus/duelo y /lista/{id}/duelo pero sobre entries.elo. El ranking resultante
    se ve ordenando /vistas por "Elo", no hay una pagina de ranking aparte para esto."""
    with get_connection() as conn:
        ids, es_anime = repo.duel_pool_for_user(conn, request.state.user_id)
        pair_ids = repo.random_duel_pair(conn, "entries", ids)
        pair = repo.get_entries_by_ids(conn, pair_ids) if pair_ids else []
        coverage = repo.duel_coverage(conn, "entries", ids)
    return templates.TemplateResponse(
        request,
        "duel.html",
        {
            "pair": pair, "coverage": coverage,
            "duelo_titulo": "Duelo: Anime" if es_anime else "Duelo",
            "duelo_url": "/duelo", "duelo_volver": "/vistas?orden=elo",
            "duelo_ambito": "el anime visto" if es_anime else "lo que has visto",
        },
    )




@router.post("/duelo", response_class=HTMLResponse)
def duelo_general_votar(request: Request, a_id: int = Form(...), b_id: int = Form(...), resultado: float = Form(1.0)):
    with get_connection() as conn:
        repo.record_duel(conn, "entries", a_id, b_id, request.state.user_id, resultado)
    return RedirectResponse("/duelo", status_code=303)




@router.post("/duelo/elo/reiniciar", response_class=HTMLResponse)
def duelo_general_elo_reiniciar(request: Request):
    """Reinicia a 1500 el Elo de TODA la biblioteca vista y borra su historial de
    duelos - por si sale algo raro. No toca listas ni waifus (tablas separadas).
    Vuelve a /vistas (de donde sale el boton) en vez de saltar a /duelo - mismo
    patron que listas/waifus, con un aviso visible (`elo_reset`) porque el numero de
    Elo por si solo, sin la señal de haber cambiado de pantalla, pasaba desapercibido
    (El usuario, 2026-08-14: "en las listas no se nota... pero si que reinicia")."""
    with get_connection() as conn:
        ids, _ = repo.duel_pool_for_user(conn, request.state.user_id)
        repo.reset_elo(conn, "entries", ids, request.state.user_id)
    return RedirectResponse("/vistas?elo_reset=1", status_code=303)
