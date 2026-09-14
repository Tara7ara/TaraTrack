"""app.matching - heuristica de emparejamiento TMDB/AniList (freno de seguridad
antes de auto-enlazar/auto-anadir un titulo desde el calendario de temporada, y
fallback de busqueda cuando el titulo trae sufijo de temporada tipo "Season 2").
Usado por routers/buscar.py y routers/calendario.py."""
import difflib
import re

from app import tmdb


def _plausible_match(query: str, candidate: str) -> bool:
    """Freno de seguridad para no auto-aceptar el primer resultado de TMDB a ciegas -
    paso de verdad, 2026-08-13: un titulo del calendario acabo enlazado a una ficha de
    TMDB completamente distinta ("ワールド イズ ダンシング" en vez del anime real). Sin
    esto, si TMDB devuelve un top result que no tiene nada que ver, mejor caer al
    buscador (que ella confirme) que crear una entry equivocada sola."""
    q, c = (query or "").strip().lower(), (candidate or "").strip().lower()
    if not q or not c:
        return False
    return q in c or c in q or difflib.SequenceMatcher(None, q, c).ratio() >= 0.4




def _best_plausible_match(title: str, results: list):
    """Antes solo miraba results[0]: si TMDB devolvia primero un resultado sin
    relacion (agregador, spin-off...) y el titulo real quedaba 2º/3º, caia al
    buscador aunque hubiera un match real justo debajo - Tara: "me lleva al
    buscador" mas de lo esperado. Ahora prueba los 3 primeros y se queda con el
    primero que pase el freno de _plausible_match, sin relajar el propio freno."""
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
    """AniList mete el numero de temporada DENTRO del titulo (romaji Y english,
    p.ej. "Seihantai na Kimi to Boku 2nd Season" / "You and I Are Polar Opposites
    Season 2") mientras que TMDB casi siempre indexa el anime entero bajo un unico
    show con todas las temporadas dentro, sin ese sufijo en el nombre - bug real
    (Tara, calendario de temporada): tmdb.search() con el titulo completo no
    encontraba nada y ni siquiera /buscar a mano daba con la ficha. Quita el
    sufijo para reintentar con "el nombre normal"; vacio si no habia sufijo que
    quitar (para no reintentar la misma busqueda dos veces sin motivo)."""
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
    """Encaja una tarjeta del calendario de temporada con una ficha real de TMDB.
    AniList prefiere el titulo en ingles para el nombre mostrado, pero TMDB muchas
    veces solo indexa el romaji/original de un anime sin licencia occidental, nunca
    la traduccion al ingles - sin el segundo intento, tanto la busqueda como el
    freno de plausibilidad fallaban aunque la ficha SI existiera en TMDB (caso real,
    Tara: "You and I Are Polar Opposites Season 2" no encontraba nada bajo ningun
    titulo calculado desde el ingles; con el romaji "Seihantai na Kimi to Boku" si
    hay match directo). Prueba titulo, y solo si no cuaja, el romaji."""
    for candidate in [title] + ([romaji] if romaji and romaji != title else []):
        results = _tmdb_search_flexible(candidate)
        match = _best_plausible_match(candidate, results)
        if match:
            return match
    return None
