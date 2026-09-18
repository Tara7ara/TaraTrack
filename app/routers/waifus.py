"""app.routers.waifus - extraido de main.py en el split de modulos (ronda 2026-08-21)."""

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import anime, repo
from app.db import get_connection
from app.web import templates

router = APIRouter()




@router.get("/waifus", response_class=HTMLResponse)
def waifus(request: Request, elo_reset: str = ""):
    with get_connection() as conn:
        chars = repo.list_waifus(conn, request.state.user_id)
        order_mode = repo.get_setting(conn, f"waifus_order_mode:{request.state.user_id}", "manual")
        ids = [w["fav_id"] for w in chars]
        elo_deltas = repo.get_elo_deltas(conn, "favorite_characters", ids)
        coverage = repo.duel_coverage(conn, "favorite_characters", ids)
    return templates.TemplateResponse(
        request,
        "waifus.html",
        {
            "chars": chars, "order_mode": order_mode, "elo_deltas": elo_deltas, "coverage": coverage,
            "elo_reset": bool(elo_reset),
        },
    )




@router.get("/waifus/buscar", response_class=HTMLResponse)
def waifus_buscar(request: Request, q: str = ""):
    """Buscador de personajes para poner estrellas en bloque: primero los ya cacheados
    y, si no hay ninguno, busqueda global (AniList con fallback a MyAnimeList). Los
    externos solo se pueden añadir si su anime esta en la biblioteca (el personaje
    necesita un titulo al que colgarse)."""
    results, externals, api_down = [], [], False
    with get_connection() as conn:
        if q.strip():
            results = repo.search_characters_local(conn, q, request.state.user_id)
            if not results:
                try:
                    found, api_down = anime.search_characters(q)
                except Exception:
                    found, api_down = [], False
                for ex in found[:8]:
                    match = repo.match_library_title(conn, ex["media"], request.state.user_id)
                    externals.append({**ex, "match": dict(match) if match else None})
    return templates.TemplateResponse(
        request,
        "partials/waifu_search_results.html",
        {"results": results, "externals": externals, "query": q, "api_down": api_down},
    )




@router.post("/waifus/anadir", response_class=HTMLResponse)
def waifus_anadir(
    request: Request,
    title_id: int = Form(...),
    char_id: int = Form(...),
    name: str = Form(...),
    image_url: str = Form(""),
):
    """Alta de personaje externo desde /waifus: lo cachea en su titulo y le pone la
    estrella del tiron (que para eso lo estaba buscando)."""
    with get_connection() as conn:
        title_row = conn.execute("SELECT * FROM titles WHERE id = ?", (title_id,)).fetchone()
        # Sin filtrar por user_id, esto engancharia el personaje favorito a la entry
        # de OTRO usuario si ya tenia este titulo en su biblioteca (mismo tipo de bug
        # ya arreglado en /pendiente y /titulo, ver notas del 2026-09-17).
        entry = conn.execute(
            "SELECT id FROM entries WHERE title_id = ? AND user_id = ?", (title_id, request.state.user_id)
        ).fetchone()
        if title_row and entry:
            repo.add_character_manual(conn, title_row, char_id, name, image_url.strip() or None)
            char = conn.execute(
                "SELECT id FROM characters WHERE title_id = ? AND tmdb_person_id = ?",
                (title_id, char_id),
            ).fetchone()
            if char:
                conn.execute(
                    "INSERT OR IGNORE INTO favorite_characters (entry_id, character_id) VALUES (?, ?)",
                    (entry["id"], char["id"]),
                )
    return RedirectResponse("/waifus", status_code=303)




@router.post("/waifu/{fav_id}/mover/{direction}", response_class=HTMLResponse)
def waifu_mover(request: Request, fav_id: int, direction: str):
    with get_connection() as conn:
        repo.move_waifu(conn, fav_id, direction, request.state.user_id)
    return RedirectResponse("/waifus", status_code=303)




@router.post("/waifus/elo/reiniciar", response_class=HTMLResponse)
def waifus_elo_reiniciar(request: Request):
    """Reinicia a 1500 el Elo de TUS waifus y borra su historial de duelos - por si
    sale algo raro y prefieres empezar el ranking de cero. `elo_reset=1` en el
    redirect para el aviso visible (ver duelo_general_elo_reiniciar)."""
    with get_connection() as conn:
        ids = [w["fav_id"] for w in repo.list_waifus(conn, request.state.user_id)]
        repo.reset_elo(conn, "favorite_characters", ids)
    return RedirectResponse("/waifus?elo_reset=1", status_code=303)




@router.post("/waifus/orden", response_class=HTMLResponse)
def waifus_orden(request: Request, modo: str = Form(...)):
    with get_connection() as conn:
        repo.set_waifus_order_mode(conn, request.state.user_id, modo)
    return RedirectResponse("/waifus", status_code=303)




@router.get("/waifus/duelo", response_class=HTMLResponse)
def waifus_duelo(request: Request):
    """Ranking por duelos sobre TUS waifus: con 60 waifus las flechas nunca llegan a
    dar un orden real - elegir A o B unas cuantas veces da un orden mas honesto (ver
    repo.record_duel)."""
    with get_connection() as conn:
        ids = [w["fav_id"] for w in repo.list_waifus(conn, request.state.user_id)]
        pair_ids = repo.random_duel_pair(conn, "favorite_characters", ids)
        pair = repo.get_waifus_by_ids(conn, pair_ids) if pair_ids else []
        coverage = repo.duel_coverage(conn, "favorite_characters", ids)
    return templates.TemplateResponse(
        request,
        "duel.html",
        {
            "pair": pair, "coverage": coverage, "duelo_titulo": "Duelo: Waifus",
            "duelo_url": "/waifus/duelo", "duelo_volver": "/waifus", "duelo_ambito": "waifus",
        },
    )




@router.post("/waifus/duelo", response_class=HTMLResponse)
def waifus_duelo_votar(request: Request, a_id: int = Form(...), b_id: int = Form(...), resultado: float = Form(1.0)):
    with get_connection() as conn:
        repo.record_duel(conn, "favorite_characters", a_id, b_id, request.state.user_id, resultado)
    return RedirectResponse("/waifus/duelo", status_code=303)
