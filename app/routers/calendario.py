"""app.routers.calendario - calendario semanal, calendario de temporada y sync."""
import asyncio
import logging
from datetime import date
from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import anime, repo
from app.db import get_connection
from app.matching import (
    _resolve_calendar_match,
)
from app.web import _recompute_with_status, templates

router = APIRouter()




@router.get("/calendario/abrir", response_class=HTMLResponse)
def calendario_abrir(request: Request, title: str = "", romaji: str = "", anilist_id: int = 0):
    match = _resolve_calendar_match(title.strip(), romaji.strip()) if title.strip() else None
    if match:
        if anilist_id:
            with get_connection() as conn:
                repo.ensure_title(conn, match["tmdb_id"], match["type"])
                repo.link_calendar_card(conn, anilist_id, match["tmdb_id"], match["type"])
        return RedirectResponse(f"/titulo/{match['tmdb_id']}/{match['type']}", status_code=303)
    return RedirectResponse(f"/buscar?q={quote(title)}", status_code=303)




@router.post("/calendario/anadir", response_class=HTMLResponse)
def calendario_anadir(
    request: Request, title: str = Form(...), romaji: str = Form(""), predict: str = Form(""),
    anilist_id: int = Form(0),
):
    """+ Pendientes desde la tarjeta del calendario de temporada, sin salir de la
    página. Guarda también el % que enseñaba la tarjeta (solo si la entry es nueva),
    para comparar después expectativa y nota."""
    r = _resolve_calendar_match(title.strip(), romaji.strip())
    if not r:
        return templates.TemplateResponse(
            request, "partials/calendar_add_result.html", {"ok": False, "title": title}
        )
    with get_connection() as conn:
        entry = repo.ensure_entry(conn, r["tmdb_id"], r["type"], request.state.user_id)
        repo.link_calendar_card(conn, anilist_id, r["tmdb_id"], r["type"])
        if predict.strip().isdigit():
            repo.set_predicted_score(conn, entry["id"], int(predict))
    return templates.TemplateResponse(
        request, "partials/calendar_add_result.html", {"ok": True, "tmdb_id": r["tmdb_id"], "type": r["type"]}
    )




def _calendario_response(request: Request, conn):
    """Hoy y futuro primero (lo que quieres ver al abrir), los dias ya pasados plegados al final."""
    today = date.today().isoformat()
    days, past_days = {}, {}
    for item in repo.list_calendar(conn, request.state.user_id):
        target = days if item["air_date"] >= today else past_days
        target.setdefault(item["air_date"], []).append(item)
    past_days = dict(sorted(past_days.items(), reverse=True))
    show_anime_calendar = repo.get_show_anime_calendar(
        conn, request.state.user_id, default=repo.user_has_anime(conn, request.state.user_id)
    )
    return templates.TemplateResponse(
        request, "calendar.html",
        {
            "days": days, "past_days": past_days, "today": today, "sync_status": repo.get_sync_status(conn),
            "show_anime_calendar": show_anime_calendar,
        },
    )




@router.get("/calendario", response_class=HTMLResponse)
def calendario(request: Request):
    with get_connection() as conn:
        return _calendario_response(request, conn)




_SEASON_ORDER = ["WINTER", "SPRING", "SUMMER", "FALL"]


_SEASON_ES = {"WINTER": "Invierno", "SPRING": "Primavera", "SUMMER": "Verano", "FALL": "Otoño"}


_WEEKDAY_ES = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]




def _current_season() -> str:
    month = date.today().month
    if month in (12, 1, 2):
        return "WINTER"
    if month in (3, 4, 5):
        return "SPRING"
    if month in (6, 7, 8):
        return "SUMMER"
    return "FALL"




async def _refresh_season_cache_async(season: str, year: int):
    """Refresca la cache de una temporada en background sin bloquear la peticion que
    la disparo - mismo patron que el backfill de perfil (asyncio.create_task +
    to_thread)."""
    def _run():
        try:
            items = anime.get_seasonal_anime(season, year)
            with get_connection() as conn:
                repo.save_season_cache(conn, season, year, items)
            logging.info("season_cache: %s %s refrescada a mano", season, year)
        except Exception:
            logging.warning("season_cache: fallo al refrescar %s %s", season, year)
    asyncio.create_task(asyncio.to_thread(_run))




