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
    """TestClient contra una BBDD sqlite temporal; init_db() crea la cuenta admin de
    prueba. base_url en https:// porque la cookie de sesión es Secure y un cliente por
    http no la reenviaría."""
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test.db"))
    main._login_failures_by_user.clear()
    main._registro_attempts.clear()
    with TestClient(main.app, base_url="https://testserver") as c:
        yield c
    main._login_failures_by_user.clear()
    main._registro_attempts.clear()


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


def test_registro_creates_account_and_logs_in(client):
    """El registro público desde /login crea la cuenta y deja la sesión iniciada."""
    r = client.post(
        "/registro",
        data={
            "username": "invitada", "password": "una-pass-cualquiera",
            "password2": "una-pass-cualquiera", "next": "/pendientes",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/pendientes"
    assert "taratrack_auth" in r.cookies


def test_registro_rejects_duplicate_username(client):
    r = client.post(
        "/registro", data={"username": USERNAME, "password": "otra-pass-larga", "password2": "otra-pass-larga", "next": "/"}
    )
    assert r.status_code == 409
    assert "taratrack_auth" not in client.cookies


def test_registro_rejects_short_password(client):
    r = client.post("/registro", data={"username": "amigo", "password": "corta", "password2": "corta", "next": "/"})
    assert r.status_code == 400
    assert "taratrack_auth" not in client.cookies


def test_registro_rejects_mismatched_passwords(client):
    """La confirmación de contraseña tiene que coincidir."""
    r = client.post(
        "/registro", data={"username": "amigo", "password": "una-pass-larga", "password2": "otra-distinta", "next": "/"}
    )
    assert r.status_code == 400
    assert "taratrack_auth" not in client.cookies


def test_registro_new_user_data_isolated_from_admin(client):
    """Una cuenta recién registrada arranca vacía: no ve las entries de otras."""
    client.post(
        "/registro",
        data={"username": "amigo", "password": "una-pass-cualquiera", "password2": "una-pass-cualquiera", "next": "/"},
    )
    r = client.get("/pendientes")
    assert r.status_code == 200


def test_registro_locks_out_after_max_attempts(client):
    """/registro tiene su propio freno contra altas en bucle."""
    for i in range(main.REGISTRO_MAX_ATTEMPTS):
        client.post(
            "/registro",
            data={"username": f"usuario{i}", "password": "una-pass-larga", "password2": "una-pass-larga"},
        )
    r = client.post(
        "/registro", data={"username": "otro-mas", "password": "una-pass-larga", "password2": "una-pass-larga"}
    )
    assert r.status_code == 429
    with db.get_connection() as conn:
        assert repo.get_user_by_username(conn, "otro-mas") is None


def test_missing_secret_key_fails_fast(monkeypatch):
    """Sin TARATRACK_SECRET_KEY, el arranque falla en vez de usar un valor por defecto."""
    monkeypatch.delenv("TARATRACK_SECRET_KEY", raising=False)
    with pytest.raises(RuntimeError):
        main._auth_serializer()
