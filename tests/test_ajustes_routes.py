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


def test_crear_usuario_con_nombre_invalido_no_revienta(client):
    """Un nombre invalido (reservado) avisa con usuario_error en vez de un 500."""
    r = client.post("/ajustes/usuarios", data={"username": "admin"}, follow_redirects=False)
    assert r.status_code == 303
    assert "usuario_error" in r.headers["location"]


def test_crear_usuario_duplicado_avisa(client):
    r = client.post("/ajustes/usuarios", data={"username": "principal"}, follow_redirects=False)
    assert r.status_code == 303 and "usuario_error" in r.headers["location"]


def test_crear_usuario_valido_enseña_codigo_una_vez(client):
    r = client.post("/ajustes/usuarios", data={"username": "amigo"}, follow_redirects=False)
    assert r.status_code == 200
    assert 'id="invite-code"' in r.text
    assert "sin activar" in r.text
    r2 = client.get("/ajustes")
    assert 'id="invite-code"' not in r2.text


def test_calendario_anime_toggle(client):
    r = client.post("/ajustes/perfil/calendario-anime", data={"mostrar": "1"}, follow_redirects=False)
    assert r.status_code == 303
    r2 = client.get("/calendario")
    assert "/calendario/anual" in r2.text

    client.post("/ajustes/perfil/calendario-anime", data={}, follow_redirects=False)
    r3 = client.get("/calendario")
    assert "/calendario/anual" not in r3.text


def test_non_static_responses_are_not_cached(client):
    """Las páginas van con Cache-Control: no-store, para que el navegador (sobre todo
    Safari/iOS) no enseñe una versión vieja al volver atrás."""
    r = client.get("/pendientes")
    assert r.headers["cache-control"] == "no-store"


def test_admin_genera_codigo_nuevo_y_la_password_vieja_deja_de_valer(client):
    """Recuperar el acceso sin que el admin sepa la contraseña."""
    from app import repo

    with db.get_connection() as conn:
        other_id = repo.create_user(conn, "amigo", "unaclave123")["id"]
    r = client.post(f"/ajustes/usuarios/{other_id}/codigo", follow_redirects=False)
    assert r.status_code == 200 and 'id="invite-code"' in r.text
    with db.get_connection() as conn:
        refreshed = repo.get_user(conn, other_id)
        assert not repo.verify_password("unaclave123", refreshed["password_hash"], refreshed["password_salt"])
        assert refreshed["invite_code_hash"] is not None


def test_admin_no_genera_codigo_para_si_mismo(client):
    with db.get_connection() as conn:
        admin_id = conn.execute("SELECT id FROM users WHERE username = 'principal'").fetchone()["id"]
    r = client.post(f"/ajustes/usuarios/{admin_id}/codigo", follow_redirects=False)
    assert r.status_code == 303 and "usuario_error" in r.headers["location"]


def test_non_admin_no_puede_generar_codigos(client):
    from app import repo

    with db.get_connection() as conn:
        amigo_id = repo.create_user(conn, "amigo", "unaclave123")["id"]
    login_as_amigo = client.post(
        "/login", data={"username": "amigo", "password": "unaclave123", "next": "/"}, follow_redirects=False
    )
    assert "taratrack_auth" in login_as_amigo.cookies
    assert client.post(f"/ajustes/usuarios/{amigo_id}/codigo").status_code == 403
    assert client.post("/ajustes/usuarios", data={"username": "otro"}).status_code == 403


