"""app.routers.titulo - ficha técnica y todas sus acciones."""
import os

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import anime, config, repo, tmdb, web
from app.db import get_connection
from app.web import templates

router = APIRouter()




@router.post("/vista/{tmdb_id}/{type}", response_class=HTMLResponse)
def marcar_vista_rapido(request: Request, tmdb_id: int, type: str):
    """Marca como vista al momento, sin pedir nota/comentario - queda en /puntuar para puntuar despues.
    En series marca solo el primer episodio (no la serie entera) - si ya se ha visto
    todo, para eso esta el doble tick de la tarjeta de "Continuar viendo"."""
    with get_connection() as conn:
        entry = repo.ensure_entry(conn, tmdb_id, type, request.state.user_id)
        title_row = repo.get_title(conn, tmdb_id)
        if type == "show" and not repo.list_episodes(conn, title_row["id"]):
            # Un reintento cubre la mayoría de fallos puntuales de TMDB. Si el segundo
            # también falla, la entry se marca vista igual (mejor que un 500).
            for _intento in range(2):
                try:
                    repo.sync_episodes(conn, title_row)
                    break
                except Exception:
                    continue
        repo.mark_watched_quick(conn, entry["id"], title_row)
        # Si la entry ya entra en "Continuar viendo", se manda también la tarjeta por
        # OOB para que aparezca sin recargar; si /pendientes no está abierto, el
        # hx-swap-oob no encuentra "#continuar-carousel" y no hace nada.
        #
        # hx-swap-oob va en un <div> envoltorio desechable: con un estilo distinto de
        # outerHTML (aquí "afterbegin"), htmx descarta el elemento que lleva el
        # atributo e inserta solo sus hijos, y la tarjeta perdería su clase.
        oob = ""
        if type == "show":
            card = repo.get_home_card(conn, entry["id"], request.state.user_id)
            if card:
                card_html = templates.env.get_template("partials/home_card.html").render(
                    {"request": request, "c": card, "confirm_all": False}
                )
                oob = f'<div hx-swap-oob="afterbegin:#continuar-carousel">{card_html}</div>'
    html = templates.env.get_template("partials/entry_actions.html").render(
        {"request": request, "tmdb_id": tmdb_id, "type": type, "state": "watched"}
    )
    return HTMLResponse(html + oob)




@router.get("/marcar-vista/{tmdb_id}/{type}", response_class=HTMLResponse)
def marcar_vista_form(request: Request, tmdb_id: int, type: str, volver: str = ""):
    with get_connection() as conn:
        entry = repo.ensure_entry(conn, tmdb_id, type, request.state.user_id)
        entry = repo.get_entry_with_title(conn, entry["id"])
    categories = {
        "historia": entry["cat_historia"],
        "animacion": entry["cat_animacion"],
        "personajes": entry["cat_personajes"],
        "musica": entry["cat_musica"],
        "disfrute": entry["cat_disfrute"],
    }
    return templates.TemplateResponse(
        request,
        "mark_watched.html",
        {
            "entry": entry,
            "rating": entry["rating"],
            "comment": entry["comment"],
            "volver": volver,
            "categories": categories,
            "weights": repo.CATEGORY_WEIGHTS,
            "is_habit": entry["is_habit"],
        },
    )




CATEGORY_FIELDS = ["historia", "animacion", "personajes", "musica", "disfrute"]




