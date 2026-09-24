"""app.routers.pendientes - pendientes, Continuar viendo y acciones de sus tarjetas."""
import logging
from collections import Counter
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import repo, web
from app.db import get_connection
from app.web import templates

router = APIRouter()




@router.get("/", response_class=HTMLResponse)
def home():
    # La pantalla por defecto son los pendientes (semanales + series/pelis enteras).
    return RedirectResponse("/pendientes")




def _parse_tags(raw: str) -> list[str]:
    """"Fantasy,-Harem,-Isekai" -> ["Fantasy", "-Harem", "-Isekai"]: con "-" delante
    se excluye, sin nada se exige."""
    return list(dict.fromkeys(t.strip() for t in raw.split(",") if t.strip().lstrip("-")))


def _entry_tags(entry) -> list[str]:
    """Generos + tags de AniList del titulo (solo anime emparejado con AniList los tiene)."""
    out = []
    for col in ("anilist_genres", "anilist_tags"):
        out += [t.strip() for t in (entry[col] or "").split(",") if t.strip()]
    return list(dict.fromkeys(out))


def _matches_tags(entry, tokens: list[str]) -> bool:
    """Filtro por tags de /pendientes: todos los incluidos y ninguno de los excluidos.
    Un título sin tags de AniList no entra en cuanto hay algún filtro."""
    if not tokens:
        return True
    have = {t.casefold() for t in _entry_tags(entry)}
    if not have:
        return False
    for tok in tokens:
        excluded = tok.startswith("-")
        if (tok.lstrip("-").casefold() in have) == excluded:
            return False
    return True


@router.get("/pendientes", response_class=HTMLResponse)
def pendientes(
    request: Request, tipo: str = "", orden: str = "anadido", q: str = "", genero: str = "",
    direccion: str = "", afinidad_alta: str = "", tags: str = "",
):
    with get_connection() as conn:
        try:
            # La foto diaria del perfil no es vital: si coincide con el recálculo de
            # fondo y da "database is locked", la página sigue sin ella.
            repo.snapshot_profile_progress(conn, request.state.user_id)
        except Exception:
            logging.warning("snapshot_profile_progress: fallo, se salta esta vez", exc_info=True)
        continuar = repo.list_continue_watching(conn, request.state.user_id)
        nuevas = repo.list_new_airing(conn, request.state.user_id)
        shown = {c["id"] for c in continuar} | {n["id"] for n in nuevas}
        # "prediccion" no es una columna SQL - se pide con el orden por defecto y se
        # reordena en Python despues de calcular el % de cada entry.
        sql_orden = orden if orden != "prediccion" else "anadido"
        pendientes = [
            e for e in repo.list_pending(conn, request.state.user_id, tipo, sql_orden, q, genero, direccion)
            if e["id"] not in shown
        ]
        entries = repo.add_predictions(conn, request.state.user_id, pendientes)
        if orden == "prediccion":
            entries.sort(
                key=lambda e: e["predict"] if e["predict"] is not None else -1,
                reverse=(direccion != "asc"),
            )
        # Filtro "solo 95% o más": el percentil está calibrado contra la propia
        # biblioteca, así que significa lo mismo que en el calendario de temporada.
        if afinidad_alta:
            entries = [e for e in entries if e["predict"] is not None and e["predict"] >= 95]
        generos = repo.list_genres(conn)
    tag_tokens = _parse_tags(tags)
    tag_counts = Counter(t for e in entries for t in _entry_tags(e))
    entries = [e for e in entries if _matches_tags(e, tag_tokens)]
    base_qs = urlencode(
        {"tipo": tipo, "orden": orden, "q": q, "genero": genero,
         "afinidad_alta": afinidad_alta, "direccion": direccion}
    )
    tag_chips = [
        (tok, ",".join(o for o in tag_tokens if o != tok)) for tok in tag_tokens
    ]
    active = {t.lstrip("-").casefold() for t in tag_tokens}
    tag_options = sorted(
        ((t, n) for t, n in tag_counts.items() if t.casefold() not in active),
        key=lambda x: x[0].casefold(),
    )
    direccion_actual = direccion if direccion in ("asc", "desc") else repo.ORDENES_PENDIENTES_DEFAULT_DIR.get(orden, "desc")
    return templates.TemplateResponse(
        request,
        "pending.html",
        {
            "entries": entries,
            "continuar": continuar,
            "nuevas": nuevas,
            "tipo": tipo,
            "orden": orden,
            "q": q,
            "genero": genero,
            "generos": generos,
            "direccion": direccion_actual,
            "afinidad_alta": afinidad_alta,
            "tags": ",".join(tag_tokens),
            "tag_chips": tag_chips,
            "tag_options": tag_options,
            "base_qs": base_qs,
        },
    )




def _home_card_response(request: Request, entry_id: int, confirm_all=False, oob: str = ""):
    """`oob` (indicador de Puntuar, ver web.render_nav_cola_oob) solo lo pasan las rutas
    que pueden cambiar la cola. get_home_card ya comprueba la propiedad: si la entry no
    es de este usuario, no hay tarjeta."""
    with get_connection() as conn:
        card = repo.get_home_card(conn, entry_id, request.state.user_id)
    html = templates.env.get_template("partials/home_card.html").render(
        {"request": request, "c": card, "confirm_all": confirm_all}
    )
    return HTMLResponse(html + oob)