def test_mensaje_de_error_con_tildes_no_rompe_el_redirect(client):
    """El mensaje real de set_admin ('...única cuenta administradora.') lleva
    tildes y espacios - confirma que el redirect no revienta con eso (Starlette
    ya quotea la URL entera, pero se comprueba end-to-end de todas formas)."""
    with db.get_connection() as conn:
        admin_id = conn.execute("SELECT id FROM users WHERE username = 'principal'").fetchone()["id"]
    r = client.post(f"/ajustes/usuarios/{admin_id}/admin", data={"valor": "0"}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/ajustes?usuario_error=")


def test_usuario_de_una_letra_se_puede_crear(client):
    r = client.post("/ajustes/usuarios", data={"username": "z"}, follow_redirects=False)
    assert r.status_code == 200 and 'id="invite-code"' in r.text


def test_bloquear_impide_entrar_y_desbloquear_lo_devuelve(client):
    from app import repo

    with db.get_connection() as conn:
        amigo_id = repo.create_user(conn, "amigo", "unaclave123")["id"]
    r = client.post(f"/ajustes/usuarios/{amigo_id}/bloquear", data={"valor": "1"}, follow_redirects=False)
    assert r.status_code == 303 and "usuario_ok" in r.headers["location"]
    with TestClient(main.app, base_url="https://testserver") as other:
        r = other.post("/login", data={"username": "amigo", "password": "unaclave123", "next": "/"}, follow_redirects=False)
        assert r.status_code == 403 and "taratrack_auth" not in r.cookies
    client.post(f"/ajustes/usuarios/{amigo_id}/bloquear", data={"valor": "0"})
    with TestClient(main.app, base_url="https://testserver") as other:
        r = other.post("/login", data={"username": "amigo", "password": "unaclave123", "next": "/"}, follow_redirects=False)
        assert r.status_code == 303


def test_bloquear_expulsa_una_sesion_ya_abierta(client):
    from app import repo

    with db.get_connection() as conn:
        amigo_id = repo.create_user(conn, "amigo", "unaclave123")["id"]
    with TestClient(main.app, base_url="https://testserver") as other:
        other.post("/login", data={"username": "amigo", "password": "unaclave123", "next": "/"})
        assert other.get("/pendientes", follow_redirects=False).status_code == 200
        with db.get_connection() as conn:
            repo.set_blocked(conn, amigo_id, True)
        r = other.get("/pendientes", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/login")


def test_borrar_cuenta_exige_escribir_el_nombre_y_borra_sus_datos(client):
    from app import repo

    with db.get_connection() as conn:
        amigo_id = repo.create_user(conn, "amigo", "unaclave123")["id"]
        tid = conn.execute("INSERT INTO titles (tmdb_id, type, title) VALUES (999001, 'movie', 'X')").lastrowid
        conn.execute("INSERT INTO entries (title_id, user_id, status) VALUES (?, ?, 'pending')", (tid, amigo_id))
        conn.execute("INSERT INTO app_settings (key, value) VALUES (?, 'manual')", (f"waifus_order_mode:{amigo_id}",))
    r = client.post(f"/ajustes/usuarios/{amigo_id}/borrar", data={"confirmar": "otro"}, follow_redirects=False)
    assert "usuario_error" in r.headers["location"]
    r = client.post(f"/ajustes/usuarios/{amigo_id}/borrar", data={"confirmar": "amigo"}, follow_redirects=False)
    assert "usuario_ok" in r.headers["location"]
    with db.get_connection() as conn:
        assert repo.get_user(conn, amigo_id) is None
        assert conn.execute("SELECT count(*) FROM entries WHERE user_id = ?", (amigo_id,)).fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM lists WHERE user_id = ?", (amigo_id,)).fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM app_settings WHERE key = ?", (f"waifus_order_mode:{amigo_id}",)).fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM titles WHERE id = ?", (tid,)).fetchone()[0] == 1  # catalogo compartido


def test_no_puedes_borrar_ni_bloquear_tu_propia_cuenta(client):
    with db.get_connection() as conn:
        admin_id = conn.execute("SELECT id FROM users WHERE username = 'principal'").fetchone()["id"]
    r = client.post(f"/ajustes/usuarios/{admin_id}/borrar", data={"confirmar": "principal"}, follow_redirects=False)
    assert "usuario_error" in r.headers["location"]
    r = client.post(f"/ajustes/usuarios/{admin_id}/bloquear", data={"valor": "1"}, follow_redirects=False)
    assert "usuario_error" in r.headers["location"]