@router.post("/marcar-vista/{entry_id}", response_class=HTMLResponse)
def marcar_vista_submit(
    request: Request,
    entry_id: int,
    rating: str = Form(""),
    comment: str = Form(""),
    volver: str = Form(""),
    watched_date: str = Form(""),
    cat_historia: str = Form(""),
    cat_animacion: str = Form(""),
    cat_personajes: str = Form(""),
    cat_musica: str = Form(""),
    cat_disfrute: str = Form(""),
    is_habit: str = Form(""),
):
    errors = {}
    raw_categories = {
        "historia": cat_historia,
        "animacion": cat_animacion,
        "personajes": cat_personajes,
        "musica": cat_musica,
        "disfrute": cat_disfrute,
    }
    categories = {}
    for cat, raw in raw_categories.items():
        if not raw.strip():
            categories[cat] = None
            continue
        try:
            value = float(raw.replace(",", "."))
            if not (0 <= value <= 10):
                errors[f"cat_{cat}"] = "Entre 0 y 10."
            categories[cat] = value
        except ValueError:
            errors[f"cat_{cat}"] = "Numero entre 0 y 10."
            categories[cat] = None

    usa_categorias = any(v is not None for v in categories.values())

    rating_value = None
    if usa_categorias:
        rating_value = repo.compute_weighted_rating(categories)
    elif rating.strip():
        try:
            rating_value = float(rating.replace(",", "."))
            if not (0 <= rating_value <= 10):
                errors["rating"] = "La nota tiene que estar entre 0 y 10."
        except ValueError:
            errors["rating"] = "Escribe un número entre 0 y 10."
    else:
        errors["rating"] = "Rellena al menos una pregunta del examen, o pon una nota directa."

    if not comment.strip():
        errors["comment"] = "El comentario es obligatorio."

    with get_connection() as conn:
        entry = repo.get_owned_entry_with_title(conn, entry_id, request.state.user_id)
        if not entry:
            raise StarletteHTTPException(status_code=404, detail="No encontrado")
        if errors:
            return templates.TemplateResponse(
                request,
                "mark_watched.html",
                {
                    "entry": entry,
                    "errors": errors,
                    "rating": rating,
                    "comment": comment,
                    "volver": volver,
                    "categories": raw_categories,
                    "weights": repo.CATEGORY_WEIGHTS,
                    "is_habit": bool(is_habit),
                },
                status_code=422,
            )
        repo.mark_watched(
            conn, entry_id, rating_value, comment.strip(), categories,
            watched_at=watched_date.strip() or None, is_habit=bool(is_habit),
        )

    if volver == "puntuar":
        destino = "/puntuar"
    elif volver == "ficha":
        destino = f"/titulo/{entry['tmdb_id']}/{entry['type']}"
    else:
        destino = "/vistas"
    return RedirectResponse(destino, status_code=303)




