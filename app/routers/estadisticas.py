"""app.routers.estadisticas - extraido de main.py en el split de modulos (ronda 2026-08-21)."""
from datetime import date

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app import repo
from app.db import get_connection
from app.web import templates

router = APIRouter()




@router.get("/estadisticas", response_class=HTMLResponse)
def estadisticas(request: Request):
    with get_connection() as conn:
        stats = repo.get_stats(conn)
        taste = repo.get_affinity_display(conn)
    return templates.TemplateResponse(request, "stats.html", {"stats": stats, "taste": taste})




@router.get("/estadisticas/episodios-favoritos", response_class=HTMLResponse)
def episodios_favoritos(request: Request):
    with get_connection() as conn:
        episodios = repo.list_favorite_episodes(conn)
    return templates.TemplateResponse(request, "favorite_episodes.html", {"episodios": episodios})




@router.get("/mosaico", response_class=HTMLResponse)
def mosaico(request: Request):
    with get_connection() as conn:
        posters = repo.list_all_posters(conn)
    return templates.TemplateResponse(request, "mosaic.html", {"posters": posters})




@router.get("/discrepancias", response_class=HTMLResponse)
def discrepancias(request: Request):
    with get_connection() as conn:
        te_gusta_mas, te_gusta_menos = repo.get_rating_discrepancies(conn)
    return templates.TemplateResponse(
        request, "discrepancies.html", {"te_gusta_mas": te_gusta_mas, "te_gusta_menos": te_gusta_menos}
    )




@router.get("/resumen", response_class=HTMLResponse)
def resumen_anual(request: Request, anio: int = 0):
    with get_connection() as conn:
        years = repo.get_available_years(conn)
        year = anio if anio in years else (years[0] if years else date.today().year)
        year_stats = repo.get_year_stats(conn, year) if years else None
    return templates.TemplateResponse(
        request, "yearly.html", {"years": years, "year": year, "stats": year_stats}
    )
