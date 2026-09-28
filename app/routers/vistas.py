"""app.routers.vistas - biblioteca vista, favoritos y búsqueda por título."""

from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app import repo, web
from app.db import get_connection
from app.web import templates

router = APIRouter()


VISTAS_TANDA = 60

# Filtro «Estado»: valor del desplegable -> textos de la tira que entran.
ESTADOS = [("emision", "En emisión"), ("proximamente", "Próximamente"), ("renovada", "Renovada"),
           ("pausa", "En pausa"), ("acabada", "Acabada"), ("cancelada", "Cancelada")]
ESTADOS_TIRA = {"emision": {"Emisión"}, "proximamente": {"Próx.", "Anunc.", "En prod."}, "renovada": {"Renovada"},
                "pausa": {"Pausa"}, "acabada": {"Acabada"}, "cancelada": {"Cancel."}}


@router.get("/vistas", response_class=HTMLResponse)
def vistas(
    request: Request, orden: str = "recientes", tipo: str = "", q: str = "", genero: str = "",
    direccion: str = "", elo_reset: str = "", desde: int = 0, parcial: str = "", estado: str = "",
):
    with get_connection() as conn:
        entries = repo.list_watched(conn, request.state.user_id, orden, tipo, q, genero, direccion)
    # Mismo estado que la tira de la carátula, así lo que filtras es lo que ves.
    if estado in ESTADOS_TIRA:
        entries = [e for e in entries if (web.show_status_ribbon(e) or ("",))[0] in ESTADOS_TIRA[estado]]
    else:
        estado = ""
    desde = max(0, desde)
    pagina = entries[desde:desde + VISTAS_TANDA]
    siguiente = desde + VISTAS_TANDA if desde + VISTAS_TANDA < len(entries) else None
    qs_vis = urlencode({"tipo": tipo, "orden": orden, "direccion": direccion, "q": q, "genero": genero, "estado": estado})
    if parcial:
        return templates.TemplateResponse(
            request, "partials/vis_cards.html", {"pagina": pagina, "siguiente": siguiente, "qs_vis": qs_vis}
        )
    with get_connection() as conn:
        default_list = repo.get_default_list(conn, request.state.user_id)
        generos = repo.list_genres(conn)
        coverage = repo.duel_coverage(conn, "entries", repo.list_watched_ids(conn, request.state.user_id))
        n_eps = conn.execute("SELECT count(*) FROM episode_watches WHERE user_id = ?", (request.state.user_id,)).fetchone()[0]
    notas = [e["rating"] for e in entries if e["rating"] is not None and not (e["rating"] == 5 and e["comment"] == "-")]
    resumen = {
        "media": (sum(notas) / len(notas)) if notas else None, "con_nota": len(notas),
        "top": sum(1 for n in notas if n >= 9), "episodios": n_eps,
    }
    direccion_actual = direccion if direccion in ("asc", "desc") else repo.ORDENES_VISTAS_DEFAULT_DIR.get(orden, "desc")
    return templates.TemplateResponse(
        request,
        "watched.html",
        {
            "entries": entries,
            "pagina": pagina,
            "siguiente": siguiente,
            "qs_vis": qs_vis,
            "orden": orden,
            "tipo": tipo,
            "q": q,
            "genero": genero,
            "generos": generos,
            "direccion": direccion_actual,
            "favoritos_id": default_list["id"] if default_list else None,
            "coverage": coverage,
            "resumen": resumen,
            "estado": estado,
            "estados": ESTADOS,
            "elo_reset": bool(elo_reset),
        },
    )
