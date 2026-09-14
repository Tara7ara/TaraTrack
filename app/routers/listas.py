"""app.routers.listas - extraido de main.py en el split de modulos (ronda 2026-08-21)."""

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import repo, tmdb
from app.db import get_connection
from app.web import templates

router = APIRouter()




@router.get("/listas", response_class=HTMLResponse)
def listas(request: Request):
    with get_connection() as conn:
        lists = repo.list_lists(conn)
        waifus_preview = [w["profile_path"] for w in repo.list_waifus(conn)[:10]]
    return templates.TemplateResponse(
        request, "lists.html", {"lists": lists, "waifus_preview": waifus_preview}
    )




@router.post("/listas", response_class=HTMLResponse)
def crear_lista(request: Request, name: str = Form(...)):
    """Post normal + redirect (no htmx): el swap de <body> entero via hx-select
    daba pantalla en negro en el movil de Tara - swapear el body es fragil, mejor
    una recarga normal como el resto de altas de la app."""
    with get_connection() as conn:
        repo.create_list(conn, name)
    return RedirectResponse("/listas", status_code=303)




@router.post("/lista/{list_id}/renombrar", response_class=HTMLResponse)
def lista_renombrar(request: Request, list_id: int, name: str = Form(...)):
    with get_connection() as conn:
        repo.rename_list(conn, list_id, name)
    return RedirectResponse(f"/lista/{list_id}", status_code=303)




@router.post("/lista/{list_id}/borrar", response_class=HTMLResponse)
def lista_borrar(request: Request, list_id: int):
    with get_connection() as conn:
        repo.delete_list(conn, list_id)
    return RedirectResponse("/listas", status_code=303)




@router.post("/lista/{list_id}/mover/{item_id}/{direction}", response_class=HTMLResponse)
def lista_mover(request: Request, list_id: int, item_id: int, direction: str):
    with get_connection() as conn:
        repo.move_list_item(conn, list_id, item_id, direction)
    return RedirectResponse(f"/lista/{list_id}", status_code=303)




@router.post("/lista/{list_id}/orden", response_class=HTMLResponse)
def lista_orden(request: Request, list_id: int, modo: str = Form(...)):
    """Elegir que orden se enseña: manual (flechas) o por duelos (Elo) - los dos se
    guardan siempre, esto solo decide cual se ve (ver repo.record_duel)."""
    with get_connection() as conn:
        repo.set_list_order_mode(conn, list_id, modo)
    return RedirectResponse(f"/lista/{list_id}", status_code=303)




@router.post("/lista/{list_id}/elo/reiniciar", response_class=HTMLResponse)
def lista_elo_reiniciar(request: Request, list_id: int):
    """Reinicia a 1500 el Elo de esta lista y borra su historial de duelos - por si
    sale algo raro y Tara prefiere empezar el ranking de cero. Solo esta lista, no
    toca el Elo de otras listas ni de waifus. `elo_reset=1` en el redirect para el
    aviso visible (ver duelo_general_elo_reiniciar)."""
    with get_connection() as conn:
        ids = [item["item_id"] for item in repo.list_items_in_list(conn, list_id)]
        repo.reset_elo(conn, "list_items", ids)
    return RedirectResponse(f"/lista/{list_id}?elo_reset=1", status_code=303)




@router.get("/lista/{list_id}/duelo", response_class=HTMLResponse)
def lista_duelo(request: Request, list_id: int):
    """Ranking por duelos para una lista concreta - mismo mecanismo que /waifus/duelo."""
    with get_connection() as conn:
        list_row = conn.execute("SELECT * FROM lists WHERE id = ?", (list_id,)).fetchone()
        ids = [item["item_id"] for item in repo.list_items_in_list(conn, list_id)]
        pair_ids = repo.random_duel_pair(conn, "list_items", ids)
        pair = repo.get_list_items_by_ids(conn, list_id, pair_ids) if pair_ids else []
        coverage = repo.duel_coverage(conn, "list_items", ids)
    return templates.TemplateResponse(
        request,
        "duel.html",
        {
            "pair": pair, "coverage": coverage, "duelo_titulo": f"Duelo: {list_row['name']}",
            "duelo_url": f"/lista/{list_id}/duelo", "duelo_volver": f"/lista/{list_id}",
            "duelo_ambito": "esta lista",
        },
    )




@router.post("/lista/{list_id}/duelo", response_class=HTMLResponse)
def lista_duelo_votar(
    request: Request, list_id: int, a_id: int = Form(...), b_id: int = Form(...), resultado: float = Form(1.0)
):
    with get_connection() as conn:
        repo.record_duel(conn, "list_items", a_id, b_id, resultado)
    return RedirectResponse(f"/lista/{list_id}/duelo", status_code=303)




@router.get("/lista/{list_id}/buscar", response_class=HTMLResponse)
def lista_buscar(request: Request, list_id: int, q: str = ""):
    """Busqueda TMDB embebida en la lista, para llenarla sin ir titulo por titulo."""
    results = []
    if q.strip():
        try:
            results = tmdb.search(q)[:12]
        except Exception:
            results = []
    with get_connection() as conn:
        en_lista = {
            row["tmdb_id"]
            for row in conn.execute(
                """SELECT titles.tmdb_id FROM list_items
                   JOIN entries ON entries.id = list_items.entry_id
                   JOIN titles ON titles.id = entries.title_id
                   WHERE list_items.list_id = ?""",
                (list_id,),
            )
        }
    return templates.TemplateResponse(
        request,
        "partials/list_add_results.html",
        {"results": results, "query": q, "list_id": list_id, "en_lista": en_lista},
    )




@router.post("/lista/{list_id}/anadir/{tmdb_id}/{type}", response_class=HTMLResponse)
def lista_anadir(request: Request, list_id: int, tmdb_id: int, type: str):
    with get_connection() as conn:
        entry = repo.ensure_entry(conn, tmdb_id, type)
        repo.add_entry_to_list(conn, list_id, entry["id"])
    return RedirectResponse(f"/lista/{list_id}", status_code=303)




@router.get("/lista/{list_id}", response_class=HTMLResponse)
def lista_detalle(request: Request, list_id: int, elo_reset: str = ""):
    with get_connection() as conn:
        list_row = conn.execute("SELECT * FROM lists WHERE id = ?", (list_id,)).fetchone()
        entries = repo.list_items_in_list(conn, list_id)
        ids = [e["item_id"] for e in entries]
        elo_deltas = repo.get_elo_deltas(conn, "list_items", ids)
        coverage = repo.duel_coverage(conn, "list_items", ids)
    return templates.TemplateResponse(
        request,
        "list_detail.html",
        {
            "list_row": list_row, "entries": entries, "elo_deltas": elo_deltas, "coverage": coverage,
            "elo_reset": bool(elo_reset),
        },
    )
