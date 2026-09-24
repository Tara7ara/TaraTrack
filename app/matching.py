"""app.matching - heuristica de emparejamiento TMDB/AniList (freno de seguridad
antes de auto-enlazar/auto-anadir un titulo desde el calendario de temporada, y
fallback de busqueda cuando el titulo trae sufijo de temporada tipo "Season 2").
Usado por routers/buscar.py y routers/calendario.py."""
import difflib
import re

from app import tmdb


def _plausible_match(query: str, candidate: str) -> bool:
    """Evita aceptar a ciegas el primer resultado de TMDB: si no se parece al título
    buscado, es mejor mandar al buscador que enlazar una ficha equivocada."""
    q, c = (query or "").strip().lower(), (candidate or "").strip().lower()
    if not q or not c:
        return False
    return q in c or c in q or difflib.SequenceMatcher(None, q, c).ratio() >= 0.4




def _best_plausible_match(title: str, results: list):
    """Primer resultado plausible entre los 3 primeros de TMDB (el correcto no
    siempre sale el primero)."""
    for r in results[:3]:
        if _plausible_match(title, r["title"]):
            return r
    return None




_SEASON_SUFFIX_RE = re.compile(
    r"[:\-\s]+(?:"
    r"season\s*\d+|temporada\s*\d+|"
    r"\d+(?:st|nd|rd|th)\s*season|"
    r"part\s*\d+|parte\s*\d+|cour\s*\d+"
    r")\s*$",
    re.IGNORECASE,
)




def _strip_season_suffix(title: str) -> str:
    """Quita el sufijo de temporada ("2nd Season", "Season 2"...) que AniList mete en
    el título: TMDB agrupa todas las temporadas bajo un único show. Devuelve "" si no
    había sufijo."""
    stripped = _SEASON_SUFFIX_RE.sub("", title or "").strip()
    return stripped if stripped and stripped.lower() != (title or "").strip().lower() else ""




def _tmdb_search_flexible(title: str) -> list[dict]:
    """tmdb.search(title) y, si no hay nada, reintenta sin el sufijo de temporada
    (ver _strip_season_suffix) antes de rendirse - mismo espiritu que el fallback a
    AniList ya existente en /buscar/resultados, pero para el caso mas comun."""
    try:
        results = tmdb.search(title)
    except Exception:
        results = []
    if not results:
        stripped = _strip_season_suffix(title)
        if stripped:
            try:
                results = tmdb.search(stripped)
            except Exception:
                pass
    return results




def _resolve_calendar_match(title: str, romaji: str = ""):
    """Empareja una tarjeta del calendario de temporada con una ficha de TMDB: prueba
    el título mostrado y, si no cuaja, el romaji (TMDB a menudo solo indexa ese)."""
    for candidate in [title] + ([romaji] if romaji and romaji != title else []):
        results = _tmdb_search_flexible(candidate)
        match = _best_plausible_match(candidate, results)
        if match:
            return match
    return None
