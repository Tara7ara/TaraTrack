"""app.routers.duelo - extraido de main.py en el split de modulos (ronda 2026-08-21)."""

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import repo
from app.db import get_connection
from app.web import templates

router = APIRouter()




@router.get("/duelo", response_class=HTMLResponse)
def duelo_general(request: Request):
    """Duelo A/B contra todo el ANIME visto, series y pelis juntas (Tara: "el duelo es
    solo de animes", 2026-08-13, corrigiendo mi primera version que cogia toda la
    biblioteca) - mismo mecanismo que /waifus/duelo y /lista/{id}/duelo pero sobre
    entries.elo, con el pool filtrado por repo.list_watched_ids. El ranking resultante
    se ve ordenando /vistas por "Elo", no hay una pagina de ranking aparte para esto."""
    with get_connection() as conn:
        ids = repo.list_watched_ids(conn)
        pair_ids = repo.random_duel_pair(conn, "entries", ids)
        pair = repo.get_entries_by_ids(conn, pair_ids) if pair_ids else []
        coverage = repo.duel_coverage(conn, "entries", ids)
    return templates.TemplateResponse(
        request,
        "duel.html",
        {
            "pair": pair, "coverage": coverage, "duelo_titulo": "Duelo: Anime",
            "duelo_url": "/duelo", "duelo_volver": "/vistas?orden=elo", "duelo_ambito": "el anime visto",
        },
    )




@router.post("/duelo", response_class=HTMLResponse)
def duelo_general_votar(request: Request, a_id: int = Form(...), b_id: int = Form(...), resultado: float = Form(1.0)):
    with get_connection() as conn:
        repo.record_duel(conn, "entries", a_id, b_id, resultado)
    return RedirectResponse("/duelo", status_code=303)




@router.post("/duelo/elo/reiniciar", response_class=HTMLResponse)
def duelo_general_elo_reiniciar(request: Request):
    """Reinicia a 1500 el Elo de TODA la biblioteca vista y borra su historial de
    duelos - por si sale algo raro. No toca listas ni waifus (tablas separadas).
    Vuelve a /vistas (de donde sale el boton) en vez de saltar a /duelo - mismo
    patron que listas/waifus, con un aviso visible (`elo_reset`) porque el numero de
    Elo por si solo, sin la señal de haber cambiado de pantalla, pasaba desapercibido
    (Tara, 2026-08-14: "en las listas no se nota... pero si que reinicia")."""
    with get_connection() as conn:
        repo.reset_elo(conn, "entries", repo.list_watched_ids(conn))
    return RedirectResponse("/vistas?elo_reset=1", status_code=303)
