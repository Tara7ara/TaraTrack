import asyncio
import io
import os
import threading
import time

os.environ.setdefault("TARATRACK_SECRET_KEY", "test-secret-para-tests")
os.environ.setdefault("TARATRACK_PASSWORD", "test-password")
os.environ.setdefault("TARATRACK_ADMIN_USERNAME", "principal")

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import db, main, matching, repo, tmdb, web
from app.repo import characters


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test.db"))
    with TestClient(main.app, base_url="https://testserver") as c:
        c.post("/login", data={"username": "principal", "password": "test-password", "next": "/"})
        yield c


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (4, 4), "orange").save(buf, "PNG")
    return buf.getvalue()


def _resolves_to(monkeypatch, *ips):
    monkeypatch.setattr(tmdb.socket, "getaddrinfo", lambda host, *a, **k: [(2, 1, 6, "", (ip, 0)) for ip in ips])


# Descarga de imágenes (SSRF)

@pytest.mark.parametrize("ips", [("172.16.5.10",), ("127.0.0.1",), ("10.0.0.5",), ("169.254.169.254",),
                                 ("172.17.0.1",), ("::1",), ("::ffff:127.0.0.1",), ("93.184.216.34", "10.0.0.1")])
def test_download_poster_rechaza_ips_no_publicas(monkeypatch, tmp_path, ips):
    _resolves_to(monkeypatch, *ips)
    monkeypatch.setattr(tmdb, "_fetch_capped", lambda *a: pytest.fail("no debería conectarse"))
    with pytest.raises(tmdb.UnsafeImageURL):
        tmdb.download_poster("http://interno.example/api", str(tmp_path / "x.jpg"))
    assert not (tmp_path / "x.jpg").exists()


@pytest.mark.parametrize("url", ["file:///etc/passwd", "gopher://x/", "http://user:pw@cdn.example/x.jpg", "//x/y"])
def test_download_poster_rechaza_esquemas_raros(tmp_path, url):
    with pytest.raises(tmdb.UnsafeImageURL):
        tmdb.download_poster(url, str(tmp_path / "x.jpg"))


def test_download_poster_conecta_a_la_ip_comprobada(monkeypatch, tmp_path):
    _resolves_to(monkeypatch, "93.184.216.34")
    calls = []
    monkeypatch.setattr(tmdb, "_fetch_capped", lambda url, headers, ext: calls.append((url, headers, ext)) or _png())
    assert tmdb.download_poster("https://cdn.example/a/b.png?x=1", str(tmp_path / "x.jpg"))
    url, headers, ext = calls[0]
    assert url == "https://93.184.216.34/a/b.png?x=1"
    assert headers == {"Host": "cdn.example"} and ext == {"sni_hostname": "cdn.example"}
    assert (tmp_path / "x.jpg").read_bytes() == _png()


def test_download_poster_rechaza_lo_que_no_es_imagen(monkeypatch, tmp_path):
    _resolves_to(monkeypatch, "93.184.216.34")
    monkeypatch.setattr(tmdb, "_fetch_capped", lambda *a: bytearray(b'{"cpu": 12, "mem": 80}'))
    with pytest.raises(tmdb.UnsafeImageURL):
        tmdb.download_poster("https://cdn.example/x.jpg", str(tmp_path / "x.jpg"))
    assert not os.listdir(tmp_path)


def test_download_poster_respeta_la_lista_de_hosts(monkeypatch, tmp_path):
    monkeypatch.setattr(tmdb.socket, "getaddrinfo", lambda *a, **k: pytest.fail("no debería resolver"))
    with pytest.raises(tmdb.UnsafeImageURL):
        tmdb.download_poster("https://evil.example/x.jpg", str(tmp_path / "x.jpg"),
                             allowed_hosts=tmdb.CHARACTER_IMAGE_HOSTS)


def test_personaje_manual_no_pisa_un_retrato_existente(conn, user_id, tmp_path, monkeypatch):
    monkeypatch.setattr(characters, "PROFILES_DIR", str(tmp_path))
    (tmp_path / "al_5.jpg").write_bytes(b"original")
    calls = []
    monkeypatch.setattr(tmdb, "download_poster", lambda *a, **k: calls.append(a) or True)
    title = repo.ensure_manual_title(conn, "show", "Serie", 2020)
    repo.add_character_manual(conn, title, 5, "Marin", "https://s4.anilist.co/otra.png")
    assert not calls and (tmp_path / "al_5.jpg").read_bytes() == b"original"
    row = conn.execute("SELECT profile_path FROM characters WHERE tmdb_person_id = 5").fetchone()
    assert row["profile_path"] == "/static/profiles/al_5.jpg"


