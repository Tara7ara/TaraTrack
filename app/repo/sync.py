"""app.repo.sync - sincronización de la biblioteca con TMDB y estado del último sync.
El resto del proyecto usa `from app import repo; repo.funcion(...)`."""
import logging
import time
from datetime import datetime, timezone

from app.db import get_connection
from app.repo._shared import (
    get_setting,
    set_setting,
)
from app.repo.affinity import recompute_taste_profile
from app.repo.titles import get_title, list_trackable_titles, mark_all_aired_watched, refresh_metadata, sync_episodes
from app.repo.users import list_users


def get_sync_status(conn):
    """Estado del último sync (botón "Sincronizar" o tarea de fondo): cuándo, cuánto
    tardó y si falló. En app_settings, como get_recompute_status."""
    raw_duration = get_setting(conn, "sync_last_duration_seconds")
    return {
        "running": get_setting(conn, "sync_running") == "1",
        "started_at": get_setting(conn, "sync_last_started_at"),
        "finished_at": get_setting(conn, "sync_last_finished_at"),
        "duration_seconds": float(raw_duration) if raw_duration is not None else None,
        "titles": int(get_setting(conn, "sync_last_titles") or 0),
        "errors": int(get_setting(conn, "sync_last_errors") or 0),
        "ok": get_setting(conn, "sync_last_ok") == "1",
        "error_message": get_setting(conn, "sync_last_error_message"),
    }




def sync_library():
    """Refresca metadatos de todo lo seguido y los episodios de las series en emision.
    La usan el boton "Sincronizar" del calendario y la tarea automatica de fondo.
    Una conexion (= una transaccion) POR TITULO: la sync tarda minutos y con una sola
    transaccion larga dejaria la BBDD bloqueada para la app mientras tanto."""
    start = time.monotonic()
    with get_connection() as conn:
        set_setting(conn, "sync_running", "1")
        set_setting(conn, "sync_last_started_at", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
        titles = list_trackable_titles(conn)
    errors = 0
    sync_error = None
    try:
        errors = _sync_library_body(titles)
    except Exception as e:
        # Fallo general (los de cada título ya se capturan dentro y solo suman a
        # errors): se registra para que /calendario lo enseñe y se relanza.
        sync_error = str(e)[:500]
        raise
    finally:
        with get_connection() as conn:
            set_setting(conn, "sync_running", "0")
            set_setting(conn, "sync_last_finished_at", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
            set_setting(conn, "sync_last_duration_seconds", str(round(time.monotonic() - start, 1)))
            set_setting(conn, "sync_last_titles", str(len(titles)))
            set_setting(conn, "sync_last_errors", str(errors))
            set_setting(conn, "sync_last_ok", "0" if sync_error else "1")
            set_setting(conn, "sync_last_error_message", sync_error or "")




def _sync_library_body(titles) -> int:
    """Cuerpo real de sync_library, separado solo para poder envolverlo en el
    try/finally de arriba sin duplicar el resto de la funcion. Devuelve el numero
    de titulos que fallaron (no tumba la sync entera por uno malo)."""
    errors = 0
    for title_row in titles:
        try:
            # Dos transacciones cortas por título: si refresh_metadata y sync_episodes
            # compartieran una, el lock de escritura del primer UPDATE se mantendría
            # durante las llamadas de red y el resto de la app vería "database is
            # locked".
            with get_connection() as conn:
                refresh_metadata(conn, title_row)
            if title_row["type"] == "show":
                with get_connection() as conn:
                    fresh = get_title(conn, title_row["tmdb_id"])
                    airing = fresh["show_status"] == "Returning Series" or fresh["next_episode_air_date"]
                    # Series empezadas sin fechas de emisión cacheadas: se sincronizan una
                    # vez para que "Continuar viendo" sepa cuántos episodios faltan.
                    # "Empezada" = algún usuario tiene un visionado en episode_watches.
                    started_without_dates = conn.execute(
                        """SELECT EXISTS(SELECT 1 FROM episode_watches
                                         JOIN episodes ON episodes.id = episode_watches.episode_id
                                         WHERE episodes.title_id = ?)
                                  AND NOT EXISTS(SELECT 1 FROM episodes WHERE title_id = ?
                                                 AND air_date IS NOT NULL)""",
                        (fresh["id"], fresh["id"]),
                    ).fetchone()[0]
                    if airing or started_without_dates:
                        sync_episodes(conn, fresh)
                        # Autover: los episodios recién sincronizados que ya emitieron se
                        # marcan vistos para cada entry que lo tenga activado.
                        auto_entries = conn.execute(
                            "SELECT id FROM entries WHERE title_id = ? AND auto_watch = 1", (fresh["id"],)
                        ).fetchall()
                        for auto_entry in auto_entries:
                            mark_all_aired_watched(conn, auto_entry["id"])
        except Exception:
            errors += 1
            logging.warning("sync_library: fallo en %s (tmdb_id=%s)", title_row["title"], title_row["tmdb_id"])
            continue
    logging.info("sync_library: %d titulos, %d fallos", len(titles), errors)

    # Recalcula el índice de afinidad de cada usuario con al menos una nota. Un fallo
    # en uno no impide los demás.
    with get_connection() as conn:
        user_ids = [u["id"] for u in list_users(conn)]
    for user_id in user_ids:
        try:
            with get_connection() as conn:
                has_ratings = conn.execute(
                    "SELECT EXISTS(SELECT 1 FROM entries WHERE user_id = ? AND rating IS NOT NULL)", (user_id,)
                ).fetchone()[0]
                if not has_ratings:
                    continue
                n_attrs, n_scores = recompute_taste_profile(conn, user_id)
            logging.info(
                "recompute_taste_profile: usuario %d, %d atributos, %d titulos en el backtesting",
                user_id, n_attrs, n_scores,
            )
        except Exception:
            logging.warning(
                "recompute_taste_profile: fallo para el usuario %d, se conserva el perfil anterior",
                user_id, exc_info=True,
            )

    return errors