@router.get("/titulo/{tmdb_id}/{type}", response_class=HTMLResponse)
def titulo_detalle(request: Request, tmdb_id: int, type: str, comentar: int = 0, apunte: int = 0):
    """Unica ruta que llamaba a TMDB sin ningun try/except: abrir por primera vez la
    ficha de algo (ensure_entry pide get_details) daba un 500 en la cara si TMDB iba
    lento o daba un hipo, mismo problema ya resuelto en /buscar y /vista pero que
    aqui seguia sin protegerse. Si el primer ensure_entry falla no hay title_row con
    el que renderizar nada, asi que se convierte en un 404/503 con la pagina de error
    propia (no en un traceback crudo); episodios/personajes son best-effort, un fallo
    ahi no debe tumbar la ficha entera."""
    with get_connection() as conn:
        title_row = repo.get_title(conn, tmdb_id)
        if title_row is None:
            try:
                repo.ensure_entry(conn, tmdb_id, type, request.state.user_id)
            except Exception:
                raise StarletteHTTPException(
                    status_code=503, detail="TMDB no responde ahora mismo, prueba en un rato."
                )
            title_row = repo.get_title(conn, tmdb_id)
        episodes = []
        if type == "show":
            try:
                if not repo.list_episodes(conn, title_row["id"]):
                    repo.sync_episodes(conn, title_row)
                episodes = repo.list_episodes(conn, title_row["id"])
            except Exception:
                episodes = repo.list_episodes(conn, title_row["id"])
        # Filtrado por user_id: si no, los botones de la ficha actuarían sobre la
        # entry de otro usuario que tenga el mismo título.
        entry = conn.execute(
            "SELECT * FROM entries WHERE title_id = ? AND user_id = ?",
            (title_row["id"], request.state.user_id),
        ).fetchone()
        # "Visto ahora" tiene en cuenta el rewatch en curso. Visto, veces, comentario y
        # favorito son de este usuario; se cargan en una sola pasada por los ids de la
        # serie. dict() porque sqlite3.Row no admite claves nuevas.
        rewatch_started_at = entry["rewatch_started_at"] if entry else None
        episodes = [dict(ep) for ep in episodes]
        ep_ids = [ep["id"] for ep in episodes]
        last_watch_by_ep, count_by_ep, state_by_ep = {}, {}, {}
        comments_by_ep: dict[int, list] = {}
        if ep_ids:
            placeholders = ",".join("?" * len(ep_ids))
            for row in conn.execute(
                f"""SELECT episode_id, max(watched_at) AS last_watch, count(*) AS n
                   FROM episode_watches WHERE episode_id IN ({placeholders}) AND user_id = ?
                   GROUP BY episode_id""",
                (*ep_ids, request.state.user_id),
            ):
                last_watch_by_ep[row["episode_id"]] = row["last_watch"]
                count_by_ep[row["episode_id"]] = row["n"]
            for row in conn.execute(
                f"""SELECT * FROM episode_user_state
                   WHERE episode_id IN ({placeholders}) AND user_id = ?""",
                (*ep_ids, request.state.user_id),
            ):
                state_by_ep[row["episode_id"]] = row
            # Debate por episodio: una sola query para toda la ficha.
            for row in conn.execute(
                f"""SELECT episode_comments.id, episode_comments.episode_id, episode_comments.body,
                           episode_comments.created_at, episode_comments.user_id, users.username,
                           users.avatar_path
                    FROM episode_comments
                    JOIN users ON users.id = episode_comments.user_id
                    WHERE episode_comments.episode_id IN ({placeholders})
                    ORDER BY episode_comments.created_at ASC, episode_comments.id ASC""",
                ep_ids,
            ):
                comments_by_ep.setdefault(row["episode_id"], []).append(row)
        for ep in episodes:
            ep["watched_at"] = last_watch_by_ep.get(ep["id"])
            ep["watch_count"] = count_by_ep.get(ep["id"], 0)
            ep["watched_now"] = _episode_watched_now(ep["watched_at"], rewatch_started_at)
            state = state_by_ep.get(ep["id"])
            ep["comment"] = state["comment"] if state else None
            ep["is_favorite"] = state["is_favorite"] if state else 0
            ep["debate"] = comments_by_ep.get(ep["id"], [])
            # Llegada desde el aviso "¿comentas?" (Continuar viendo/Pendientes, ver
            # pendientes.py) - abre solo el hilo de ESE episodio, con el enlace
            # llevando ademas a "#ep-{id}" para que el navegador haga scroll solo.
            ep["open_debate"] = comentar == ep["id"]
            # `apunte` abre el comentario privado del episodio (lo usa el aviso
            # "¿comentas?"); `comentar` abre el debate y lo usa /comentarios/siguiente.
            ep["open_note"] = apunte == ep["id"]
        is_favorite = repo.is_entry_favorite(conn, entry["id"], request.state.user_id) if entry else False

        try:
            repo.sync_characters(conn, title_row)
        except Exception:
            pass
        characters = repo.list_characters(conn, title_row["id"], entry["id"] if entry else None)

        sessions, plays, rating_history = [], 0, []
        if entry and entry["status"] == "watched":
            sessions = repo.list_watch_sessions(conn, entry["id"])
            plays = repo.count_plays(conn, entry["id"])
            rating_history = repo.list_rating_history(conn, entry["id"])
        season_ratings = repo.list_season_ratings(conn, entry["id"]) if entry else {}
        weekday_override = (
            repo.get_weekday_override(conn, title_row["anilist_id"])
            if title_row["anilist_id"] else None
        )

    outcome_label = None
    if entry and entry["predicted_score"] is not None and entry["rating"] is not None:
        outcome_label = repo.predicted_outcome_label(entry["predicted_score"], entry["rating"])

    seasons = {}
    for ep in episodes:
        seasons.setdefault(ep["season_number"], []).append(ep)
    # Mini-diario: todos los comentarios de episodios juntos, en orden de emision.
    ep_comments = [ep for ep in episodes if ep["comment"]]

    return templates.TemplateResponse(
        request,
        "title_detail.html",
        {
            "title_row": title_row,
            "entry": entry,
            "is_favorite": is_favorite,
            "seasons": seasons,
            "characters": characters,
            "sessions": sessions,
            "plays": plays,
            "ep_comments": ep_comments,
            "rating_history": rating_history,
            "season_ratings": season_ratings,
            "outcome_label": outcome_label,
            "weekday_override": weekday_override,
        },
    )