def test_personaje_manual_solo_descarga_de_los_cdn_conocidos(conn, user_id, tmp_path, monkeypatch):
    monkeypatch.setattr(characters, "PROFILES_DIR", str(tmp_path))
    monkeypatch.setattr(tmdb.socket, "getaddrinfo", lambda *a, **k: pytest.fail("no debería resolver"))
    title = repo.ensure_manual_title(conn, "show", "Serie", 2020)
    repo.add_character_manual(conn, title, 6, "X", "http://10.1.2.3:8080/api/status")
    assert conn.execute("SELECT profile_path FROM characters WHERE tmdb_person_id = 6").fetchone()[0] is None


# Tipo de título y escapado de card() (XSS)

def test_tipo_de_titulo_no_valido_se_rechaza(client):
    r = client.post('/pendiente/1/movie"><img src=x onerror=alert(1)>')
    assert r.status_code == 422
    assert client.get("/titulo/1/tv").status_code == 422
    with db.get_connection() as conn:
        with pytest.raises(ValueError):
            repo.ensure_title(conn, 1, 'movie"><b>')
        assert conn.execute("SELECT count(*) FROM titles").fetchone()[0] == 0


def test_card_escapa_lo_que_no_es_html_de_la_plantilla():
    tpl = web.templates.env.from_string(
        '{% from "partials/ui.html" import card %}'
        "{{ card('', 't', l1=studio, l2='Nota <b>'|safe ~ nota ~ '</b>'|safe, corner=corner, has_acts=false) }}"
    )
    html = tpl.render(studio='<img src=x onerror=alert(1)>', nota='<script>', corner="<i>")
    assert "<img" not in html and "&lt;img src=x" in html
    assert "Nota <b>&lt;script&gt;</b>" in html
    assert "<i>" not in html


# Regex con retroceso cuadrático (ReDoS)

def test_heuristicas_de_titulo_son_lineales():
    start = time.perf_counter()
    matching._strip_season_suffix("a" + " " * 200_000 + "b")
    list(characters._title_variants("(" * 200_000))
    assert time.perf_counter() - start < 1
    assert matching._strip_season_suffix("Blue Box Season 2") == "Blue Box"
    assert list(characters._title_variants("劇場版 Foo (Bar)")) == ["劇場版 Foo (Bar)", "Foo", "Bar"]


def test_busqueda_y_alta_manual_tienen_tope_de_longitud(client):
    assert client.get("/buscar/resultados", params={"q": "a" * 5000}).status_code == 422
    assert client.post("/alta-manual", data={"title": "(" * 5000}).status_code == 422


# Login

def test_login_de_usuario_inexistente_hace_el_mismo_trabajo(client, monkeypatch):
    calls = []
    monkeypatch.setattr(repo, "burn_password_check", lambda pw: calls.append(pw))
    main._login_failures_by_user.clear()
    main._failures_by_ip.clear()
    r = client.post("/login", data={"username": "nadie", "password": "x", "next": "/"})
    assert r.status_code == 401 and calls == ["x"]


