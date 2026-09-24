from app import db, repo


def test_sync_library_records_status_on_success(conn):
    """sync_library deja su estado en app_settings incluso con una BBDD vacía (sin
    llamadas de red)."""
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
