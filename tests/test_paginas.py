import os

os.environ.setdefault("TARATRACK_SECRET_KEY", "test-secret-para-tests")
os.environ.setdefault("TARATRACK_PASSWORD", "test-password")
os.environ.setdefault("TARATRACK_ADMIN_USERNAME", "principal")

import pytest
from fastapi.testclient import TestClient

from app import db, main


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test.db"))
    with TestClient(main.app, base_url="https://testserver") as c:
        c.post("/login", data={"username": "principal", "password": "test-password", "next": "/"})
        yield c


def _seed(conn):
    uid = conn.execute("SELECT id FROM users WHERE username = 'principal'").fetchone()["id"]
    show = conn.execute("INSERT INTO titles (tmdb_id, type, title) VALUES (5001, 'show', 'Serie X')").lastrowid
    conn.execute("INSERT INTO entries (title_id, user_id, status) VALUES (?, ?, 'watched')", (show, uid))
    for n in (1, 2, 3):
        ep = conn.execute(
            "INSERT INTO episodes (title_id, season_number, episode_number, name, air_date) VALUES (?, 1, ?, ?, '2026-01-01')",
            (show, n, f"Episodio {n}" if n != 2 else "Un nombre de verdad"),
        ).lastrowid
        conn.execute("INSERT INTO episode_watches (episode_id, user_id, watched_at) VALUES (?, ?, ?)",
                     (ep, uid, f"2026-09-20T20:0{n}:00"))
    for i in range(8):
        t = conn.execute("INSERT INTO titles (tmdb_id, type, title, year) VALUES (?, 'movie', ?, 2020)", (6000 + i, f"Peli {i}")).lastrowid
        conn.execute("INSERT INTO entries (title_id, user_id, status) VALUES (?, ?, 'pending')", (t, uid))


def test_historial_agrupa_episodios_de_la_misma_serie_y_dia(client):
    with db.get_connection() as conn:
        _seed(conn)
    r = client.get("/historial")
    assert r.status_code == 200
    assert r.text.count('class="hcard k-ep"') == 1
    assert "T1E1 – E3" in r.text and "3 ep." in r.text


def test_sorprendeme_portada_y_o_quiza(client):
    with db.get_connection() as conn:
        _seed(conn)
    r = client.get("/sorprendeme")
    assert r.status_code == 200 and "O quizá" in r.text
    assert r.text.count('class="surprise-alt"') == 6
    r = client.get("/sorprendeme?ver=6003")
    assert "Peli 3" in r.text
    r = client.get("/sorprendeme?tipo=series")
    assert "No tienes pendientes con este filtro" in r.text


def test_quitar_fondo_no_vuelve_y_el_principal_pasa_al_siguiente(client, monkeypatch, tmp_path):
    import json

    from app import repo, tmdb
    with db.get_connection() as conn:
        tid = conn.execute(
            "INSERT INTO titles (tmdb_id, type, title, backdrop_path, backdrops) VALUES (7001, 'show', 'GB', '/a.jpg', ?)",
            (json.dumps(["/a.jpg", "/b.jpg", "/c.jpg"]),),
        ).lastrowid
    r = client.get("/titulo/7001/show/fondos")
    assert [x["path"] for x in r.json()] == ["/a.jpg", "/b.jpg", "/c.jpg"]
    import os

    from app import config
    monkeypatch.setattr(config, "POSTERS_DIR", str(tmp_path / "posters"))
    bdir = os.path.join(config.POSTERS_DIR, "backdrops")
    os.makedirs(bdir, exist_ok=True)
    with open(os.path.join(bdir, "a.jpg"), "wb") as f:
        f.write(b"x")
    assert client.post("/titulo/7001/show/fondos/quitar", data={"path": "/a.jpg"}).json()["ok"]
    assert not os.path.exists(os.path.join(bdir, "a.jpg"))  # también sale del disco
    assert [x["path"] for x in client.get("/titulo/7001/show/fondos").json()] == ["/b.jpg", "/c.jpg"]
    with db.get_connection() as conn:
        assert conn.execute("SELECT backdrop_path FROM titles WHERE id = ?", (tid,)).fetchone()[0] == "/b.jpg"
        # La sincronización con TMDB no repone el fondo quitado como principal.
        monkeypatch.setattr(tmdb, "get_details", lambda *a: {"title": "GB", "overview": "", "backdrop_path": "/a.jpg", "logo_path": None})
        repo.refresh_metadata(conn, conn.execute("SELECT * FROM titles WHERE id = ?", (tid,)).fetchone())
        assert conn.execute("SELECT backdrop_path FROM titles WHERE id = ?", (tid,)).fetchone()[0] == "/b.jpg"


