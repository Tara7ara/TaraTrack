"""app.web - infraestructura compartida entre routers: templates Jinja2 y sus
filtros/globales custom. Separado de main.py para que los routers puedan importar
`templates` sin crear un import circular (main.py incluye los routers, los routers
no pueden importar de vuelta desde main.py)."""
import base64
import os
import re
import time
from datetime import date, datetime, timezone

from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape

from app import repo, thumbs
from app.db import get_connection

templates = Jinja2Templates(directory="app/templates")


def avatar(username: str, avatar_path: str | None = None) -> Markup:
    """Foto de perfil si la hay; si no, iniciales con un color estable derivado del
    nombre. Único sitio que genera este marcado."""
    username = username or "?"
    if avatar_path:
        return Markup(f'<img class="avatar avatar-img" src="{escape(avatar_path)}" alt="{escape(username)}">')
    idx = sum(ord(c) for c in username) % 6
    return Markup(f'<span class="avatar avatar-{idx}">{escape(username[0].upper())}</span>')




def fmt_rating(value, decimals=2):
    """10.0 -> '10', 8.5 -> '8.5' - fuera los .0 en notas redondas."""
    if value is None:
        return ""
    return f"{value:.{decimals}f}".rstrip("0").rstrip(".")




_SHOW_STATUS_ES = {
    # 'Returning Series' no significa "en emisión" (sigue activo entre temporadas):
    # "Continúa" no promete un próximo episodio inminente.
    "Returning Series": "Continúa",
    "In Production": "En producción",
    "Planned": "Anunciada",
    "Pilot": "Piloto",
    "Ended": "Finalizada",
    "Canceled": "Cancelada",
}


def fmt_show_status(value):
    return _SHOW_STATUS_ES.get(value, value or "")


def show_status_ribbon(title_row):
    """(texto, clase) de la tira de estado en la esquina del póster, o None. "En
    emisión" sale de que haya próximo episodio con fecha, no del estado de TMDB."""
    if title_row["type"] != "show" or not title_row["show_status"]:
        return None
    status = title_row["show_status"]
    if status in ("Returning Series", "In Production") and title_row["next_episode_air_date"]:
        # Próximo episodio = el 1 de una temporada: aún no ha empezado.
        label = title_row["next_episode_label"] if "next_episode_label" in title_row.keys() else None
        if re.match(r"T\d+E1\b", label or ""):
            return "Prox.", "ribbon-planned"
        return "Emisión", "ribbon-airing"
    if status == "Returning Series":
        # Sin proximo episodio con fecha: entre temporadas ("Continua" no le gustaba)
        return "Pausa", "ribbon-returning"
    css = {"Ended": "ribbon-ended", "Canceled": "ribbon-canceled"}.get(status, "ribbon-planned")
    return _RIBBON_SHORT.get(status, fmt_show_status(status)), css


# Textos cortos para la tira: con ellos cabe pegada a la esquina, tapando poca portada.
# El estado completo sigue en el chip de la ficha (fmt_show_status).
_RIBBON_SHORT = {"Ended": "Acabada", "Canceled": "Cancel.", "In Production": "En prod.", "Planned": "Anunc."}




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
    """'2026-08-21T12:00:00Z' o '2026-08-21 12:00:00' (datetime('now') de SQLite) ->
    'hace 3 min' / 'hace 2 h' / 'hace 5 días'."""
    if not iso_str:
        return ""
    try:
        dt = datetime.fromisoformat(iso_str)
    except ValueError:
        return iso_str
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
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
    """Cambia la resolución de una URL de TMDB (guardada en w500) a `size` (p. ej.
    'w185'). Los pósters locales y el placeholder se devuelven tal cual."""
    if not url:
        return url
    if "image.tmdb.org" not in url:
        # Portada local: miniatura para los tamaños pequeños (ver app/thumbs.py).
        return thumbs.thumb_url(url, size) or url
    return _TMDB_SIZE_RE.sub(f"/t/p/{size}/", url)