@router.get("/calendario/anual", response_class=HTMLResponse)
async def calendario_anual(
    request: Request, year: int = 0, season: str = "", vista: str = "dia", afinidad_alta: str = ""
):
    """Calendario de temporada: todo el anime TV/ONA de una temporada según AniList,
    una temporada a la vez (por defecto la actual), por día de emisión, por % o por
    nota.

    Sale de la caché de la BBDD. La primera vez que se pide una temporada se espera a
    AniList; si la caché tiene más de 24 h se enseña igual y se refresca en segundo
    plano. La temporada actual se refresca sola cada día (season_cache_loop)."""
    year = year or date.today().year
    if season not in _SEASON_ORDER:
        season = _current_season()

    with get_connection() as conn:
        items, age_hours = repo.get_cached_season(conn, season, year)
    api_down = False
    if items is None:
        try:
            items = anime.get_seasonal_anime(season, year)
            with get_connection() as conn:
                repo.save_season_cache(conn, season, year, items)
        except Exception:
            items, api_down = [], True
    elif age_hours is not None and age_hours > 24:
        await _refresh_season_cache_async(season, year)

    with get_connection() as conn:
        profile = repo.build_taste_profile(conn, request.state.user_id)
        pendientes_perfil = repo.count_anilist_backfill_pending(conn)
        weekday_overrides = repo.get_weekday_overrides(conn)
        in_library = repo.library_status_for_cards(conn, request.state.user_id, items)
    for item in items:
        # Día de emisión corregido a mano: pisa el calculado en UTC (tabla
        # weekday_overrides).
        if item["anilist_id"] in weekday_overrides:
            item["weekday"] = weekday_overrides[item["anilist_id"]]
            item["weekday_overridden"] = True
        else:
            item["weekday_overridden"] = False
        item["library_status"] = in_library.get(item["anilist_id"])
        detail = repo.predict_score_detail(item, profile)
        item["predict"] = detail["score"] if detail else None
        item["predict_confidence"] = detail["confidence"] if detail else None
        # "Candidato a obra maestra" exige apoyo de tags, estudio o precuela, no solo
        # un género (ver repo.MASTERPIECE_MIN_CONFIDENCE).
        item["masterpiece_candidate"] = (
            item["predict"] is not None and item["predict"] >= 95
            and item["predict_confidence"] is not None
            and item["predict_confidence"] > repo.MASTERPIECE_MIN_CONFIDENCE
        )
    if afinidad_alta:
        items = [i for i in items if i["masterpiece_candidate"]]

    dias, sin_dia, por_porcentaje, por_nota = None, None, None, None
    if vista == "porcentaje":
        por_porcentaje = sorted(items, key=lambda i: i["predict"] if i["predict"] is not None else -1, reverse=True)
    elif vista == "nota":
        # Orden por nota = item["score"], la media de AniList que ya enseña la tarjeta.
        por_nota = sorted(items, key=lambda i: i["score"] if i["score"] is not None else -1, reverse=True)
    else:
        vista = "dia"
        dias = {i: [] for i in range(7)}
        sin_dia = []
        for item in items:
            (dias[item["weekday"]] if item["weekday"] is not None else sin_dia).append(item)
        dias = [(_WEEKDAY_ES[i], dias[i]) for i in range(7)]

    return templates.TemplateResponse(
        request,
        "yearly_calendar.html",
        {
            "year": year, "season": season, "season_es": _SEASON_ES[season], "vista": vista,
            "dias": dias, "sin_dia": sin_dia, "por_porcentaje": por_porcentaje, "por_nota": por_nota,
            "api_down": api_down, "total": len(items),
            "prev_year": year - 1, "next_year": year + 1,
            "perfil_cubierto": profile["covered"], "perfil_pendiente": pendientes_perfil,
            "afinidad_alta": afinidad_alta,
        },
    )




@router.post("/calendario/anual/perfil", response_class=HTMLResponse)
async def calendario_anual_perfil(request: Request, year: int = 0, season: str = "", vista: str = "dia"):
    """Rellena los datos de AniList del perfil de gustos (repo.backfill_anilist_profile)
    en segundo plano y redirige al instante: puede tardar minutos, más que el timeout
    del proxy. El indicador de /afinidad/estado cubre la espera."""
    def _backfill():
        with get_connection() as conn:
            repo.backfill_anilist_profile(conn)
    asyncio.create_task(asyncio.to_thread(_recompute_with_status, request.state.user_id, _backfill))
    return RedirectResponse(f"/calendario/anual?year={year}&season={season}&vista={vista}", status_code=303)




@router.post("/calendario/sincronizar")
def calendario_sincronizar(request: Request):
    """POST normal + redirect, como crear_lista: sustituir el <body> entero con htmx
    daba pantalla en negro en móvil."""
    repo.sync_library()
    return RedirectResponse("/calendario", status_code=303)
