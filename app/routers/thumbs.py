"""app.routers.thumbs - miniaturas de las portadas locales (ver app/thumbs.py)."""

from fastapi import APIRouter
from fastapi.responses import FileResponse, RedirectResponse, Response

from app import thumbs

router = APIRouter()


@router.get("/thumbs/{width}/{name}")
def miniatura(width: int, name: str):
    """Sirve la miniatura, creándola si hace falta. Si la portada no se puede procesar,
    redirige a la original en vez de dejar la casilla vacía."""
    try:
        path = thumbs.ensure_thumb(width, name)
    except Exception:
        return RedirectResponse(f"{thumbs.LOCAL_PREFIX}{name}", status_code=302)
    if not path:
        return Response(status_code=404)
    return FileResponse(path, media_type="image/jpeg")
