"""app.routers.ajustes - ajustes, cuentas, pesos de afinidad y exportación."""
import asyncio
import json
import math
import os
import time
from datetime import date, datetime, timezone
from urllib.parse import quote_plus

from fastapi import APIRouter, File, Form, Request, Response, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import config, repo
from app.db import get_connection
from app.sesion import set_session_cookie
from app.web import _recompute_with_status, templates

router = APIRouter()

AVATARS_DIR = "app/static/avatars"


@router.get("/ajustes", response_class=HTMLResponse)
def ajustes(
    request: Request, usuario_creado: str = "", usuario_error: str = "", usuario_ok: str = "",
    perfil_error: str = "", perfil_ok: str = "",
):
    return _ajustes_page(request, usuario_error=usuario_error, usuario_ok=usuario_ok,
                         perfil_error=perfil_error, perfil_ok=perfil_ok)


def _ajustes_page(
    request: Request, usuario_error: str = "", usuario_ok: str = "",
    perfil_error: str = "", perfil_ok: str = "", invitacion: dict | None = None,
):
    """`invitacion` ({"username", "code", "nueva"}) solo llega en la respuesta directa al
    crear una cuenta o pedir un codigo nuevo: el codigo se enseña una vez y nunca va en
    la URL (acabaria en historiales y logs del proxy)."""
    with get_connection() as conn:
        rechazados = repo.list_rejected_recommendations(conn, request.state.user_id)
        affinity_config = repo.get_affinity_config(conn, request.state.user_id)
        usuarios = repo.list_users(conn) if request.state.is_admin else []
        show_anime_calendar = repo.get_show_anime_calendar(
            conn, request.state.user_id, default=repo.user_has_anime(conn, request.state.user_id)
        )
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "rechazados": rechazados,
            "affinity_config": affinity_config,
            "usuarios": usuarios,
            "invitacion": invitacion,
            "now_iso": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "invite_days": repo.INVITE_DAYS,
            "usuario_error": usuario_error,
            "usuario_ok": usuario_ok,
            "perfil_error": perfil_error,
            "perfil_ok": perfil_ok,
            "show_anime_calendar": show_anime_calendar,
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
        user = repo.get_user(conn, request.state.user_id)
    return set_session_cookie(RedirectResponse("/ajustes?perfil_ok=Contraseña+actualizada", status_code=303), user)


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
            conn, request.state.user_id, f"/static/avatars/{filename}?v={int(time.time())}"
        )
    return RedirectResponse("/ajustes", status_code=303)


@router.post("/ajustes/usuarios", response_class=HTMLResponse)
def ajustes_crear_usuario(request: Request, username: str = Form(...)):
    if not request.state.is_admin:
        raise StarletteHTTPException(status_code=403, detail="Solo un administrador puede crear cuentas.")
    with get_connection() as conn:
        try:
            user, code = repo.create_invited_user(conn, username)
        except ValueError as e:
            return RedirectResponse(f"/ajustes?usuario_error={quote_plus(str(e))}#s-users", status_code=303)
    return _ajustes_page(request, invitacion={"username": user["username"], "code": code, "nueva": True})


@router.post("/ajustes/usuarios/{user_id}/bloquear", response_class=HTMLResponse)
def ajustes_bloquear(request: Request, user_id: int, valor: str = Form(...)):
    if not request.state.is_admin:
        raise StarletteHTTPException(status_code=403, detail="Solo un administrador puede bloquear cuentas.")
    if user_id == request.state.user_id:
        return RedirectResponse(f"/ajustes?usuario_error={quote_plus('No puedes bloquear tu propia cuenta')}#s-users", status_code=303)
    with get_connection() as conn:
        try:
            repo.set_blocked(conn, user_id, valor == "1")
            user = repo.get_user(conn, user_id)
        except ValueError as e:
            return RedirectResponse(f"/ajustes?usuario_error={quote_plus(str(e))}#s-users", status_code=303)
    msg = f"«{user['username']}» " + ("bloqueada: ya no puede entrar" if valor == "1" else "desbloqueada")
    return RedirectResponse(f"/ajustes?usuario_ok={quote_plus(msg)}#s-users", status_code=303)


