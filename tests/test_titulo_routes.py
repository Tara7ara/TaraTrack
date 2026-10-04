import os

os.environ.setdefault("TARATRACK_SECRET_KEY", "test-secret-para-tests")
os.environ.setdefault("TARATRACK_PASSWORD", "test-password")
os.environ.setdefault("TARATRACK_ADMIN_USERNAME", "principal")

import pytest
from fastapi.testclient import TestClient

from app import db, main, repo


@pytest.fixture
def client(tmp_path, monkeypatch):
    """TestClient con sesión iniciada, para probar las rutas por HTTP: así se detecta un
    router que no pasa un argumento nuevo a repo."""
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test.db"))
    with TestClient(main.app, base_url="https://testserver") as c:
        c.post("/login", data={"username": "principal", "password": "test-password", "next": "/"})
        yield c


def test_marcar_vista_rapido_on_a_show_does_not_500(client):
    """POST /vista/{tmdb_id}/show no revienta: la ruta pasa el user_id a get_home_card.
    Título manual para no depender de la red; sync_episodes falla (API key de test) y
    la ruta lo tolera."""
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
    """Marcar un episodio visto abre su hilo de debate."""
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
    """El aviso "¿comentas?" sale al marcar desde Continuar viendo en /pendientes."""
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
        conn.execute("UPDATE users SET comments_seen_at = '2000-01-01 00:00:00' WHERE username = 'principal'")
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
    """Un valor no numérico se ignora en vez de dar un 500."""
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
    """Un weekday no numérico se ignora en vez de dar un 500."""
    with db.get_connection() as conn:
        title = repo.ensure_manual_title(conn, "show", "Serie con dia raro", 2020)
        conn.execute("UPDATE titles SET anilist_id = 12345 WHERE id = ?", (title["id"],))
        tmdb_id, tipo = title["tmdb_id"], title["type"]

    r = client.post(
        f"/titulo/{tmdb_id}/{tipo}/dia-emision", data={"weekday": "lunes"}, follow_redirects=False
    )
    assert r.status_code == 303


def test_marcar_temporada_creates_entry_if_missing(client):
    """"Temp. completa" funciona aunque la ficha se haya abierto sin "+ Pendientes"."""
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


def test_catalog_edits_are_admin_only(client):
    """Portada y emisión son del catálogo compartido: una cuenta normal no las toca."""
    with db.get_connection() as conn:
        title = repo.ensure_manual_title(conn, "show", "Serie del catalogo", 2020)
        tmdb_id = title["tmdb_id"]
        conn.execute("UPDATE users SET is_admin = 0")
    assert client.post(f"/titulo/{tmdb_id}/show/desfase-emision", data={"dias": "1"}, follow_redirects=False).status_code == 403
    assert client.post(f"/titulo/{tmdb_id}/show/dia-emision", data={"weekday": "1"}, follow_redirects=False).status_code == 403


def test_list_detail_remove_item_and_cross_list_duel(client):
    with db.get_connection() as conn:
        uid = conn.execute("SELECT id FROM users ORDER BY id LIMIT 1").fetchone()["id"]
        lista = repo.create_list(conn, "Romance", uid)
        otra = repo.create_list(conn, "Otra", uid)
        a = repo.ensure_entry(conn, repo.ensure_manual_title(conn, "show", "Uno", 2020)["tmdb_id"], "show", uid)
        b = repo.ensure_entry(conn, repo.ensure_manual_title(conn, "show", "Dos", 2020)["tmdb_id"], "show", uid)
        repo.add_entry_to_list(conn, lista["id"], a["id"])
        repo.add_entry_to_list(conn, otra["id"], b["id"])
        item_a = conn.execute("SELECT id FROM list_items WHERE list_id = ?", (lista["id"],)).fetchone()["id"]
        item_b = conn.execute("SELECT id FROM list_items WHERE list_id = ?", (otra["id"],)).fetchone()["id"]

    r = client.get(f"/lista/{lista['id']}")
    assert r.status_code == 200 and f"/lista/{lista['id']}/quitar/{item_a}" in r.text

    client.post(f"/lista/{lista['id']}/duelo", data={"a_id": item_a, "b_id": item_b, "resultado": "1"})
    with db.get_connection() as conn:
        assert conn.execute("SELECT count(*) FROM duels").fetchone()[0] == 0

    assert client.post(f"/lista/{lista['id']}/quitar/{item_a}", follow_redirects=False).status_code == 303
    r = client.get(f"/lista/{lista['id']}")
    assert r.status_code == 200 and "Vacía todavía" in r.text
    assert client.get("/waifus").status_code == 200


