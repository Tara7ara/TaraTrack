"""app.routers.estadisticas - estadísticas, resumen anual y discrepancias."""
import asyncio
from datetime import date

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import repo
from app.db import get_connection
from app.web import _recompute_with_status, templates

router = APIRouter()


@router.get("/estadisticas", response_class=HTMLResponse)
def estadisticas(request: Request, anio: int = 0, pestana: str = "genres"):
    uid = request.state.user_id
    with get_connection() as conn:
        stats = repo.get_stats(conn, uid)
        taste = repo.get_affinity_display(conn, uid)
        years = repo.get_available_years(conn, uid)
        year = anio if anio in years else (years[0] if years else date.today().year)
        year_stats = repo.get_year_stats(conn, uid, year) if years else None
        te_gusta_mas, te_gusta_menos = repo.get_rating_discrepancies(conn, uid)
        cal = repo.get_prediction_calibration(conn, uid)
        accuracy = repo.get_accuracy_history(conn, uid)
        history = repo.get_profile_history(conn, uid)
        cfg = repo.get_affinity_config(conn, uid)
        pendientes = repo.count_anilist_backfill_pending(conn)
    return templates.TemplateResponse(
        request, "stats.html",
        {
            "stats": stats, "taste": taste,
            "years": years, "year": year, "ystats": year_stats,
            "te_gusta_mas": te_gusta_mas, "te_gusta_menos": te_gusta_menos,
            "cal": cal, "acc": accuracy[-1] if accuracy else None,
            "acc_first": accuracy[0] if accuracy else None, "n_acc": len(accuracy),
            "history": list(reversed(history[:14])), "last": history[0] if history else None,
            "cfg": cfg, "pestana": pestana if pestana in ("genres", "tags", "studios") else "genres",
            "backfill_pendientes": pendientes, "title": "Estadísticas",
            "defaults": {
                "appetite_weights": repo.APPETITE_WEIGHTS, "quality_weights": repo.QUALITY_WEIGHTS,
                "appetite_exp": repo.AFFINITY_APPETITE_EXP, "quality_exp": repo.AFFINITY_QUALITY_EXP,
                "gap_threshold": repo.AFFINITY_GAP_THRESHOLD, "gap_factor": repo.AFFINITY_GAP_FACTOR,
                "predict_weights": repo.PREDICT_SIGNAL_WEIGHTS,
            },
        },
    )


@router.get("/estadisticas/episodios-favoritos", response_class=HTMLResponse)
def episodios_favoritos(request: Request):
    with get_connection() as conn:
        episodios = repo.list_favorite_episodes(conn, request.state.user_id)
    return templates.TemplateResponse(request, "favorite_episodes.html", {"episodios": episodios})


@router.get("/mosaico")
def mosaico():
    return RedirectResponse("/vistas", status_code=303)


# Rutas viejas: ahora son secciones de /estadisticas.
@router.get("/discrepancias")
def discrepancias():
    return RedirectResponse("/estadisticas#discrepancias", status_code=303)


@router.get("/resumen")
def resumen_anual(anio: int = 0):
    return RedirectResponse(f"/estadisticas{'?anio=' + str(anio) if anio else ''}#anio", status_code=303)


@router.get("/gustos")
def gustos(pestana: str = ""):
    return RedirectResponse(f"/estadisticas{'?pestana=' + pestana if pestana else ''}#gustos", status_code=303)


@router.post("/gustos/actualizar")
async def gustos_actualizar(request: Request):
    def _backfill():
        with get_connection() as conn:
            repo.backfill_anilist_profile(conn)
    asyncio.create_task(asyncio.to_thread(_recompute_with_status, request.state.user_id, _backfill))
    return RedirectResponse("/estadisticas#gustos", status_code=303)