MAX_POSTER_BYTES = config.MAX_POSTER_BYTES


POSTER_CONTENT_TYPES = config.POSTER_CONTENT_TYPES




@router.post("/titulo/{tmdb_id}/{type}/portada", response_class=HTMLResponse)
async def cambiar_portada(request: Request, tmdb_id: int, type: str, imagen: UploadFile = File(...)):
    """Portada propia subida a mano. Sobrescribe el fichero con el mismo convenio de
    nombre que ensure_title/create_manual_entry, así el resto del código no distingue."""
    if imagen.content_type not in POSTER_CONTENT_TYPES:
        raise StarletteHTTPException(400, "Ese archivo no es una imagen JPEG/PNG/WebP.")
    # Leido en trozos con tope en vez de un unico .read() sin limite - un archivo
    # enorme (por error o a proposito) no debe poder llenar la memoria/el disco.
    data = bytearray()
    while chunk := await imagen.read(1024 * 1024):
        data.extend(chunk)
        if len(data) > MAX_POSTER_BYTES:
            raise StarletteHTTPException(400, "Esa imagen pesa demasiado (máximo 8 MB).")
    filename = f"{tmdb_id}.jpg" if tmdb_id > 0 else f"manual_{-tmdb_id}.jpg"
    os.makedirs(repo.POSTERS_DIR, exist_ok=True)
    with open(os.path.join(repo.POSTERS_DIR, filename), "wb") as f:
        f.write(data)
    with get_connection() as conn:
        repo.set_poster(conn, tmdb_id, f"/static/posters/{filename}")
    return RedirectResponse(f"/titulo/{tmdb_id}/{type}", status_code=303)


@router.post("/titulo/{tmdb_id}/{type}/dia-emision", response_class=HTMLResponse)
def corregir_dia_emision(request: Request, tmdb_id: int, type: str, weekday: str = Form("")):
    """Corrige a mano el día de la semana de este título en /calendario/anual: el
    cálculo automático usa UTC y puede desplazar un día los animes de madrugada.
    weekday vacío = volver al cálculo automático."""
    with get_connection() as conn:
        title_row = repo.get_title(conn, tmdb_id)
        if title_row and title_row["anilist_id"]:
            if weekday.strip() == "":
                repo.clear_weekday_override(conn, title_row["anilist_id"])
            # Un valor no numérico (POST manipulado) se ignora en vez de dar un 500.
            elif weekday.strip().lstrip("-").isdigit():
                repo.set_weekday_override(conn, title_row["anilist_id"], int(weekday))
    return RedirectResponse(f"/titulo/{tmdb_id}/{type}", status_code=303)


@router.post("/titulo/{tmdb_id}/{type}/desfase-emision", response_class=HTMLResponse)
def corregir_desfase_emision(request: Request, tmdb_id: int, type: str, dias: str = Form("0")):
    """Corrige el desfase entre la fecha de emisión de TMDB y la real. A diferencia de
    la corrección de día de arriba (solo el calendario de temporada), esto cambia la
    fecha que usa toda la app para decidir si algo ya ha emitido. Resincroniza al
    guardar para no esperar a la sync de fondo."""
    dias_str = dias.strip()
    if not dias_str.lstrip("-").isdigit():
        return RedirectResponse(f"/titulo/{tmdb_id}/{type}", status_code=303)
    with get_connection() as conn:
        title_row = repo.get_title(conn, tmdb_id)
        if title_row:
            repo.set_air_date_offset(conn, title_row["id"], int(dias_str))
            fresh = repo.get_title(conn, tmdb_id)
            try:
                repo.refresh_metadata(conn, fresh)
                repo.sync_episodes(conn, repo.get_title(conn, tmdb_id))
            except Exception:
                pass
    return RedirectResponse(f"/titulo/{tmdb_id}/{type}", status_code=303)




def _owned_entry_or_404(conn, entry_id: int, user_id: int):
    entry = repo.get_owned_entry_with_title(conn, entry_id, user_id)
    if not entry:
        raise StarletteHTTPException(status_code=404, detail="No encontrado")
    return entry