def test_season_rating_is_saved_without_prior_entry(client):
    with db.get_connection() as conn:
        title = repo.ensure_manual_title(conn, "show", "Sin entry", 2020)
    r = client.post(f"/titulo/{title['tmdb_id']}/show/temporada/1/puntuar", data={"rating": "8"}, follow_redirects=False)
    assert r.status_code == 303
    with db.get_connection() as conn:
        assert conn.execute("SELECT count(*) FROM season_ratings").fetchone()[0] == 1
    assert client.get("/titulo/999999999/show/temporada/1/puntuar").status_code == 404


def test_first_episode_refreshes_the_detail_header(client):
    """Marcar el primer episodio crea o promociona la entry: la cabecera de la ficha
    deja de ofrecer "+ Pendientes" sin recargar."""
    with db.get_connection() as conn:
        title = repo.ensure_manual_title(conn, "show", "Cabecera", 2020)
        conn.execute(
            "INSERT INTO episodes (title_id, season_number, episode_number, air_date) VALUES (?, 1, 1, '2020-01-01')",
            (title["id"],),
        )
        ep = conn.execute("SELECT id FROM episodes WHERE title_id = ?", (title["id"],)).fetchone()

    detail = client.get(f"/titulo/{title['tmdb_id']}/show")
    assert detail.status_code == 200 and 'id="action-bar"' in detail.text

    r = client.post(f"/episodio/{ep['id']}/toggle")
    assert 'id="action-bar" hx-swap-oob="true"' in r.text and "act-add" not in r.text

    r = client.post(f"/episodio/{ep['id']}/toggle")
    assert 'id="action-bar" hx-swap-oob="true"' in r.text


def test_season_rated_show_removes_ratings_per_season(client):
    with db.get_connection() as conn:
        uid = conn.execute("SELECT id FROM users ORDER BY id LIMIT 1").fetchone()["id"]
        title = repo.ensure_manual_title(conn, "show", "Por temporadas", 2020)
        conn.execute("INSERT INTO entries (title_id, user_id, status) VALUES (?, ?, 'watched')", (title["id"], uid))
        entry = conn.execute("SELECT id FROM entries WHERE title_id = ?", (title["id"],)).fetchone()
        repo.set_season_rating(conn, entry["id"], 1, 9.0, "la mejor")
        repo.set_season_rating(conn, entry["id"], 2, 6.0, "floja")
    base = f"/titulo/{title['tmdb_id']}/show"

    detail = client.get(base)
    assert "/quitar-nota" not in detail.text.split("Zona destructiva")[1].split("</div>")[0]
    client.post(f"/entrada/{entry['id']}/quitar-nota")
    with db.get_connection() as conn:
        assert conn.execute("SELECT rating FROM entries WHERE id = ?", (entry["id"],)).fetchone()[0] == 7.5

    assert f"{base}/temporada/2/quitar-nota" in client.get(f"{base}/temporada/2/puntuar").text
    client.post(f"{base}/temporada/2/quitar-nota")
    with db.get_connection() as conn:
        assert conn.execute("SELECT rating FROM entries WHERE id = ?", (entry["id"],)).fetchone()[0] == 9.0
        assert conn.execute("SELECT comment FROM season_ratings WHERE entry_id = ?", (entry["id"],)).fetchone()[0] == "la mejor"
    client.post(f"{base}/temporada/1/quitar-nota")
    with db.get_connection() as conn:
        assert conn.execute("SELECT rating FROM entries WHERE id = ?", (entry["id"],)).fetchone()[0] is None
