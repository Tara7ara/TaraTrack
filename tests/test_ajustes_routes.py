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
    """Un alta desde /ajustes con un nombre inválido da un aviso, no un 500."""
    r = client.post(
        "/ajustes/usuarios", data={"username": "admin", "password": "unaclave123"}, follow_redirects=False
    )
    assert r.status_code == 303
    assert "usuario_error" in r.headers["location"]


def test_crear_usuario_con_password_corta_no_revienta(client):
    r = client.post(
        "/ajustes/usuarios", data={"username": "amigo", "password": "corta"}, follow_redirects=False
    )
    assert r.status_code == 303
    assert "usuario_error" in r.headers["location"]


def test_crear_usuario_valido_funciona(client):
    r = client.post(
        "/ajustes/usuarios", data={"username": "amigo", "password": "unaclave123"}, follow_redirects=False
    )
    assert r.status_code == 303
    assert "usuario_creado" in r.headers["location"]


def test_calendario_anime_toggle(client):
    """Se puede forzar a mano si se ve el calendario de temporada."""
    r = client.post("/ajustes/perfil/calendario-anime", data={"mostrar": "1"}, follow_redirects=False)
    assert r.status_code == 303
    r2 = client.get("/calendario")
    assert "Calendario de temporada" in r2.text

    client.post("/ajustes/perfil/calendario-anime", data={}, follow_redirects=False)
    r3 = client.get("/calendario")
    assert "Calendario de temporada" not in r3.text


def test_non_static_responses_are_not_cached(client):
    """Las páginas van con Cache-Control: no-store, para que el navegador (sobre todo
    Safari/iOS) no enseñe una versión vieja al volver atrás."""
    r = client.get("/pendientes")
    assert r.headers["cache-control"] == "no-store"


def test_admin_puede_resetear_password_de_otro(client):
    """Un admin puede restablecer la contraseña de otra cuenta."""
    from app import repo

    with db.get_connection() as conn:
        other = repo.get_user_by_username(conn, "amigo") or repo.create_user(conn, "amigo", "unaclave123")
        other_id = other["id"]

    r = client.post(
        f"/ajustes/usuarios/{other_id}/password", data={"password": "una-pass-recuperada"}, follow_redirects=False
    )
    assert r.status_code == 303

    with db.get_connection() as conn:
        refreshed = repo.get_user(conn, other_id)
        assert repo.verify_password("una-pass-recuperada", refreshed["password_hash"], refreshed["password_salt"])


def test_non_admin_no_puede_resetear_password(client):
    from app import repo

    with db.get_connection() as conn:
        other = repo.create_user(conn, "amigo", "unaclave123")
        amigo_id = other["id"]

    login_as_amigo = client.post(
        "/login", data={"username": "amigo", "password": "unaclave123", "next": "/"}, follow_redirects=False
    )
    assert "taratrack_auth" in login_as_amigo.cookies

    r = client.post(f"/ajustes/usuarios/{amigo_id}/password", data={"password": "otra-cosa-larga"})
    assert r.status_code == 403


def test_mensaje_de_error_con_tildes_no_rompe_el_redirect(client):
    """El mensaje real de set_admin ('...única cuenta administradora.') lleva
    tildes y espacios - confirma que el redirect no revienta con eso (Starlette
    ya quotea la URL entera, pero se comprueba end-to-end de todas formas)."""
    with db.get_connection() as conn:
        admin_id = conn.execute("SELECT id FROM users WHERE username = 'principal'").fetchone()["id"]
    r = client.post(f"/ajustes/usuarios/{admin_id}/admin", data={"valor": "0"}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/ajustes?usuario_error=")
