"""app.web - infraestructura compartida entre routers: templates Jinja2 y sus
filtros/globales custom. Separado de main.py para que los routers puedan importar
`templates` sin crear un import circular (main.py incluye los routers, los routers
no pueden importar de vuelta desde main.py)."""
import re
import time
from datetime import date, datetime, timezone

from fastapi.templating import Jinja2Templates

from app import repo
from app.db import get_connection

templates = Jinja2Templates(directory="app/templates")




def fmt_rating(value, decimals=2):
    """10.0 -> '10', 8.5 -> '8.5' - fuera los .0 en notas redondas."""
    if value is None:
        return ""
    return f"{value:.{decimals}f}".rstrip("0").rstrip(".")




def fmt_duration(minutes):
    """112 -> '1 h 52 min', 45 -> '45 min'."""
    if not minutes:
        return ""
    hours, mins = divmod(int(minutes), 60)
    if hours and mins:
        return f"{hours} h {mins} min"
    return f"{hours} h" if hours else f"{mins} min"




DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]


MESES = [
    "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
]




def fmt_day(date_str):
    """'2026-08-11' -> 'Hoy', 'Mañana', 'Ayer' o 'miércoles 12 de agosto'."""
    try:
        d = date.fromisoformat(date_str)
    except (TypeError, ValueError):
        return date_str or ""
    delta = (d - date.today()).days
    if delta == 0:
        return "Hoy"
    if delta == 1:
        return "Mañana"
    if delta == -1:
        return "Ayer"
    label = f"{DIAS[d.weekday()]} {d.day} de {MESES[d.month - 1]}"
    return label if d.year == date.today().year else f"{label} de {d.year}"




def fmt_month(mm: str) -> str:
    """'08' -> 'agosto' - para el resumen anual."""
    try:
        return MESES[int(mm) - 1]
    except (TypeError, ValueError, IndexError):
        return mm




def fmt_ago(iso_str):
    """'2026-08-21T12:00:00Z' -> 'hace 3 min' / 'hace 2 h' / 'hace 5 días' - para el
    estado del ultimo sync en /calendario (Tara: "guardar y mostrar ultimo sync
    correcto, duracion y si fallo")."""
    if not iso_str:
        return ""
    try:
        dt = datetime.strptime(iso_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return iso_str
    seconds = (datetime.now(timezone.utc) - dt).total_seconds()
    if seconds < 60:
        return "hace un momento"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"hace {minutes} min"
    hours = int(minutes // 60)
    if hours < 24:
        return f"hace {hours} h"
    days = int(hours // 24)
    return f"hace {days} día{'s' if days != 1 else ''}"




def fmt_sync_duration(seconds):
    """8.3 -> '8 s', 125.0 -> '2 min 5 s' - a diferencia de fmt_duration (que trabaja
    en minutos, pensado para runtime de episodios) un sync puede durar solo unos
    segundos y redondear a minutos ahi perderia toda la señal."""
    if seconds is None:
        return ""
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds} s"
    minutes, secs = divmod(seconds, 60)
    return f"{minutes} min {secs} s" if secs else f"{minutes} min"




_TMDB_SIZE_RE = re.compile(r"/t/p/w\d+/")




def poster_size(url, size):
    """Cambia la resolucion de una URL de TMDB (siempre guardada en w500) a `size`
    (p.ej. 'w185'). Los posters locales (/static/posters/...) y el placeholder
    sin-portada no llevan ese patron, se devuelven tal cual - Tara: pantallas sin
    Retina (27"/23.8") no necesitan pedir siempre la version mas pesada."""
    if not url or "image.tmdb.org" not in url:
        return url
    return _TMDB_SIZE_RE.sub(f"/t/p/{size}/", url)




templates.env.filters["fmt_rating"] = fmt_rating
templates.env.filters["fmt_duration"] = fmt_duration
templates.env.filters["fmt_day"] = fmt_day
templates.env.filters["fmt_month"] = fmt_month
templates.env.filters["fmt_ago"] = fmt_ago
templates.env.filters["fmt_sync_duration"] = fmt_sync_duration
templates.env.filters["poster_size"] = poster_size
templates.env.filters["elo_confidence"] = repo.elo_confidence_label
# Cache-busting de estaticos propios: /static se cachea 7 dias, asi que sin esto un
# cambio de CSS tarda una semana en llegar al movil de Tara. Cambia en cada arranque.
templates.env.globals["static_v"] = int(time.time())


def _puntuar_queue_count() -> int:
    """Contador vivo para el nav (topnav agrupado). Registrado como global de Jinja
    en vez de context_processor a proposito: un context_processor correria en CADA
    TemplateResponse, incluidos los partials de htmx (marcar episodio, favorito...),
    sumando una conexion/query de mas a cada micro-interaccion - aqui solo se paga
    el coste cuando base.html la llama de verdad (paginas completas, no partials)."""
    try:
        with get_connection() as conn:
            return repo.count_review_queue(conn)
    except Exception:
        return 0


templates.env.globals["puntuar_queue_count"] = _puntuar_queue_count


def render_nav_cola_oob(conn) -> str:
    """Fragmento OOB (hx-swap-oob) para refrescar el indicador '✦' de Puntuar sin
    recargar la pagina - solo se llama desde las 3 rutas que pueden cambiar la cola
    con una respuesta parcial (toggle de episodio, marcar todos/siguiente episodio),
    nunca desde el resto de partials, mismo criterio de coste que _puntuar_queue_count
    de arriba. Los ids deben coincidir con los de base.html (desktop y movil)."""
    cola = repo.count_review_queue(conn)
    hidden = "" if cola else " hidden"
    span = (
        '<span id="{id}" class="nav-cola" hx-swap-oob="true"'
        ' title="Tienes títulos pendientes de puntuar"{hidden}>✦</span>'
    )
    return span.format(id="nav-cola-desktop", hidden=hidden) + span.format(
        id="nav-cola-mobile", hidden=hidden
    )


def _recompute_with_status(extra=None):
    """Envuelve recompute_taste_profile marcando running=True/False en app_settings
    (repo.set_recompute_running) - sin esto, lanzar el recalculo desde /ajustes o
    /calendario/anual redirigia al instante sin ninguna señal de que estuviera
    pasando algo (Tara, 2026-08-14). `extra`, si se da, corre ANTES del recalculo y
    cuenta dentro de la misma ventana de "running" (el backfill de AniList de
    /calendario/anual/perfil, que puede tardar varios minutos el solo). `finally`
    para que un fallo a medias no deje el indicador pegado en "recalculando" para
    siempre. Compartida por routers/ajustes.py y routers/calendario.py, ver ahi."""
    with get_connection() as conn:
        repo.set_recompute_running(conn, True)
    try:
        if extra:
            extra()
        with get_connection() as conn:
            repo.recompute_taste_profile(conn)
    finally:
        with get_connection() as conn:
            repo.set_recompute_running(conn, False)