templates.env.filters["fmt_rating"] = fmt_rating
templates.env.filters["fmt_show_status"] = fmt_show_status
templates.env.filters["show_status_ribbon"] = show_status_ribbon
templates.env.filters["fmt_duration"] = fmt_duration
templates.env.filters["fmt_day"] = fmt_day
templates.env.filters["fmt_month"] = fmt_month
templates.env.filters["fmt_ago"] = fmt_ago
templates.env.filters["fmt_sync_duration"] = fmt_sync_duration
templates.env.filters["poster_size"] = poster_size
templates.env.filters["elo_confidence"] = repo.elo_confidence_label
templates.env.filters["avatar"] = avatar
# Cache-busting de estáticos propios: /static se cachea 7 días. Cambia en cada arranque.
templates.env.globals["static_v"] = int(time.time())

# El icono de "Añadir a pantalla de inicio" va en la propia página (data URI): iOS lo
# descarga en un proceso aparte que no acepta un certificado autofirmado.
with open(os.path.join(os.path.dirname(__file__), "static/img/apple-touch-icon.png"), "rb") as _f:
    templates.env.globals["touch_icon_data_uri"] = "data:image/png;base64," + base64.b64encode(_f.read()).decode()


def _puntuar_queue_count(user_id: int) -> int:
    """Contador del nav. Es un global de Jinja y no un context_processor para que solo
    se consulte en páginas completas (base.html), no en cada partial de htmx."""
    try:
        with get_connection() as conn:
            return repo.count_review_queue(conn, user_id)
    except Exception:
        return 0


templates.env.globals["puntuar_queue_count"] = _puntuar_queue_count


def _comments_badge_count(user_id: int) -> int:
    """Contador de comentarios nuevos del nav, con el mismo criterio que
    _puntuar_queue_count."""
    try:
        with get_connection() as conn:
            return repo.count_unseen_comments(conn, user_id)
    except Exception:
        return 0


templates.env.globals["comments_badge_count"] = _comments_badge_count


def _waifus_label(user_id: int) -> str:
    """"Waifus" o "Personajes favoritos" según el usuario tenga anime en su biblioteca."""
    try:
        with get_connection() as conn:
            return "Waifus" if repo.user_has_anime(conn, user_id) else "Personajes favoritos"
    except Exception:
        return "Personajes favoritos"


templates.env.globals["waifus_label"] = _waifus_label


def render_nav_cola_oob(conn, user_id: int) -> str:
    """Fragmento OOB (hx-swap-oob) para refrescar el indicador '✦' de Puntuar sin
    recargar la pagina - solo se llama desde las 3 rutas que pueden cambiar la cola
    con una respuesta parcial (toggle de episodio, marcar todos/siguiente episodio),
    nunca desde el resto de partials, mismo criterio de coste que _puntuar_queue_count
    de arriba. Los ids deben coincidir con los de base.html (desktop y movil)."""
    cola = repo.count_review_queue(conn, user_id)
    hidden = "" if cola else " hidden"
    span = (
        '<span id="{id}" class="nav-cola" hx-swap-oob="true"'
        ' title="Tienes títulos pendientes de puntuar"{hidden}>✦</span>'
    )
    return span.format(id="nav-cola-desktop", hidden=hidden) + span.format(
        id="nav-cola-mobile", hidden=hidden
    )


def _recompute_with_status(user_id, extra=None):
    """Envuelve recompute_taste_profile marcando running en app_settings, para el
    indicador de /ajustes y /calendario/anual. `extra` corre antes dentro de la misma
    ventana (el backfill de AniList). `finally` para que un fallo no deje el indicador
    pegado. Solo recalcula el perfil de este usuario."""
    with get_connection() as conn:
        repo.set_recompute_running(conn, user_id, True)
    try:
        if extra:
            extra()
        with get_connection() as conn:
            repo.recompute_taste_profile(conn, user_id)
    finally:
        with get_connection() as conn:
            repo.set_recompute_running(conn, user_id, False)
