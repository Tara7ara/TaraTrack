"""app.routers.thumbs - miniaturas de las portadas locales (ver app/thumbs.py)."""

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, RedirectResponse, Response

from app import thumbs
from app.db import get_connection

router = APIRouter()


def _es_safari(request: Request) -> bool:
    ua = request.headers.get("user-agent", "")
    if any(k in ua for k in ("iPhone", "iPad", "iPod")):
        return True
    return "AppleWebKit" in ua and "Safari/" in ua and not any(k in ua for k in ("Chrome/", "Chromium/", "Edg/", "OPR/", "Android"))


def _puede_guardar(request: Request, tmdb_id: int) -> bool:
    if not tmdb_id or not getattr(request.state, "is_admin", False) or _es_safari(request):
        return False
    with get_connection() as conn:
        row = conn.execute(
            """SELECT 1 FROM entries JOIN titles ON titles.id = entries.title_id
               WHERE titles.tmdb_id = ? AND entries.user_id = ? AND entries.status = 'watched'""",
            (tmdb_id, request.state.user_id),
        ).fetchone()
    return row is not None


@router.get("/fondo/{name}")
def fondo(request: Request, name: str, t: int = 0):
    """Fondo de una ficha. Si ya está guardado en el servidor, sale de ahí (para cualquier
    cuenta, es más rápido). Si no, solo se descarga y guarda cuando lo pide la cuenta admin
    y el título está visto; en el resto de casos se redirige a TMDB sin guardar nada. Si
    TMDB falla al guardar, también se redirige a TMDB para no dejar la portada vacía."""
    media = "image/png" if name.endswith(".png") else "image/jpeg"
    local = thumbs.local_backdrop(name)
    if local:
        return FileResponse(local, media_type=media)
    if not thumbs._BACKDROP_RE.match(name):
        return Response(status_code=404)
    if _puede_guardar(request, t):
        try:
            path = thumbs.ensure_backdrop(name)
            if path:
                return FileResponse(path, media_type=media)
        except Exception:
            pass
    return RedirectResponse(thumbs.tmdb_backdrop_url(name), status_code=302)


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
