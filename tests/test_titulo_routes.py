import os

os.environ.setdefault("TARATRACK_SECRET_KEY", "test-secret-para-tests")
os.environ.setdefault("TARATRACK_PASSWORD", "test-password")
os.environ.setdefault("TARATRACK_ADMIN_USERNAME", "tara")

import pytest
from fastapi.testclient import TestClient

from app import db, main, repo


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Mismo patron que tests/test_login.py - TestClient real logueado como tara,
    para probar rutas de verdad via HTTP (no solo las funciones de repo por debajo),
    que es la unica forma de cazar un router que se olvida de pasar un argumento
    nuevo a una funcion de repo (bug real, ver test de abajo)."""
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test.db"))
    with TestClient(main.app, base_url="https://testserver") as c:
        c.post("/login", data={"username": "tara", "password": "test-password", "next": "/"})
        yield c


def test_marcar_vista_rapido_on_a_show_does_not_500(client):
    """Bug real (2026-09-18, Tara: 'no puedo marcar como vista pero si como
    pendiente'): marcar_vista_rapido (POST /vista/{tmdb_id}/show) llamaba a
    repo.get_home_card(conn, entry_id) sin el user_id que ya es obligatorio desde el
    multiusuario (Fase 2) - TypeError, 500 en cualquier serie. Titulo manual (sin
    tmdb real) para no depender de la red en el test - sync_episodes fallara (API key
    de test) pero la ruta ya lo tolera (ver comentario de marcar_vista_rapido)."""
    with db.get_connection() as conn:
        title = repo.ensure_manual_title(conn, "show", "Serie de prueba", 2020)
        tmdb_id, tipo = title["tmdb_id"], title["type"]

    r = client.post(f"/vista/{tmdb_id}/{tipo}")
    assert r.status_code == 200


def test_marcar_vista_rapido_on_a_movie_does_not_500(client):
    with db.get_connection() as conn:
        title = repo.ensure_manual_title(conn, "movie", "Peli de prueba", 2020)
        tmdb_id, tipo = title["tmdb_id"], title["type"]

    r = client.post(f"/vista/{tmdb_id}/{tipo}")
    assert r.status_code == 200


def test_episodio_toggle_opens_debate_thread_when_marking_watched(client):
    """Tara, 2026-09-18 ('estilo tvtime'): marcar un episodio visto abre solo su
    hilo de debate, sin tener que ir a buscarlo despues."""
    with db.get_connection() as conn:
        title = repo.ensure_manual_title(conn, "show", "Serie con debate", 2020)
        conn.execute(
            "INSERT INTO episodes (title_id, season_number, episode_number) VALUES (?, 1, 1)",
            (title["id"],),
        )
        ep = conn.execute("SELECT * FROM episodes WHERE title_id = ?", (title["id"],)).fetchone()

    r = client.post(f"/episodio/{ep['id']}/toggle")
    assert r.status_code == 200
    assert '<details class="ep-more ep-debate" open' in r.text

    # Desmarcar (segunda vez) no debe reabrirlo.
    r2 = client.post(f"/episodio/{ep['id']}/toggle")
    assert '<details class="ep-more ep-debate" open' not in r2.text


def test_titulo_detalle_opens_debate_thread_via_comentar_param(client):
    with db.get_connection() as conn:
        title = repo.ensure_manual_title(conn, "show", "Serie con dos eps", 2020)
        conn.execute(
            "INSERT INTO episodes (title_id, season_number, episode_number) VALUES (?, 1, 1)", (title["id"],)
        )
        conn.execute(
            "INSERT INTO episodes (title_id, season_number, episode_number) VALUES (?, 1, 2)", (title["id"],)
        )
        ep1, ep2 = conn.execute(
            "SELECT * FROM episodes WHERE title_id = ? ORDER BY episode_number", (title["id"],)
        ).fetchall()
        tmdb_id, tipo = title["tmdb_id"], title["type"]

    r = client.get(f"/titulo/{tmdb_id}/{tipo}?comentar={ep1['id']}")
    assert r.status_code == 200
    assert f'id="ep-{ep1["id"]}"' in r.text
    assert f'id="ep-{ep2["id"]}"' in r.text
    # Solo el episodio pedido (ep1) se abre, no el resto (ep2).
    assert r.text.count('ep-debate" open') == 1


def test_marcar_siguiente_muestra_aviso_de_comentar(client):
    """Tara, 2026-09-18: el aviso "¿comentas?" tiene que salir en el sitio donde de
    verdad se marcan episodios dia a dia (Continuar viendo en /pendientes)."""
    with db.get_connection() as conn:
        title = repo.ensure_manual_title(conn, "show", "Serie de continuar viendo", 2020)
        conn.execute(
            "INSERT INTO entries (title_id, user_id, status) VALUES (?, ?, 'pending')",
            (title["id"], _user_id(conn)),
        )
        entry = conn.execute("SELECT * FROM entries WHERE title_id = ?", (title["id"],)).fetchone()
        conn.execute(
            "INSERT INTO episodes (title_id, season_number, episode_number, air_date) VALUES (?, 1, 1, '2020-01-01')",
            (title["id"],),
        )

    r = client.post(f"/entrada/{entry['id']}/siguiente")
    assert r.status_code == 200
    assert 'id="comment-toast"' in r.text
    assert ">Comentar<" in r.text


def _user_id(conn):
    return conn.execute("SELECT id FROM users ORDER BY id LIMIT 1").fetchone()["id"]


def test_comentarios_siguiente_redirects_to_the_right_episode_and_marks_seen(client):
    with db.get_connection() as conn:
        conn.execute("UPDATE users SET comments_seen_at = '2000-01-01 00:00:00' WHERE username = 'tara'")
        other = repo.create_user(conn, "amigo", "unaclave123")
        title = repo.ensure_manual_title(conn, "show", "Serie con aviso", 2020)
        conn.execute(
            "INSERT INTO episodes (title_id, season_number, episode_number) VALUES (?, 1, 1)", (title["id"],)
        )
        ep = conn.execute("SELECT * FROM episodes WHERE title_id = ?", (title["id"],)).fetchone()
        repo.add_episode_comment(conn, ep["id"], other["id"], "del amigo")
        tmdb_id, tipo = title["tmdb_id"], title["type"]

    r = client.get("/comentarios/siguiente", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == f"/titulo/{tmdb_id}/{tipo}?comentar={ep['id']}#ep-{ep['id']}"

    with db.get_connection() as conn:
        assert repo.count_unseen_comments(conn, _user_id(conn)) == 0


def test_comentarios_siguiente_sin_novedades_va_a_pendientes(client):
    r = client.get("/comentarios/siguiente", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/pendientes"


def test_puntuar_temporada_con_texto_no_numerico_no_revienta(client):
    """Bug real (AGY, 2026-09-18): a diferencia de marcar_vista_submit, esto
    convertia a float sin proteger - un valor no numerico tumbaba la peticion con
    un 500 en vez de ignorarlo."""
    with db.get_connection() as conn:
        title = repo.ensure_manual_title(conn, "show", "Serie con temporada", 2020)
        tmdb_id, tipo = title["tmdb_id"], title["type"]

    r = client.post(
        f"/titulo/{tmdb_id}/{tipo}/temporada/1/puntuar",
        data={"rating": "no-es-un-numero", "comment": ""},
        follow_redirects=False,
    )
    assert r.status_code == 303


def test_corregir_dia_emision_con_valor_no_numerico_no_revienta(client):
    """Bug real (AGY, 2026-09-18): int(weekday) sin validar tumbaba la peticion con
    un 500 ante un POST manipulado con un valor no numerico."""
    with db.get_connection() as conn:
        title = repo.ensure_manual_title(conn, "show", "Serie con dia raro", 2020)
        conn.execute("UPDATE titles SET anilist_id = 12345 WHERE id = ?", (title["id"],))
        tmdb_id, tipo = title["tmdb_id"], title["type"]

    r = client.post(
        f"/titulo/{tmdb_id}/{tipo}/dia-emision", data={"weekday": "lunes"}, follow_redirects=False
    )
    assert r.status_code == 303


def test_marcar_temporada_creates_entry_if_missing(client):
    """Bug real (AGY, 2026-09-18): entrar directo a la ficha y darle a "Temp. completa"
    sin haber pulsado antes "+ Pendientes" no encontraba entry y no marcaba nada."""
    with db.get_connection() as conn:
        title = repo.ensure_manual_title(conn, "show", "Serie de temporada", 2020)
        conn.execute(
            "INSERT INTO episodes (title_id, season_number, episode_number, air_date) VALUES (?, 1, 1, '2020-01-01')",
            (title["id"],),
        )
        tmdb_id, tipo = title["tmdb_id"], title["type"]

    r = client.post(f"/titulo/{tmdb_id}/{tipo}/temporada/1/marcar", follow_redirects=False)
    assert r.status_code == 303

    with db.get_connection() as conn:
        entry = conn.execute("SELECT * FROM entries WHERE title_id = ?", (title["id"],)).fetchone()
        assert entry is not None
        watched = conn.execute(
            "SELECT count(*) FROM episode_watches WHERE episode_id IN (SELECT id FROM episodes WHERE title_id = ?)",
            (title["id"],),
        ).fetchone()[0]
        assert watched == 1
