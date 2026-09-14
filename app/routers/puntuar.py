"""app.routers.puntuar - extraido de main.py en el split de modulos (ronda 2026-08-21)."""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import repo
from app.db import get_connection
from app.web import templates

router = APIRouter()




@router.post("/puntuar/{entry_id}/no-recuerdo", response_class=HTMLResponse)
def puntuar_no_recuerdo(entry_id: int):
    """Atajo para lo importado de Trakt sin memoria real de la nota: 5 y comentario
    '-' de un toque, sin pasar por el examen. No es una nota real, es un relleno
    explicito para vaciar la cola sin fingir que sí se acuerda."""
    with get_connection() as conn:
        repo.mark_watched(conn, entry_id, 5.0, "-")
    return RedirectResponse("/puntuar", status_code=303)




@router.get("/puntuar", response_class=HTMLResponse)
def puntuar(request: Request, excluir: str = ""):
    excluir_ids = [int(x) for x in excluir.split(",") if x.strip().isdigit()]
    with get_connection() as conn:
        total = repo.count_review_queue(conn)
        entry = repo.next_review_item(conn, excluir_ids)
        if entry is None and excluir_ids:
            # Ya ha pasado de todas en esta ronda - empieza otra desde cero.
            excluir_ids = []
            entry = repo.next_review_item(conn, excluir_ids)
    excluir_siguiente = ",".join(str(i) for i in [*excluir_ids, *([entry["id"]] if entry else [])])
    return templates.TemplateResponse(
        request,
        "review_queue.html",
        {"entry": entry, "total": total, "excluir_siguiente": excluir_siguiente},
    )