def test_freno_de_login_aguanta_peticiones_simultaneas(monkeypatch):
    main._login_failures_by_user.clear()
    main._failures_by_ip.clear()
    barrier = threading.Barrier(40)
    granted = []

    def attempt():
        barrier.wait()
        if main._reserve_attempt("principal", "1.2.3.4"):
            granted.append(1)

    threads = [threading.Thread(target=attempt) for _ in range(40)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(granted) == main.LOGIN_MAX_ATTEMPTS
    main._login_failures_by_user.clear()
    main._failures_by_ip.clear()


# /static privado

def _raw_get(path: str) -> int:
    """GET sin pasar por httpx, que normaliza los "..": así llega tal cual a la app."""
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(msg):
        sent.append(msg)

    scope = {"type": "http", "method": "GET", "path": path, "raw_path": path.encode(), "query_string": b"",
             "headers": [(b"host", b"testserver")], "scheme": "https", "server": ("testserver", 443),
             "client": ("1.2.3.4", 1), "http_version": "1.1", "root_path": ""}
    asyncio.run(main.app(scope, receive, send))
    return next(m["status"] for m in sent if m["type"] == "http.response.start")


def test_static_privado_pide_sesion(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test.db"))
    with TestClient(main.app, base_url="https://testserver") as anon:
        r = anon.get("/static/avatars/1.jpg", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/login")
        assert r.headers.get("cache-control") == "no-store"
        assert anon.get("/static/profiles/al_1.jpg", follow_redirects=False).status_code == 303
        css = anon.get("/static/css/app.css")
        assert css.status_code == 200 and "public" in css.headers["cache-control"]
    assert _raw_get("/static/css/../manifest.webmanifest") == 303


# Integridad entre cuentas

def test_quitar_item_de_mi_lista_no_toca_duelos_ajenos(conn, user_id):
    other = repo.create_user(conn, "amigo", "unaclave123")
    title = repo.ensure_manual_title(conn, "show", "Serie", 2020)
    mine = repo.ensure_entry(conn, title["tmdb_id"], "show", user_id)
    theirs = repo.ensure_entry(conn, title["tmdb_id"], "show", other["id"])
    my_list = repo.create_list(conn, "Mía", user_id)["id"]
    their_list = repo.create_list(conn, "Suya", other["id"])["id"]
    conn.execute("INSERT INTO list_items (list_id, entry_id) VALUES (?, ?)", (my_list, mine["id"]))
    their_item = conn.execute("INSERT INTO list_items (list_id, entry_id) VALUES (?, ?)",
                              (their_list, theirs["id"])).lastrowid
    conn.execute("INSERT INTO duels (table_name, winner_id, loser_id) VALUES ('list_items', ?, 999)", (their_item,))
    repo.remove_list_item(conn, my_list, their_item)
    assert conn.execute("SELECT count(*) FROM duels").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM list_items WHERE id = ?", (their_item,)).fetchone()[0] == 1


def test_enlaces_del_calendario_son_de_cada_cuenta(conn, user_id):
    other = repo.create_user(conn, "amigo", "unaclave123")
    a = repo.ensure_manual_title(conn, "show", "Serie A", 2020)
    b = repo.ensure_manual_title(conn, "show", "Serie B", 2020)
    repo.ensure_entry(conn, a["tmdb_id"], "show", user_id)
    repo.link_calendar_card(conn, 777, a["tmdb_id"], "show", user_id)
    # La otra cuenta apunta la misma tarjeta a otro título: a mí no me cambia nada.
    repo.link_calendar_card(conn, 777, b["tmdb_id"], "show", other["id"])
    card = [{"anilist_id": 777, "title": "Algo que no casa por nombre"}]
    assert repo.library_status_for_cards(conn, user_id, card) == {777: "pending"}
    assert repo.library_status_for_cards(conn, other["id"], card) == {}


def test_migracion_copia_los_enlaces_a_quien_tiene_el_titulo(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "m.db"))
    db.init_db()
    with db.get_connection() as conn:
        uid = conn.execute("SELECT id FROM users LIMIT 1").fetchone()[0]
        other = repo.create_user(conn, "amigo", "unaclave123")
        t = repo.ensure_manual_title(conn, "show", "Serie", 2020)
        repo.ensure_entry(conn, t["tmdb_id"], "show", uid)
        conn.execute("INSERT INTO calendar_links (anilist_id, title_id) VALUES (42, ?)", (t["id"],))
        conn.execute("DELETE FROM app_settings WHERE key = 'calendar_links_per_user'")
    db.init_db()
    with db.get_connection() as conn:
        rows = conn.execute("SELECT user_id, anilist_id FROM user_calendar_links").fetchall()
        assert [tuple(r) for r in rows] == [(uid, 42)] and other["id"] != uid


def test_admin_inicial_exige_contrasena_larga(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "corta.db"))
    monkeypatch.setenv("TARATRACK_PASSWORD", "corta")
    with pytest.raises(RuntimeError):
        db.init_db()


def test_contrasena_corta_no_afecta_si_el_admin_ya_existe(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "ya.db"))
    db.init_db()
    monkeypatch.setenv("TARATRACK_PASSWORD", "corta")
    db.init_db()
