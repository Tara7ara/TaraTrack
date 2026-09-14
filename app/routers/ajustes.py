"""app.routers.ajustes - extraido de main.py en el split de modulos (ronda 2026-08-21)."""
import asyncio
import json
from datetime import date

from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from app import repo
from app.db import get_connection
from app.web import _recompute_with_status, templates

router = APIRouter()




@router.get("/ajustes", response_class=HTMLResponse)
def ajustes(request: Request):
    with get_connection() as conn:
        rechazados = repo.list_rejected_recommendations(conn)
        profile_history = repo.get_profile_history(conn)
        affinity_config = repo.get_affinity_config(conn)
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "rechazados": rechazados,
            "profile_history": profile_history,
            "affinity_config": affinity_config,
            # Valor de referencia junto a cada input de peso (auditoria visual
            # 2026-08-20) - de las constantes reales de repo.py, no numeros
            # duplicados a mano en la plantilla.
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





@router.get("/afinidad/estado", response_class=HTMLResponse)
def afinidad_estado(request: Request):
    """Estado del recalculo para el indicador que se autorrefresca (htmx polling) en
    /ajustes y /calendario/anual mientras `_recompute_with_status` esta corriendo."""
    with get_connection() as conn:
        status = repo.get_recompute_status(conn)
    return templates.TemplateResponse(request, "partials/recompute_status.html", {"status": status})




@router.post("/ajustes/afinidad", response_class=HTMLResponse)
async def ajustes_afinidad(request: Request):
    """Guarda los pesos del indice de afinidad (Fase 2 del encargo: "configurables
    desde /ajustes") y relanza el recalculo completo en background - mismo patron que
    "Actualizar perfil de gustos", no se espera desde la peticion HTTP (recompute_taste_profile
    tarda del orden de un minuto)."""
    form = await request.form()
    with get_connection() as conn:
        cfg = repo.get_affinity_config(conn)
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
        repo.set_affinity_config(conn, cfg)

    asyncio.create_task(asyncio.to_thread(_recompute_with_status))
    return RedirectResponse("/ajustes", status_code=303)




@router.post("/ajustes/afinidad/restablecer", response_class=HTMLResponse)
async def ajustes_afinidad_restablecer(request: Request):
    """`async def` a proposito, no `def`: FastAPI corre las rutas `def` normales en un
    hilo de threadpool sin event loop propio, y `asyncio.create_task` (para lanzar el
    recalculo sin esperarlo) revienta ahi con `RuntimeError: no running event loop` -
    bug real encontrado en pruebas 2026-08-14 al verificar el indicador de
    /afinidad/estado, preexistente (mismo fallo que tendria antes de este indicador,
    solo que nadie lo habia disparado). Mismo patron que la ruta gemela `ajustes_afinidad`
    (guardar pesos), que ya era `async def` y por eso nunca fallaba."""
    with get_connection() as conn:
        repo.reset_affinity_config(conn)

    asyncio.create_task(asyncio.to_thread(_recompute_with_status))
    return RedirectResponse("/ajustes", status_code=303)




@router.get("/afinidad/calibracion", response_class=HTMLResponse)
def afinidad_calibracion(request: Request):
    with get_connection() as conn:
        cal = repo.get_prediction_calibration(conn)
    return templates.TemplateResponse(request, "calibration.html", {"cal": cal})




@router.get("/ajustes/exportar")
def exportar_datos():
    """Volcado JSON de lo curado a mano (notas, comentarios, listas, waifus) - copia
    propia independiente del backup de la BBDD del servidor."""
    with get_connection() as conn:
        data = repo.export_data(conn)
    body = json.dumps(data, ensure_ascii=False, indent=2)
    return Response(
        content=body,
        media_type="application/json",
        headers={"Content-Disposition": f"attachment; filename=taratrack-{date.today().isoformat()}.json"},
    )




@router.post("/ajustes/rechazados/{rejected_id}/quitar", response_class=HTMLResponse)
def ajustes_quitar_rechazo(request: Request, rejected_id: int):
    with get_connection() as conn:
        repo.unreject_recommendation(conn, rejected_id)
    return RedirectResponse("/ajustes", status_code=303)
