"""app.routers.vistas - extraido de main.py en el split de modulos (ronda 2026-08-21)."""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app import repo
from app.db import get_connection
from app.web import templates

router = APIRouter()




@router.get("/vistas", response_class=HTMLResponse)
def vistas(
    request: Request, orden: str = "recientes", tipo: str = "", q: str = "", genero: str = "",
    direccion: str = "", elo_reset: str = "",
):
    with get_connection() as conn:
        entries = repo.list_watched(conn, orden, tipo, q, genero, direccion)
        default_list = repo.get_default_list(conn)
        generos = repo.list_genres(conn)
        coverage = repo.duel_coverage(conn, "entries", repo.list_watched_ids(conn))
    direccion_actual = direccion if direccion in ("asc", "desc") else repo.ORDENES_VISTAS_DEFAULT_DIR.get(orden, "desc")
    return templates.TemplateResponse(
        request,
        "watched.html",
        {
            "entries": entries,
            "orden": orden,
            "tipo": tipo,
            "q": q,
            "genero": genero,
            "generos": generos,
            "direccion": direccion_actual,
            "favoritos_id": default_list["id"],
            "coverage": coverage,
            "elo_reset": bool(elo_reset),
        },
    )
