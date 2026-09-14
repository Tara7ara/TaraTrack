from app import db, repo


def test_sync_library_records_status_on_success(conn):
    """Ronda 2026-08-21, punto 4 de la lista de Tara: guardar y mostrar ultimo sync
    correcto, duracion y si fallo. Con una BBDD vacia (0 titulos trackeables) no hay
    llamadas de red reales - sync_library debe seguir dejando su rastro en app_settings."""
    repo.sync_library()
    with db.get_connection() as c:
        status = repo.get_sync_status(c)

    assert status["running"] is False
    assert status["ok"] is True
    assert status["titles"] == 0
    assert status["errors"] == 0
    assert status["duration_seconds"] is not None
    assert status["started_at"] is not None
    assert status["finished_at"] is not None
    assert status["error_message"] == ""


def test_get_sync_status_before_any_sync_is_empty(conn):
    """Antes del primer sync (instalacion nueva) no debe petar - todo en blanco/False."""
    status = repo.get_sync_status(conn)
    assert status["running"] is False
    assert status["ok"] is False
    assert status["finished_at"] is None
    assert status["duration_seconds"] is None
