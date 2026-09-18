"""app.routers.historial - extraido de main.py en el split de modulos (ronda 2026-08-21)."""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app import repo
from app.db import get_connection
from app.web import templates

router = APIRouter()




@router.get("/historial", response_class=HTMLResponse)
def historial(request: Request):
    with get_connection() as conn:
        items = repo.list_history(conn, request.state.user_id)
    days = {}
    for item in items:
        days.setdefault((item["at"] or "")[:10], []).append(item)
    return templates.TemplateResponse(request, "history.html", {"days": days})
