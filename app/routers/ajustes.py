"""app.routers.ajustes - ajustes, cuentas, pesos de afinidad y exportación."""
import asyncio
import json
import os
from datetime import date
from urllib.parse import quote_plus

from fastapi import APIRouter, File, Form, Request, Response, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import config, repo
from app.db import get_connection
from app.web import _recompute_with_status, templates

router = APIRouter()

AVATARS_DIR = "app/static/avatars"




@router.get("/ajustes", response_class=HTMLResponse)
def ajustes(
    request: Request, usuario_creado: str = "", usuario_error: str = "", usuario_ok: str = "",
    perfil_error: str = "", perfil_ok: str = "",
):
    with get_connection() as conn:
        rechazados = repo.list_rejected_recommendations(conn, request.state.user_id)
        profile_history = repo.get_profile_history(conn, request.state.user_id)
        affinity_config = repo.get_affinity_config(conn, request.state.user_id)
        # Solo un admin ve y gestiona las cuentas.
        usuarios = repo.list_users(conn) if request.state.is_admin else []
        show_anime_calendar = repo.get_show_anime_calendar(
            conn, request.state.user_id, default=repo.user_has_anime(conn, request.state.user_id)
        )
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "rechazados": rechazados,
            "profile_history": profile_history,
            "affinity_config": affinity_config,
            "usuarios": usuarios,
            "usuario_creado": usuario_creado,
            "usuario_error": usuario_error,
            "usuario_ok": usuario_ok,
            "perfil_error": perfil_error,
            "perfil_ok": perfil_ok,
            "show_anime_calendar": show_anime_calendar,
            # Valor por defecto junto a cada peso, sacado de las constantes de repo.
            "affinity_defaults": {
                "appetite_weights": repo.APPETITE_WEIGHTS,
                "quality_weights": repo.QUALITY_WEIGHTS,
                "appetite_exp": repo.AFFINITY_APPETITE_EXP,
                "quality_exp": repo.AFFINITY_QUALITY_EXP,
                "gap_threshold": repo.AFFINITY_GAP_THRESHOLD,
                "gap_factor": repo.AFFINITY_GAP_FACTOR,
                "predict_weights": repo.PREDICT_SIGNAL_WEIGHTS,
            },
        },
    )





@router.post("/ajustes/perfil/calendario-anime", response_class=HTMLResponse)
def ajustes_calendario_anime(request: Request, mostrar: str = Form("0")):
    """Muestra u oculta el enlace al calendario de temporada, por encima de la
    detección automática de repo.user_has_anime."""
    with get_connection() as conn:
        repo.set_show_anime_calendar(conn, request.state.user_id, mostrar == "1")
    return RedirectResponse("/ajustes", status_code=303)




@router.post("/ajustes/perfil/nombre", response_class=HTMLResponse)
def ajustes_cambiar_nombre(request: Request, username: str = Form(...)):
    """Cambia tu propio nombre de usuario."""
    with get_connection() as conn:
        try:
            repo.set_username(conn, request.state.user_id, username)
        except ValueError as e:
            return RedirectResponse(f"/ajustes?perfil_error={quote_plus(str(e))}", status_code=303)
    return RedirectResponse("/ajustes", status_code=303)




@router.post("/ajustes/perfil/password", response_class=HTMLResponse)
def ajustes_cambiar_password(
    request: Request,
    password_actual: str = Form(...),
    password_nueva: str = Form(...),
    password_nueva2: str = Form(...),
):
    """Cambia tu propia contraseña."""
    if password_nueva != password_nueva2:
        return RedirectResponse(
            f"/ajustes?perfil_error={quote_plus('Las contraseñas nuevas no coinciden')}", status_code=303
        )
    with get_connection() as conn:
        try:
            repo.change_password(conn, request.state.user_id, password_actual, password_nueva)
        except ValueError as e:
            return RedirectResponse(f"/ajustes?perfil_error={quote_plus(str(e))}", status_code=303)
    return RedirectResponse("/ajustes?perfil_ok=Contraseña+actualizada", status_code=303)




@router.post("/ajustes/perfil/foto", response_class=HTMLResponse)
async def ajustes_cambiar_foto(request: Request, imagen: UploadFile = File(...)):
    """Foto de perfil propia. Valida tipo y tamaño leyendo por trozos, igual que
    cambiar_portada."""
    if imagen.content_type not in config.POSTER_CONTENT_TYPES:
        raise StarletteHTTPException(400, "Ese archivo no es una imagen JPEG/PNG/WebP.")
    data = bytearray()
    while chunk := await imagen.read(1024 * 1024):
        data.extend(chunk)
        if len(data) > config.MAX_POSTER_BYTES:
            raise StarletteHTTPException(400, "Esa imagen pesa demasiado (máximo 8 MB).")
    os.makedirs(AVATARS_DIR, exist_ok=True)
    filename = f"{request.state.user_id}.jpg"
    with open(os.path.join(AVATARS_DIR, filename), "wb") as f:
        f.write(data)
    with get_connection() as conn:
        repo.set_avatar_path(
            conn, request.state.user_id, f"/static/avatars/{filename}?v={int(date.today().strftime('%Y%m%d'))}"
        )
    return RedirectResponse("/ajustes", status_code=303)