@router.post("/entrada/{entry_id}/volver-a-ver", response_class=HTMLResponse)
def volver_a_ver(request: Request, entry_id: int):
    """Empieza una ronda de rewatch: en series vuelve a "Continuar viendo" sin borrar el
    historial; en pelis solo suma un visionado. La nota original no se toca."""
    with get_connection() as conn:
        entry = _owned_entry_or_404(conn, entry_id, request.state.user_id)
        repo.start_rewatch(conn, entry_id)
    return RedirectResponse(f"/titulo/{entry['tmdb_id']}/{entry['type']}", status_code=303)




@router.post("/entrada/{entry_id}/deshacer-vista", response_class=HTMLResponse)
def deshacer_vista(request: Request, entry_id: int):
    """Deshace un "Marcar vista" por error, antes de puntuar: vuelve a pending y
    desmarca todos los episodios (ver repo.undo_mark_watched)."""
    with get_connection() as conn:
        entry = _owned_entry_or_404(conn, entry_id, request.state.user_id)
        repo.undo_mark_watched(conn, entry_id)
    return RedirectResponse(f"/titulo/{entry['tmdb_id']}/{entry['type']}", status_code=303)




@router.post("/entrada/{entry_id}/quitar-nota", response_class=HTMLResponse)
def quitar_nota(request: Request, entry_id: int):
    """Quita la nota puesta antes de tiempo (p.ej. un 10 a mitad de una serie que
    aun no ha acabado) - sigue marcada como vista, vuelve a la cola de /puntuar."""
    with get_connection() as conn:
        entry = _owned_entry_or_404(conn, entry_id, request.state.user_id)
        repo.clear_rating(conn, entry_id)
    return RedirectResponse(f"/titulo/{entry['tmdb_id']}/{entry['type']}", status_code=303)




@router.post("/entrada/{entry_id}/fecha-visionado", response_class=HTMLResponse)
def corregir_fecha_visionado(request: Request, entry_id: int, fecha: str = Form(...)):
    """Corrige la fecha de visionado de algo ya visto, sin tocar nota ni comentario."""
    with get_connection() as conn:
        entry = _owned_entry_or_404(conn, entry_id, request.state.user_id)
        repo.set_watched_at(conn, entry_id, fecha)
    return RedirectResponse(f"/titulo/{entry['tmdb_id']}/{entry['type']}", status_code=303)




@router.post("/entrada/{entry_id}/habito", response_class=HTMLResponse)
def entrada_habito_toggle(request: Request, entry_id: int):
    """Toggle de habito en la ficha (Bloque 3a) - sitio canonico, siempre disponible."""
    with get_connection() as conn:
        _owned_entry_or_404(conn, entry_id, request.state.user_id)
        is_habit = repo.toggle_habit(conn, entry_id)
    return templates.TemplateResponse(
        request, "partials/habit_button.html", {"entry_id": entry_id, "is_habit": is_habit}
    )




@router.post("/entrada/{entry_id}/autover", response_class=HTMLResponse)
def entrada_autover_toggle(request: Request, entry_id: int):
    """Toggle de autover en la ficha - series como One Piece, que se autoveen solas
    (ver repo.sync_library) sin pasar por Continuar viendo."""
    with get_connection() as conn:
        _owned_entry_or_404(conn, entry_id, request.state.user_id)
        auto_watch = repo.toggle_auto_watch(conn, entry_id)
    return templates.TemplateResponse(
        request, "partials/autowatch_button.html", {"entry_id": entry_id, "auto_watch": auto_watch}
    )




@router.post("/personaje/{character_id}/favorito/{entry_id}", response_class=HTMLResponse)
def personaje_favorito_toggle(request: Request, character_id: int, entry_id: int):
    with get_connection() as conn:
        _owned_entry_or_404(conn, entry_id, request.state.user_id)
        is_fav = repo.toggle_favorite_character(conn, entry_id, character_id)
        character = conn.execute("SELECT * FROM characters WHERE id = ?", (character_id,)).fetchone()
    return templates.TemplateResponse(
        request,
        "partials/character_card.html",
        {"c": character, "entry_id": entry_id, "is_favorite": is_fav},
    )