def test_fondo_solo_se_guarda_para_admin_con_el_titulo_visto(client, tmp_path, monkeypatch):
    from app import config, repo, tmdb
    monkeypatch.setattr(config, "POSTERS_DIR", str(tmp_path / "posters"))
    calls = []

    def fake_download(url, dest):
        calls.append(url)
        with open(dest, "wb") as f:
            f.write(b"\xff\xd8\xff fake jpg")
        return True

    monkeypatch.setattr(tmdb, "download_poster", fake_download)
    with db.get_connection() as conn:
        uid = conn.execute("SELECT id FROM users WHERE username = 'principal'").fetchone()["id"]
        vista = conn.execute("INSERT INTO titles (tmdb_id, type, title) VALUES (8001, 'show', 'Vista')").lastrowid
        pend = conn.execute("INSERT INTO titles (tmdb_id, type, title) VALUES (8002, 'show', 'Pendiente')").lastrowid
        conn.execute("INSERT INTO entries (title_id, user_id, status) VALUES (?, ?, 'watched')", (vista, uid))
        conn.execute("INSERT INTO entries (title_id, user_id, status) VALUES (?, ?, 'pending')", (pend, uid))
        repo.create_user(conn, "amigo", "unaclave123")
    # No visto: a TMDB, sin guardar ni cachear la redirección.
    r = client.get("/fondo/pend.jpg?t=8002", follow_redirects=False)
    assert r.status_code == 302 and "image.tmdb.org" in r.headers["location"] and not calls
    assert "immutable" not in r.headers.get("cache-control", "")
    # Visto por la admin: se guarda una vez y luego sale del disco.
    assert client.get("/fondo/abc123.jpg?t=8001").status_code == 200
    assert client.get("/fondo/abc123.jpg?t=8001").status_code == 200
    assert len(calls) == 1
    with TestClient(main.app, base_url="https://testserver") as other:
        other.post("/login", data={"username": "amigo", "password": "unaclave123", "next": "/"})
        # Otra cuenta: lo ya guardado se sirve local; lo nuevo va a TMDB aunque sea el mismo título.
        r = other.get("/fondo/abc123.jpg?t=8001", follow_redirects=False)
        assert r.status_code == 200 and "immutable" in r.headers["cache-control"]
        assert other.get("/fondo/otro.jpg?t=8001", follow_redirects=False).status_code == 302
    assert len(calls) == 1
    assert client.get("/fondo/..%2Fsecreto.jpg").status_code == 404


def test_quitar_fondo_solo_admin(client):
    import json

    from app import repo
    with db.get_connection() as conn:
        conn.execute("INSERT INTO titles (tmdb_id, type, title, backdrop_path, backdrops) VALUES (7002, 'show', 'X', '/a.jpg', ?)",
                     (json.dumps(["/a.jpg", "/b.jpg"]),))
        repo.create_user(conn, "amigo", "unaclave123")
    with TestClient(main.app, base_url="https://testserver") as other:
        other.post("/login", data={"username": "amigo", "password": "unaclave123", "next": "/"})
        assert other.post("/titulo/7002/show/fondos/quitar", data={"path": "/a.jpg"}).status_code == 403
        assert 'data-admin="1"' not in other.get("/titulo/7002/show").text
    assert len(client.get("/titulo/7002/show/fondos").json()) == 2


def test_desde_safari_no_se_guarda_ningun_fondo(client, tmp_path, monkeypatch):
    from app import config, tmdb
    monkeypatch.setattr(config, "POSTERS_DIR", str(tmp_path / "posters"))
    calls = []
    monkeypatch.setattr(tmdb, "download_poster", lambda url, dest: calls.append(url) or open(dest, "wb").write(b"x"))
    with db.get_connection() as conn:
        uid = conn.execute("SELECT id FROM users WHERE username = 'principal'").fetchone()["id"]
        t = conn.execute("INSERT INTO titles (tmdb_id, type, title) VALUES (8101, 'show', 'V')").lastrowid
        conn.execute("INSERT INTO entries (title_id, user_id, status) VALUES (?, ?, 'watched')", (t, uid))
    for ua in ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148",
               "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15"):
        r = client.get("/fondo/safari.jpg?t=8101", headers={"User-Agent": ua}, follow_redirects=False)
        assert r.status_code == 302
    assert not calls
    chrome = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"
    assert client.get("/fondo/safari.jpg?t=8101", headers={"User-Agent": chrome}).status_code == 200 and len(calls) == 1


def test_pestana_anime_solo_con_el_ajuste_activado(client):
    client.post("/ajustes/perfil/calendario-anime", data={}, follow_redirects=False)
    assert "tipo=anime" not in client.get("/pendientes").text
    client.post("/ajustes/perfil/calendario-anime", data={"mostrar": "1"}, follow_redirects=False)
    assert "tipo=anime" in client.get("/pendientes").text


def test_vistas_por_tandas(client):
    with db.get_connection() as conn:
        uid = conn.execute("SELECT id FROM users WHERE username = 'principal'").fetchone()["id"]
        for i in range(75):
            t = conn.execute("INSERT INTO titles (tmdb_id, type, title) VALUES (?, 'movie', ?)", (9000 + i, f"P{i}")).lastrowid
            conn.execute("INSERT INTO entries (title_id, user_id, status, watched_at) VALUES (?, ?, 'watched', '2026-01-01')", (t, uid))
    r = client.get("/vistas")
    assert r.text.count('class="pcard') == 60 and "desde=60" in r.text
    r2 = client.get("/vistas?desde=60&parcial=1")
    assert r2.text.count('class="pcard') == 15 and "desde=" not in r2.text
