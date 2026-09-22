"""app.repo.sync - extraido de repo.py en el split de modulos (ronda 2026-08-21).
Ver app/repo/__init__.py para el mapa completo de que vive en cada fichero -
el resto del proyecto sigue usando `from app import repo; repo.funcion(...)`
exactamente igual que antes, este split es puramente interno."""
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
    """Estado del ultimo sync completado (boton "Sincronizar" o tarea de fondo cada
    12h) - El usuario, tras la revision externa del codigo: "guardar y mostrar ultimo sync
    correcto, duracion y si fallo... es pequeño pero util cuando algo externo deja de
    responder". Mismo patron app_settings clave/valor que get_recompute_status."""
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
        # Fallo de verdad, no el "por titulo" ya capturado dentro (ese solo suma a
        # errors) - se re-lanza igual (calendario_sincronizar no lo captura, sync_loop
        # si) pero antes queda registrado para que /calendario lo enseñe.
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
            # Dos conexiones (= dos transacciones) por titulo, no una: refresh_metadata
            # ya escribe (UPDATE titles) antes de que sync_episodes haga sus propias
            # llamadas de red (get_details + episodios por temporada) - con todo en la
            # MISMA transaccion, el lock de escritura que agarra ese primer UPDATE se
            # quedaba sujeto durante esas llamadas de red tambien, no solo durante los
            # INSERT finales. Bug real (El usuario: "/pendientes tarda mucho a veces, pero
            # Docker no consume nada" - encaja exacto con estar bloqueado esperando el
            # lock, no computando): `snapshot_profile_progress` (la unica otra escritura
            # de la app, llamada en cada carga de /pendientes) fallaba con "database is
            # locked" en los logs de produccion, siempre en mitad de una sync de fondo.
            # Partiendo en dos transacciones cortas, cada una solo agarra el lock justo
            # antes de sus INSERT/UPDATE ya con los datos de red en mano, nunca durante
            # la espera de red en si.
            with get_connection() as conn:
                refresh_metadata(conn, title_row)
            if title_row["type"] == "show":
                with get_connection() as conn:
                    fresh = get_title(conn, title_row["tmdb_id"])
                    airing = fresh["show_status"] == "Returning Series" or fresh["next_episode_air_date"]
                    # Series empezadas (historial importado) pero sin fechas de emision
                    # cacheadas: una sincronizacion unica para que "Continuar viendo"
                    # sepa cuantos episodios faltan. Las terminadas no cambian, no se repite.
                    # Multiusuario Fase 2 (2026-09-17): "empezada" ya no mira
                    # episodes.watched_at (columna compartida, ya no se actualiza) -
                    # mira si ALGUIEN (cualquier usuario) tiene algun marcado real via
                    # episode_watches, que es lo que de verdad indica "esto se sigue".
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
                        # Autover: episodios recien sincronizados que ya emitieron se
                        # marcan vistos solos, reusando el mismo "doble tick" manual -
                        # asi nunca se acumulan esperando un click en Continuar viendo.
                        # Es un flag POR USUARIO (entries.auto_watch) - se aplica a cada
                        # entry que lo tenga activado, no solo a "la" entry del titulo
                        # (bug real de la primera version multiusuario: solo cogia una
                        # entry cualquiera, sin mirar de quien era ni si habia mas).
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

    # Multiusuario Fase 3 (2026-09-18): el indice de afinidad es por-usuario - la sync
    # de fondo recalcula el de CADA usuario con al menos una nota puesta (saltarse a
    # los que no tienen ninguna evita un recalculo vacio inutil). Un fallo en el
    # perfil de un usuario no debe impedir que se recalculen los demas.
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
