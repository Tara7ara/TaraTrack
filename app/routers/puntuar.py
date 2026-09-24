"""app.routers.puntuar - cola de puntuar y examen por categorías."""

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import repo
from app.db import get_connection
from app.web import templates

router = APIRouter()




@router.post("/puntuar/{entry_id}/no-recuerdo", response_class=HTMLResponse)
def puntuar_no_recuerdo(request: Request, entry_id: int, season_number: int | None = Form(None)):
    """Atajo para lo importado de Trakt sin memoria real de la nota: 5 y comentario
    '-' de un toque, sin pasar por el examen. No es una nota real, es un relleno
    explicito para vaciar la cola sin fingir que sí se acuerda. Si el item de la cola
    era una temporada nueva sin puntuar (ver next_review_item), rellena esa temporada
    con set_season_rating en vez de pisar entries.rating entero - mismo atajo, pero
    en el sitio correcto para una serie que ya usa puntuacion por temporada."""
    with get_connection() as conn:
        entry = repo.get_owned_entry_with_title(conn, entry_id, request.state.user_id)
        if entry and season_number is not None:
            repo.set_season_rating(conn, entry_id, season_number, 5.0, "-")
        elif entry:
            repo.mark_watched(conn, entry_id, 5.0, "-")
    return RedirectResponse("/puntuar", status_code=303)




@router.get("/puntuar", response_class=HTMLResponse)
def puntuar(request: Request, excluir: str = ""):
    excluir_ids = [int(x) for x in excluir.split(",") if x.strip().isdigit()]
    with get_connection() as conn:
        total = repo.count_review_queue(conn, request.state.user_id)
        entry = repo.next_review_item(conn, request.state.user_id, excluir_ids)
        if entry is None and excluir_ids:
            # Ya ha pasado de todas en esta ronda - empieza otra desde cero.
            excluir_ids = []
            entry = repo.next_review_item(conn, request.state.user_id, excluir_ids)
    excluir_siguiente = ",".join(str(i) for i in [*excluir_ids, *([entry["id"]] if entry else [])])
    return templates.TemplateResponse(
        request,
        "review_queue.html",
        {"entry": entry, "total": total, "excluir_siguiente": excluir_siguiente},
    )