@router.post("/ajustes/usuarios", response_class=HTMLResponse)
def ajustes_crear_usuario(request: Request, username: str = Form(...), password: str = Form(...)):
    """Alta de cuenta nueva (solo admin). POST normal + redirect, como crear_lista."""
    if not request.state.is_admin:
        raise StarletteHTTPException(status_code=403, detail="Solo un administrador puede crear cuentas.")
    username = username.strip()
    if not username or not password:
        return RedirectResponse("/ajustes?usuario_error=Usuario+y+contraseña+obligatorios", status_code=303)
    with get_connection() as conn:
        if repo.get_user_by_username(conn, username):
            return RedirectResponse("/ajustes?usuario_error=Ese+usuario+ya+existe", status_code=303)
        # create_user valida usuario y contraseña: un ValueError se enseña como aviso,
        # no como 500.
        try:
            repo.create_user(conn, username, password)
        except ValueError as e:
            return RedirectResponse(f"/ajustes?usuario_error={quote_plus(str(e))}", status_code=303)
    return RedirectResponse(f"/ajustes?usuario_creado={quote_plus(username)}", status_code=303)




@router.post("/ajustes/usuarios/{user_id}/admin", response_class=HTMLResponse)
def ajustes_toggle_admin(request: Request, user_id: int, valor: str = Form(...)):
    """Da o quita admin (solo admin; nunca deja la instancia sin ninguno, ver
    repo.set_admin)."""
    if not request.state.is_admin:
        raise StarletteHTTPException(status_code=403, detail="Solo un administrador puede gestionar roles.")
    with get_connection() as conn:
        try:
            repo.set_admin(conn, user_id, valor == "1")
        except ValueError as e:
            return RedirectResponse(f"/ajustes?usuario_error={quote_plus(str(e))}", status_code=303)
    return RedirectResponse("/ajustes", status_code=303)




@router.post("/ajustes/usuarios/{user_id}/password", response_class=HTMLResponse)
def ajustes_resetear_password(request: Request, user_id: int, password: str = Form(...)):
    """Un admin pone una contraseña nueva a quien se haya quedado fuera."""
    if not request.state.is_admin:
        raise StarletteHTTPException(status_code=403, detail="Solo un administrador puede resetear contraseñas.")
    with get_connection() as conn:
        try:
            repo.admin_reset_password(conn, user_id, password)
        except ValueError as e:
            return RedirectResponse(f"/ajustes?usuario_error={quote_plus(str(e))}", status_code=303)
    return RedirectResponse(f"/ajustes?usuario_ok={quote_plus('Contraseña reseteada correctamente')}", status_code=303)




@router.get("/afinidad/estado", response_class=HTMLResponse)
def afinidad_estado(request: Request):
    """Estado del recalculo para el indicador que se autorrefresca (htmx polling) en
    /ajustes y /calendario/anual mientras `_recompute_with_status` esta corriendo."""
    with get_connection() as conn:
        status = repo.get_recompute_status(conn, request.state.user_id)
    return templates.TemplateResponse(request, "partials/recompute_status.html", {"status": status})




@router.post("/ajustes/afinidad", response_class=HTMLResponse)
async def ajustes_afinidad(request: Request):
    """Guarda los pesos del índice de afinidad y relanza el recálculo en segundo plano
    (tarda del orden de un minuto)."""
    form = await request.form()
    with get_connection() as conn:
        cfg = repo.get_affinity_config(conn, request.state.user_id)
        for group in ("appetite_weights", "quality_weights"):
            for key in cfg[group]:
                field = f"{group}__{key}"
                if field in form:
                    try:
                        cfg[group][key] = float(form[field])
                    except ValueError:
                        pass
        for key in ("appetite_exp", "quality_exp", "gap_threshold", "gap_factor"):
            if key in form:
                try:
                    cfg[key] = float(form[key])
                except ValueError:
                    pass
        for key in cfg["predict_weights"]:
            field = f"predict_weights__{key}"
            if field in form:
                try:
                    cfg["predict_weights"][key] = float(form[field])
                except ValueError:
                    pass
        repo.set_affinity_config(conn, request.state.user_id, cfg)

    asyncio.create_task(asyncio.to_thread(_recompute_with_status, request.state.user_id))
    return RedirectResponse("/ajustes", status_code=303)




@router.post("/ajustes/afinidad/restablecer", response_class=HTMLResponse)
async def ajustes_afinidad_restablecer(request: Request):
    """`async def` a propósito: FastAPI corre las rutas `def` en un hilo sin event
    loop y ahí asyncio.create_task falla con "no running event loop"."""
    with get_connection() as conn:
        repo.reset_affinity_config(conn, request.state.user_id)

    asyncio.create_task(asyncio.to_thread(_recompute_with_status, request.state.user_id))
    return RedirectResponse("/ajustes", status_code=303)




@router.get("/afinidad/calibracion", response_class=HTMLResponse)
def afinidad_calibracion(request: Request):
    with get_connection() as conn:
        cal = repo.get_prediction_calibration(conn, request.state.user_id)
        accuracy = repo.get_accuracy_history(conn, request.state.user_id)
    return templates.TemplateResponse(
        request, "calibration.html",
        {"cal": cal, "acc": accuracy[-1] if accuracy else None, "acc_first": accuracy[0] if accuracy else None,
         "acc_history": accuracy[-10:][::-1]},
    )




@router.get("/ajustes/exportar")
def exportar_datos(request: Request):
    """Volcado JSON de lo curado a mano por ESTE usuario (notas, comentarios, listas,
    waifus) - copia propia independiente del backup de la BBDD del servidor."""
    with get_connection() as conn:
        data = repo.export_data(conn, request.state.user_id)
    body = json.dumps(data, ensure_ascii=False, indent=2)
    return Response(
        content=body,
        media_type="application/json",
        headers={"Content-Disposition": f"attachment; filename=taratrack-{date.today().isoformat()}.json"},
    )




@router.post("/ajustes/rechazados/{rejected_id}/quitar", response_class=HTMLResponse)
def ajustes_quitar_rechazo(request: Request, rejected_id: int):
    with get_connection() as conn:
        repo.unreject_recommendation(conn, rejected_id, request.state.user_id)
    return RedirectResponse("/ajustes", status_code=303)