@router.post("/ajustes/usuarios/{user_id}/borrar", response_class=HTMLResponse)
def ajustes_borrar(request: Request, user_id: int, confirmar: str = Form("")):
    """Borra una cuenta y todo lo suyo. Hay que escribir el nombre de la cuenta para
    confirmar: no se puede deshacer."""
    if not request.state.is_admin:
        raise StarletteHTTPException(status_code=403, detail="Solo un administrador puede borrar cuentas.")
    if user_id == request.state.user_id:
        return RedirectResponse(f"/ajustes?usuario_error={quote_plus('No puedes borrar tu propia cuenta')}#s-users", status_code=303)
    with get_connection() as conn:
        user = repo.get_user(conn, user_id)
        if not user:
            return RedirectResponse("/ajustes#s-users", status_code=303)
        if confirmar.strip().lower() != user["username"].lower():
            return RedirectResponse(f"/ajustes?usuario_error={quote_plus('Para borrar, escribe el nombre exacto de la cuenta')}#s-users", status_code=303)
        try:
            repo.delete_user(conn, user_id)
        except ValueError as e:
            return RedirectResponse(f"/ajustes?usuario_error={quote_plus(str(e))}#s-users", status_code=303)
    if user["avatar_path"]:
        try:
            os.remove(os.path.join(AVATARS_DIR, f"{user_id}.jpg"))
        except OSError:
            pass
    return RedirectResponse(f"/ajustes?usuario_ok={quote_plus('Cuenta «' + user['username'] + '» borrada')}#s-users", status_code=303)


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


@router.post("/ajustes/usuarios/{user_id}/codigo", response_class=HTMLResponse)
def ajustes_nuevo_codigo(request: Request, user_id: int):
    if not request.state.is_admin:
        raise StarletteHTTPException(status_code=403, detail="Solo un administrador puede generar códigos.")
    if user_id == request.state.user_id:
        return RedirectResponse(f"/ajustes?usuario_error={quote_plus('Para tu cuenta usa «Cambiar contraseña»')}#s-users", status_code=303)
    with get_connection() as conn:
        try:
            code = repo.regenerate_invite(conn, user_id)
        except ValueError as e:
            return RedirectResponse(f"/ajustes?usuario_error={quote_plus(str(e))}#s-users", status_code=303)
        user = repo.get_user(conn, user_id)
    return _ajustes_page(request, invitacion={"username": user["username"], "code": code, "nueva": False})


@router.get("/afinidad/estado", response_class=HTMLResponse)
def afinidad_estado(request: Request):
    """Estado del recalculo para el indicador que se autorrefresca (htmx polling) en
    /ajustes y /calendario/anual mientras `_recompute_with_status` esta corriendo."""
    with get_connection() as conn:
        status = repo.get_recompute_status(conn, request.state.user_id)
    return templates.TemplateResponse(request, "partials/recompute_status.html", {"status": status})


def _peso(raw, actual: float) -> float:
    """Peso del formulario de afinidad: número finito y >= 0 (un negativo o "nan"
    rompería las medias ponderadas del motor); si no, se queda el que había."""
    try:
        valor = float(raw)
    except ValueError:
        return actual
    return valor if math.isfinite(valor) and valor >= 0 else actual


@router.post("/ajustes/afinidad", response_class=HTMLResponse)
async def ajustes_afinidad(request: Request):
    """Guarda los pesos del índice de afinidad y relanza el recálculo en segundo plano:
    tarda del orden de un minuto, no se espera desde la petición."""
    form = await request.form()
    with get_connection() as conn:
        cfg = repo.get_affinity_config(conn, request.state.user_id)
        for group in ("appetite_weights", "quality_weights"):
            for key in cfg[group]:
                field = f"{group}__{key}"
                if field in form:
                    cfg[group][key] = _peso(form[field], cfg[group][key])
        for key in ("appetite_exp", "quality_exp", "gap_threshold", "gap_factor"):
            if key in form:
                try:
                    cfg[key] = float(form[key])
                except ValueError:
                    pass
        for key in cfg["predict_weights"]:
            field = f"predict_weights__{key}"
            if field in form:
                cfg["predict_weights"][key] = _peso(form[field], cfg["predict_weights"][key])
        repo.set_affinity_config(conn, request.state.user_id, cfg)

    asyncio.create_task(asyncio.to_thread(_recompute_with_status, request.state.user_id))
    return RedirectResponse("/estadisticas#gustos", status_code=303)


@router.post("/ajustes/afinidad/restablecer", response_class=HTMLResponse)
async def ajustes_afinidad_restablecer(request: Request):
    with get_connection() as conn:
        repo.reset_affinity_config(conn, request.state.user_id)

    asyncio.create_task(asyncio.to_thread(_recompute_with_status, request.state.user_id))
    return RedirectResponse("/estadisticas#gustos", status_code=303)


@router.get("/afinidad/calibracion")
def afinidad_calibracion():
    return RedirectResponse("/estadisticas#gustos", status_code=303)


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