@router.get("/entrada/{entry_id}/tarjeta", response_class=HTMLResponse)
def tarjeta_inicio(request: Request, entry_id: int):
    """Re-render de una tarjeta de inicio (el 'no' de la confirmacion del doble tick)."""
    return _home_card_response(request, entry_id)




@router.get("/entrada/{entry_id}/todos/confirmar", response_class=HTMLResponse)
def confirmar_todos_episodios(request: Request, entry_id: int):
    """Primer toque del doble tick: la tarjeta pasa a modo confirmación. Confirmación
    renderizada por el servidor: window.confirm no salta en algunos navegadores móviles."""
    return _home_card_response(request, entry_id, confirm_all=True)




@router.post("/entrada/{entry_id}/todos", response_class=HTMLResponse)
def marcar_todos_episodios(request: Request, entry_id: int):
    """El doble tick (ya confirmado): marca vistos TODOS los episodios emitidos."""
    with get_connection() as conn:
        entry = repo.get_owned_entry_with_title(conn, entry_id, request.state.user_id)
        oob = ""
        if entry:
            repo.mark_all_aired_watched(conn, entry_id)
            oob = web.render_nav_cola_oob(conn, request.state.user_id)
    return _home_card_response(request, entry_id, oob=oob)




@router.post("/entrada/{entry_id}/siguiente", response_class=HTMLResponse)
def marcar_siguiente_episodio(request: Request, entry_id: int):
    """Check de la tarjeta de inicio: marca visto el siguiente episodio emitido y
    ofrece el aviso "¿comentas?" en el mismo sitio."""
    with get_connection() as conn:
        entry = repo.get_owned_entry_with_title(conn, entry_id, request.state.user_id)
        oob = ""
        if entry:
            ep = repo.next_unwatched_episode(conn, entry["title_id"], entry["rewatch_started_at"], entry["user_id"])
            if ep:
                just_watched = repo.toggle_episode(conn, ep["id"], entry["user_id"])
                oob = web.render_nav_cola_oob(conn, request.state.user_id)
                if just_watched:
                    oob += templates.env.get_template("partials/comment_toast.html").render({
                        "tmdb_id": entry["tmdb_id"], "type": entry["type"], "episode_id": ep["id"],
                        "season_number": ep["season_number"], "episode_number": ep["episode_number"],
                    })
    return _home_card_response(request, entry_id, oob=oob)




@router.get("/sorprendeme")
def sorprendeme(request: Request, tipo: str = "", excluir: int = 0):
    """Un pendiente al azar (respetando Series/Pelis): portada grande + sinopsis en
    vez de ir directo a la ficha, para poder decidir "ahora no" con 'Otro' sin
    perder tiempo entrando y saliendo de fichas."""
    with get_connection() as conn:
        entry = repo.random_pending(conn, request.state.user_id, tipo, excluir=excluir or None)
    return templates.TemplateResponse(request, "surprise.html", {"entry": entry, "tipo": tipo})




@router.post("/pendiente/{tmdb_id}/{type}", response_class=HTMLResponse)
def anadir_pendiente(request: Request, tmdb_id: int, type: str):
    """Toggle: si ya esta pendiente lo quita entero (p.ej. una prueba anadida sin
    querer desde una lista); si no, lo anade."""
    with get_connection() as conn:
        title_row = repo.get_title(conn, tmdb_id)
        # Filtrado por user_id: si no, "+ Pendientes" encontraría la entry de otro
        # usuario y la borraría en vez de crear la propia.
        existing = (
            conn.execute(
                "SELECT * FROM entries WHERE title_id = ? AND user_id = ?",
                (title_row["id"], request.state.user_id),
            ).fetchone()
            if title_row else None
        )
        if existing and existing["status"] == "pending":
            repo.remove_pending_entry(conn, existing["id"])
            state = "new"
        else:
            repo.ensure_entry(conn, tmdb_id, type, request.state.user_id)
            state = "pending"
    return templates.TemplateResponse(
        request, "partials/entry_actions.html", {"tmdb_id": tmdb_id, "type": type, "state": state}
    )




@router.get("/pendiente/{tmdb_id}/{type}/estado", response_class=HTMLResponse)
def pendiente_estado(request: Request, tmdb_id: int, type: str):
    """El 'No' de la confirmacion: vuelve al estado real (sin asumir que sigue pending)."""
    with get_connection() as conn:
        state = _entry_state(conn, tmdb_id, request.state.user_id)
    return templates.TemplateResponse(
        request, "partials/entry_actions.html", {"tmdb_id": tmdb_id, "type": type, "state": state}
    )




@router.get("/pendiente/{tmdb_id}/{type}/confirmar", response_class=HTMLResponse)
def quitar_pendiente_confirmar(request: Request, tmdb_id: int, type: str):
    """Primer toque de 'quitar de pendientes': pasa a modo confirmar en vez de
    quitarlo directo (fácil de tocar sin querer al hacer scroll en /pendientes)."""
    return templates.TemplateResponse(
        request, "partials/entry_actions.html", {"tmdb_id": tmdb_id, "type": type, "state": "pending_confirm"}
    )




def _entry_state(conn, tmdb_id: int, user_id: int) -> str:
    title_row = repo.get_title(conn, tmdb_id)
    existing = (
        conn.execute(
            "SELECT status FROM entries WHERE title_id = ? AND user_id = ?", (title_row["id"], user_id)
        ).fetchone()
        if title_row else None
    )
    return existing["status"] if existing else "new"