@router.post("/entrada/{entry_id}/favorito", response_class=HTMLResponse)
def favorito_toggle(request: Request, entry_id: int):
    with get_connection() as conn:
        _owned_entry_or_404(conn, entry_id, request.state.user_id)
        default_list = repo.get_or_create_default_list(conn, request.state.user_id)
        is_fav = repo.toggle_list_item(conn, default_list["id"], entry_id)
    return templates.TemplateResponse(
        request, "partials/favorite_button.html", {"entry_id": entry_id, "is_favorite": is_fav}
    )




def _episode_watched_now(watched_at, rewatch_started_at) -> bool:
    """"Visto ahora": un visionado anterior al rewatch en curso cuenta como pendiente.
    Mismo criterio que repo._pending_clause, en Python sobre filas ya cargadas."""
    return bool(watched_at) and (not rewatch_started_at or watched_at >= rewatch_started_at)




def _episode_row_response(request: Request, conn, episode_id: int, oob: str = "", open_debate: bool = False):
    """`oob` (indicador de Puntuar, ver web.render_nav_cola_oob) solo lo pasa
    episodio_toggle; rewatch y favorito no cambian entries.status. Visto, veces,
    comentario y favorito son de este usuario."""
    user_id = request.state.user_id
    episode = dict(conn.execute("SELECT * FROM episodes WHERE id = ?", (episode_id,)).fetchone())
    rewatch_started_at = conn.execute(
        "SELECT rewatch_started_at FROM entries WHERE title_id = ? AND user_id = ?",
        (episode["title_id"], user_id),
    ).fetchone()
    rewatch_started_at = rewatch_started_at["rewatch_started_at"] if rewatch_started_at else None
    last_watch = conn.execute(
        "SELECT watched_at FROM episode_watches WHERE episode_id = ? AND user_id = ? ORDER BY watched_at DESC LIMIT 1",
        (episode_id, user_id),
    ).fetchone()
    episode["watched_at"] = last_watch["watched_at"] if last_watch else None
    episode["watched_now"] = _episode_watched_now(episode["watched_at"], rewatch_started_at)
    episode["watch_count"] = conn.execute(
        "SELECT count(*) FROM episode_watches WHERE episode_id = ? AND user_id = ?", (episode_id, user_id)
    ).fetchone()[0]
    state = repo.get_episode_user_state(conn, episode_id, user_id)
    episode["comment"] = state["comment"] if state else None
    episode["is_favorite"] = state["is_favorite"] if state else 0
    episode["debate"] = repo.list_episode_comments(conn, episode_id)
    episode["open_debate"] = open_debate
    html = templates.env.get_template("partials/episode_row.html").render(
        {"request": request, "ep": episode}
    )
    return HTMLResponse(html + oob)




@router.post("/episodio/{episode_id}/toggle", response_class=HTMLResponse)
def episodio_toggle(request: Request, episode_id: int):
    with get_connection() as conn:
        # Al marcar visto (no al desmarcar) se abre el debate de ese episodio.
        just_watched = repo.toggle_episode(conn, episode_id, request.state.user_id)
        oob = web.render_nav_cola_oob(conn, request.state.user_id)
        return _episode_row_response(request, conn, episode_id, oob=oob, open_debate=just_watched)




@router.post("/episodio/{episode_id}/volver-a-ver", response_class=HTMLResponse)
def episodio_volver_a_ver(request: Request, episode_id: int):
    with get_connection() as conn:
        repo.rewatch_episode(conn, episode_id, request.state.user_id)
        return _episode_row_response(request, conn, episode_id)


@router.get("/episodio/{episode_id}/historial", response_class=HTMLResponse)
def episodio_historial(request: Request, episode_id: int):
    with get_connection() as conn:
        fechas = repo.list_episode_watch_dates(conn, episode_id, request.state.user_id)
        return templates.TemplateResponse(
            request, "partials/episode_watch_history.html", {"fechas": fechas}
        )


@router.post("/episodio/{episode_id}/favorito", response_class=HTMLResponse)
def episodio_favorito(request: Request, episode_id: int):
    with get_connection() as conn:
        repo.toggle_episode_favorite(conn, episode_id, request.state.user_id)
        return _episode_row_response(request, conn, episode_id)




