import os

os.environ.setdefault("TARATRACK_SECRET_KEY", "test-secret-para-tests")
os.environ.setdefault("TARATRACK_PASSWORD", "test-password")
os.environ.setdefault("TARATRACK_ADMIN_USERNAME", "principal")

import pytest
from fastapi.testclient import TestClient

from app import db, main, repo

USERNAME = "principal"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test.db"))
    main._login_failures_by_user.clear()
    main._failures_by_ip.clear()
    with TestClient(main.app, base_url="https://testserver") as c:
        yield c
    main._login_failures_by_user.clear()
    main._failures_by_ip.clear()


def test_login_wrong_password_rejected(client):
    r = client.post("/login", data={"username": USERNAME, "password": "mala", "next": "/"})
    assert r.status_code == 401
    assert "taratrack_auth" not in client.cookies


def test_login_unknown_username_rejected(client):
    r = client.post("/login", data={"username": "no-existo", "password": "test-password", "next": "/"})
    assert r.status_code == 401
    assert "taratrack_auth" not in client.cookies


def test_login_correct_password_sets_cookie_and_redirects(client):
    r = client.post(
        "/login", data={"username": USERNAME, "password": "test-password", "next": "/pendientes"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/pendientes"
    assert "taratrack_auth" in r.cookies


def test_unauthenticated_request_redirects_to_login(client):
    r = client.get("/pendientes", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/login")


def test_open_redirect_next_is_rejected():
    """'next' solo puede ser una ruta interna: ?next=https://evil.com no redirige."""
    from app.main import _safe_next

    assert _safe_next("https://evil.com") == "/"
    assert _safe_next("//evil.com") == "/"
    assert _safe_next("/pendientes") == "/pendientes"
    assert _safe_next("") == "/"


def test_login_locks_out_after_max_failed_attempts(client):
    """5 fallos en la ventana bloquean incluso la contraseña correcta."""
    for _ in range(main.LOGIN_MAX_ATTEMPTS):
        r = client.post("/login", data={"username": USERNAME, "password": "mala", "next": "/"})
        assert r.status_code == 401

    r = client.post("/login", data={"username": USERNAME, "password": "test-password", "next": "/"})
    assert r.status_code == 429
    assert "taratrack_auth" not in client.cookies


def test_login_rate_limit_is_per_username(client):
    """Los fallos contra un usuario no bloquean el login de otro."""
    for _ in range(main.LOGIN_MAX_ATTEMPTS):
        client.post("/login", data={"username": "otra-cuenta", "password": "mala", "next": "/"})

    r = client.post("/login", data={"username": USERNAME, "password": "test-password", "next": "/"})
    assert r.status_code in (200, 303)
    assert "taratrack_auth" in client.cookies


def _invite(username="hermana"):
    with db.get_connection() as conn:
        _, code = repo.create_invited_user(conn, username)
    return code


def _activar(client, username, code, password="una-pass-cualquiera", password2=None):
    return client.post(
        "/activar",
        data={"username": username, "code": code, "password": password, "password2": password2 or password},
        follow_redirects=False,
    )


def test_registro_abierto_ya_no_existe(client):
    r = client.get("/registro", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/activar"
    r = client.post("/registro", data={"username": "x", "password": "una-pass-larga"}, follow_redirects=False)
    assert r.status_code in (303, 405)
    with db.get_connection() as conn:
        assert repo.get_user_by_username(conn, "x") is None


def test_activar_con_codigo_pone_password_y_entra(client):
    code = _invite()
    r = _activar(client, "hermana", code.lower().replace("-", " "))  # tolera minusculas y espacios
    assert r.status_code == 303 and "taratrack_auth" in r.cookies
    client.cookies.clear()
    r = client.post("/login", data={"username": "hermana", "password": "una-pass-cualquiera", "next": "/"}, follow_redirects=False)
    assert r.status_code == 303 and "taratrack_auth" in r.cookies


def test_codigo_solo_sirve_una_vez(client):
    code = _invite()
    assert _activar(client, "hermana", code).status_code == 303
    client.cookies.clear()
    r = _activar(client, "hermana", code, password="otra-pass-distinta")
    assert r.status_code == 400 and "taratrack_auth" not in r.cookies


def test_cuenta_sin_activar_no_puede_entrar_por_login(client):
    _invite()
    with db.get_connection() as conn:
        u = repo.get_user_by_username(conn, "hermana")
        assert u["invite_code_hash"] is not None
    r = client.post("/login", data={"username": "hermana", "password": "", "next": "/"})
    assert r.status_code in (401, 422)


def test_activar_rechaza_codigo_malo_password_corta_y_distinta(client):
    code = _invite()
    assert _activar(client, "hermana", "AAAAA-AAAAA").status_code == 400
    assert _activar(client, "hermana", code, password="corta").status_code == 400
    assert _activar(client, "hermana", code, password="una-pass-larga", password2="otra-distinta").status_code == 400
    assert _activar(client, "no-existe", code).status_code == 400
    # el codigo bueno sigue valiendo tras esos fallos
    assert _activar(client, "hermana", code).status_code == 303


def test_codigo_caducado(client):
    code = _invite()
    with db.get_connection() as conn:
        conn.execute("UPDATE users SET invite_expires_at = '2000-01-01T00:00:00+00:00' WHERE username = 'hermana'")
    r = _activar(client, "hermana", code)
    assert r.status_code == 400 and "caducado" in r.text


def test_activar_se_bloquea_tras_muchos_fallos(client):
    code = _invite()
    for _ in range(main.LOGIN_MAX_ATTEMPTS):
        _activar(client, "hermana", "AAAAA-AAAAA")
    assert _activar(client, "hermana", code).status_code == 429


def test_login_se_bloquea_por_ip_probando_muchos_usuarios(client):
    for i in range(main.IP_MAX_FAILURES):
        client.post("/login", data={"username": f"u{i}", "password": "mala", "next": "/"})
    r = client.post("/login", data={"username": USERNAME, "password": "test-password", "next": "/"})
    assert r.status_code == 429


def test_cuenta_invitada_arranca_vacia(client):
    code = _invite("amigo")
    _activar(client, "amigo", code)
    r = client.get("/pendientes")
    assert r.status_code == 200


def test_missing_secret_key_fails_fast(monkeypatch):
    """Sin TARATRACK_SECRET_KEY, el arranque falla en vez de usar un valor por defecto."""
    monkeypatch.delenv("TARATRACK_SECRET_KEY", raising=False)
    with pytest.raises(RuntimeError):
        main._auth_serializer()
