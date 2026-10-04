import os

os.environ.setdefault("TARATRACK_SECRET_KEY", "test-secret-para-tests")
os.environ.setdefault("TARATRACK_PASSWORD", "test-password")
os.environ.setdefault("TARATRACK_ADMIN_USERNAME", "principal")

import sqlite3
from datetime import date

import pytest
from fastapi.testclient import TestClient

from app import db, main, repo


def _watched_entry(conn, user_id, title_name="Serie vista"):
    title = repo.ensure_manual_title(conn, "show", title_name, 2020)
    conn.execute(
        "INSERT INTO entries (title_id, user_id, status, watched_at) VALUES (?, ?, 'watched', '2024-01-01T00:00:00Z')",
        (title["id"], user_id),
    )
    return conn.execute("SELECT * FROM entries WHERE title_id = ? AND user_id = ?", (title["id"], user_id)).fetchone()


def test_past_watches_count_but_leave_no_round_or_history(conn, user_id):
    """Los visionados sin fecha cuentan en count_plays, pero no abren vuelta nueva ni salen en
    el historial."""
    entry = _watched_entry(conn, user_id)
    repo.add_past_watch(conn, entry["id"])
    repo.add_past_watch(conn, entry["id"])
    assert repo.count_plays(conn, entry["id"]) == 3
    assert conn.execute("SELECT rewatch_started_at FROM entries WHERE id = ?", (entry["id"],)).fetchone()[0] is None
    assert all(h["detail"] != "Volver a ver" for h in repo.list_history(conn, user_id))


def test_remove_past_watch_never_touches_dated_rounds(conn, user_id):
    entry = _watched_entry(conn, user_id)
    repo.start_rewatch(conn, entry["id"])
    repo.add_past_watch(conn, entry["id"])
    assert repo.remove_past_watch(conn, entry["id"]) is True
    assert repo.remove_past_watch(conn, entry["id"]) is False
    assert repo.count_plays(conn, entry["id"]) == 2
    assert [h["detail"] for h in repo.list_history(conn, user_id)].count("Volver a ver") == 1


def test_migration_makes_watch_sessions_date_nullable_keeping_rows(tmp_path, monkeypatch):
    """Con watched_at NOT NULL, la migración rehace la tabla sin perder filas."""
    path = tmp_path / "vieja.db"
    monkeypatch.setattr(db, "DB_PATH", str(path))
    db.init_db()
    with db.get_connection() as conn:
        conn.executescript(
            """DROP TABLE watch_sessions;
               CREATE TABLE watch_sessions (id INTEGER PRIMARY KEY AUTOINCREMENT,
                 entry_id INTEGER NOT NULL REFERENCES entries(id), watched_at TEXT NOT NULL,
                 speed TEXT NOT NULL DEFAULT '1x', notes TEXT);"""
        )
        title = repo.ensure_manual_title(conn, "movie", "Peli", 2020)
        conn.execute("INSERT INTO entries (title_id, user_id, status) VALUES (?, 1, 'watched')", (title["id"],))
        conn.execute("INSERT INTO watch_sessions (entry_id, watched_at) VALUES (1, '2024-01-01T10:00:00Z')")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO watch_sessions (entry_id, watched_at) VALUES (1, NULL)")
    db.init_db()
    with db.get_connection() as conn:
        conn.execute("INSERT INTO watch_sessions (entry_id, watched_at) VALUES (1, NULL)")
        rows = conn.execute("SELECT id, watched_at FROM watch_sessions ORDER BY id").fetchall()
        assert [tuple(r) for r in rows] == [(1, "2024-01-01T10:00:00Z"), (2, None)]
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'idx_watch_sessions_entry'").fetchone()


def _fake(i, predict):
    return {"id": i, "predict": predict}


def test_pick_pending_same_all_day_and_from_top_pool():
    entries = [_fake(i, 100 - i) for i in range(60)]
    a = repo.pick_pending_for_today(entries, 1, today=date(2026, 10, 4))
    b = repo.pick_pending_for_today(entries, 1, today=date(2026, 10, 4))
    c = repo.pick_pending_for_today(entries, 1, today=date(2026, 10, 5))
    assert a == b and len(a) == 10
    assert {e["id"] for e in a} != {e["id"] for e in c}
    assert all(e["id"] < 30 for e in a + c)
    assert [e["predict"] for e in a] == sorted((e["predict"] for e in a), reverse=True)


def test_pick_pending_fills_with_random_when_no_predictions():
    """Sin ninguna predicción: 10 al azar del día."""
    entries = [_fake(i, None) for i in range(78)]
    picks = repo.pick_pending_for_today(entries, 2, today=date(2026, 10, 4))
    assert len(picks) == 10 and len({e["id"] for e in picks}) == 10
    mixed = [_fake(0, 90), _fake(1, 80)] + [_fake(i, None) for i in range(2, 20)]
    picks = repo.pick_pending_for_today(mixed, 2, today=date(2026, 10, 4))
    assert [e["id"] for e in picks[:2]] == [0, 1] and len(picks) == 10


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test.db"))
    with TestClient(main.app, base_url="https://testserver") as c:
        c.post("/login", data={"username": "principal", "password": "test-password", "next": "/"})
        yield c


def _principal_id(conn):
    return conn.execute("SELECT id FROM users WHERE username = 'principal'").fetchone()[0]


def test_visionados_route_htmx_and_ownership(client):
    with db.get_connection() as conn:
        entry = _watched_entry(conn, _principal_id(conn))
        conn.execute("INSERT INTO users (username, password_hash, password_salt) VALUES ('otra', 'x', 'x')")
        otra = _watched_entry(conn, conn.execute("SELECT id FROM users WHERE username = 'otra'").fetchone()[0], "Ajena")
    r = client.post(f"/entrada/{entry['id']}/visionados/sumar", headers={"HX-Request": "true"})
    assert r.status_code == 200 and "<b>2</b>" in r.text and 'id="plays"' in r.text
    r = client.post(f"/entrada/{entry['id']}/visionados/restar", headers={"HX-Request": "true"})
    assert "<b>1</b>" in r.text
    assert client.post(f"/entrada/{entry['id']}/visionados/otra-cosa").status_code == 422
    assert client.post(f"/entrada/{otra['id']}/visionados/sumar").status_code == 404


def test_inicio_shows_pending_row_only_with_three_or_more(client):
    with db.get_connection() as conn:
        uid = _principal_id(conn)
        _watched_entry(conn, uid, "Algo visto")  # para que Inicio no redirija a Pendientes
        for i in range(2):
            t = repo.ensure_manual_title(conn, "movie", f"Pendiente {i}", 2020)
            conn.execute("INSERT INTO entries (title_id, user_id, status) VALUES (?, ?, 'pending')", (t["id"], uid))
    r = client.get("/", headers={"HX-Request": "true"})
    assert r.status_code == 200 and 'id="home-pendientes"' not in r.text
    with db.get_connection() as conn:
        t = repo.ensure_manual_title(conn, "movie", "Pendiente 2", 2020)
        conn.execute("INSERT INTO entries (title_id, user_id, status) VALUES (?, ?, 'pending')", (t["id"], uid))
    r = client.get("/", headers={"HX-Request": "true"})
    assert 'id="home-pendientes"' in r.text and "Pendiente 2" in r.text