@router.post("/episodio/{episode_id}/comentario", response_class=HTMLResponse)
def episodio_comentario(request: Request, episode_id: int, comment: str = Form("")):
    with get_connection() as conn:
        repo.set_episode_comment(conn, episode_id, request.state.user_id, comment)
        return _episode_row_response(request, conn, episode_id)


@router.post("/episodio/{episode_id}/debate", response_class=HTMLResponse)
def episodio_debate(request: Request, episode_id: int, body: str = Form("")):
    """Debate por episodio, visible para todos los usuarios (a diferencia de
    /comentario, que es privado). La ficha lo difumina hasta que cada usuario ha visto
    el episodio, así que aquí no hace falta control de spoilers."""
    with get_connection() as conn:
        repo.add_episode_comment(conn, episode_id, request.state.user_id, body)
        return _episode_row_response(request, conn, episode_id, open_debate=True)


@router.get("/comentarios/siguiente")
def comentarios_siguiente(request: Request):
    """Destino del aviso del nav: salta al comentario ajeno más antiguo sin ver y marca
    todos como vistos."""
    with get_connection() as conn:
        target = repo.get_oldest_unseen_comment(conn, request.state.user_id)
        repo.mark_comments_seen(conn, request.state.user_id)
    if not target:
        return RedirectResponse("/pendientes", status_code=303)
    return RedirectResponse(
        f"/titulo/{target['tmdb_id']}/{target['type']}?comentar={target['episode_id']}#ep-{target['episode_id']}",
        status_code=303,
    )


@router.post("/episodio/{episode_id}/debate/{comment_id}/borrar", response_class=HTMLResponse)
def episodio_debate_borrar(request: Request, episode_id: int, comment_id: int):
    """Cada uno borra su propio comentario; un admin, cualquiera."""
    with get_connection() as conn:
        repo.delete_episode_comment(conn, comment_id, request.state.user_id, request.state.is_admin)
        return _episode_row_response(request, conn, episode_id, open_debate=True)




@router.get("/titulo/{tmdb_id}/{type}/similares", response_class=HTMLResponse)
def titulo_similares(request: Request, tmdb_id: int, type: str):
    """Se carga lazy via htmx para no frenar el render del detalle."""
    with get_connection() as conn:
        title_row = repo.get_title(conn, tmdb_id)
        try:
            similares = repo.get_similar(conn, title_row) if title_row else []
        except Exception:
            similares = []
    return templates.TemplateResponse(request, "partials/similar_titles.html", {"similares": similares})




@router.get("/titulo/{tmdb_id}/{type}/trailer", response_class=HTMLResponse)
def titulo_trailer(request: Request, tmdb_id: int, type: str):
    """Se carga lazy via htmx, igual que Similares - no frenar el render del detalle
    con la llamada a TMDB."""
    try:
        trailer_url = tmdb.get_trailer(tmdb_id, type) if tmdb_id > 0 else None
    except Exception:
        trailer_url = None
    return templates.TemplateResponse(request, "partials/trailer.html", {"trailer_url": trailer_url})




@router.post("/titulo/{tmdb_id}/{type}/temporada/{season_number}/marcar", response_class=HTMLResponse)
def marcar_temporada(request: Request, tmdb_id: int, type: str, season_number: int):
    """Marca vista una temporada entera de golpe (los episodios ya emitidos) - para
    tragarse una temporada de una sentada sin ir episodio a episodio."""
    with get_connection() as conn:
        # Si aún no hay entry (ficha abierta sin "+ Pendientes"), ensure_entry la crea,
        # igual que marcar_vista_rapido.
        entry = repo.ensure_entry(conn, tmdb_id, type, request.state.user_id)
        repo.mark_season_watched(conn, entry["id"], season_number)
    return RedirectResponse(f"/titulo/{tmdb_id}/{type}", status_code=303)




@router.get("/titulo/{tmdb_id}/{type}/temporada/{season_number}/puntuar", response_class=HTMLResponse)
def puntuar_temporada_form(request: Request, tmdb_id: int, type: str, season_number: int):
    """Nota por temporada, aparte de la nota general de la serie - para series donde
    unas temporadas valen mas que otras (una sola nota por serie completa las mezclaba)."""
    with get_connection() as conn:
        title_row = repo.get_title(conn, tmdb_id)
        entry = conn.execute(
            "SELECT * FROM entries WHERE title_id = ? AND user_id = ?",
            (title_row["id"], request.state.user_id),
        ).fetchone()
        existing = repo.get_season_rating(conn, entry["id"], season_number) if entry else None
    categories = {
        "historia": existing["cat_historia"] if existing else None,
        "animacion": existing["cat_animacion"] if existing else None,
        "personajes": existing["cat_personajes"] if existing else None,
        "musica": existing["cat_musica"] if existing else None,
        "disfrute": existing["cat_disfrute"] if existing else None,
    }
    return templates.TemplateResponse(
        request,
        "season_rate.html",
        {
            "title_row": title_row,
            "season_number": season_number,
            "rating": existing["rating"] if existing else None,
            "comment": existing["comment"] if existing else "",
            "categories": categories,
            "weights": repo.CATEGORY_WEIGHTS,
        },
    )




@router.post("/titulo/{tmdb_id}/{type}/temporada/{season_number}/puntuar", response_class=HTMLResponse)
def puntuar_temporada_submit(
    request: Request,
    tmdb_id: int,
    type: str,
    season_number: int,
    rating: str = Form(""),
    comment: str = Form(""),
    cat_historia: str = Form(""),
    cat_animacion: str = Form(""),
    cat_personajes: str = Form(""),
    cat_musica: str = Form(""),
    cat_disfrute: str = Form(""),
):
    raw_categories = {
        "historia": cat_historia, "animacion": cat_animacion, "personajes": cat_personajes,
        "musica": cat_musica, "disfrute": cat_disfrute,
    }
    # Un valor no numérico se ignora en vez de dar un 500.
    def _parse_float(raw: str) -> float | None:
        if not raw.strip():
            return None
        try:
            return float(raw.replace(",", "."))
        except ValueError:
            return None

    categories = {cat: _parse_float(raw) for cat, raw in raw_categories.items()}
    usa_categorias = any(v is not None for v in categories.values())
    rating_value = repo.compute_weighted_rating(categories) if usa_categorias else _parse_float(rating)
    with get_connection() as conn:
        title_row = repo.get_title(conn, tmdb_id)
        entry = conn.execute(
            "SELECT * FROM entries WHERE title_id = ? AND user_id = ?",
            (title_row["id"], request.state.user_id),
        ).fetchone()
        if entry and rating_value is not None:
            repo.set_season_rating(conn, entry["id"], season_number, rating_value, comment.strip(), categories)
    return RedirectResponse(f"/titulo/{tmdb_id}/{type}", status_code=303)




@router.post("/titulo/{tmdb_id}/{type}/personajes", response_class=HTMLResponse)
def titulo_recargar_personajes(request: Request, tmdb_id: int, type: str):
    with get_connection() as conn:
        title_row = repo.get_title(conn, tmdb_id)
        if title_row:
            repo.resync_characters(conn, title_row)
    return RedirectResponse(f"/titulo/{tmdb_id}/{type}", status_code=303)




@router.get("/titulo/{tmdb_id}/{type}/personajes/buscar", response_class=HTMLResponse)
def titulo_buscar_personaje(request: Request, tmdb_id: int, type: str, q: str = ""):
    """Busqueda por nombre en AniList, para añadir personajes que el sync automatico
    no trae (solo cachea el top 25 del titulo)."""
    results, api_down = [], False
    if q.strip():
        try:
            results, api_down = anime.search_characters(q)
        except Exception:
            results, api_down = [], False
    return templates.TemplateResponse(
        request,
        "partials/character_search_results.html",
        {"results": results, "query": q, "tmdb_id": tmdb_id, "type": type, "api_down": api_down},
    )




@router.post("/titulo/{tmdb_id}/{type}/personajes/anadir", response_class=HTMLResponse)
def titulo_anadir_personaje(
    request: Request,
    tmdb_id: int,
    type: str,
    char_id: int = Form(...),
    name: str = Form(...),
    image_url: str = Form(""),
):
    with get_connection() as conn:
        title_row = repo.get_title(conn, tmdb_id)
        if title_row:
            repo.add_character_manual(conn, title_row, char_id, name, image_url.strip() or None)
    return RedirectResponse(f"/titulo/{tmdb_id}/{type}", status_code=303)
