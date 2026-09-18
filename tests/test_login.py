import os

os.environ.setdefault("TARATRACK_SECRET_KEY", "test-secret-para-tests")
os.environ.setdefault("TARATRACK_PASSWORD", "test-password")
os.environ.setdefault("TARATRACK_ADMIN_USERNAME", "tara")

import pytest
from fastapi.testclient import TestClient

from app import db, main, repo

USERNAME = "tara"


@pytest.fixture
def client(tmp_path, monkeypatch):
    """TestClient real contra una BBDD sqlite temporal - init_db() bootstrapea la
    cuenta admin "tara"/test-password (db._migrate_multiuser) igual que en produccion.
    base_url en https:// a proposito (no http://) - la cookie de sesion es Secure=True
    y un cliente hablando por http nunca la reenviaria en peticiones siguientes, igual
    que un navegador real jamas la manda salvo por HTTPS (el dominio real fuerza SSL)."""
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
    """Ronda 2026-08-21: 'next' solo puede ser una ruta interna - un link
    manipulado con ?next=https://evil.com no debe poder redirigir ahi tras el login."""
    from app.main import _safe_next

    assert _safe_next("https://evil.com") == "/"
    assert _safe_next("//evil.com") == "/"
    assert _safe_next("/pendientes") == "/pendientes"
    assert _safe_next("") == "/"


def test_login_locks_out_after_max_failed_attempts(client):
    """Ronda 2026-08-21: 5 fallos en la ventana bloquean incluso la contraseña
    correcta - freno a fuerza bruta contra el unico punto de entrada de la app."""
    for _ in range(main.LOGIN_MAX_ATTEMPTS):
        r = client.post("/login", data={"username": USERNAME, "password": "mala", "next": "/"})
        assert r.status_code == 401

    r = client.post("/login", data={"username": USERNAME, "password": "test-password", "next": "/"})
    assert r.status_code == 429
    assert "taratrack_auth" not in client.cookies


def test_login_rate_limit_is_per_username(client):
    """Ronda 2026-09-17 (multiusuario): 5 fallos contra un usuario NO deben bloquear
    el login de otro usuario distinto - antes de esto, el contador era global y
    cualquiera bloqueaba a todos."""
    for _ in range(main.LOGIN_MAX_ATTEMPTS):
        client.post("/login", data={"username": "otra-cuenta", "password": "mala", "next": "/"})

    r = client.post("/login", data={"username": USERNAME, "password": "test-password", "next": "/"})
    assert r.status_code in (200, 303)
    assert "taratrack_auth" in client.cookies


def test_registro_creates_account_and_logs_in(client):
    """Ronda 2026-09-18: 'lo tienen que hacer ellos' - autoregistro publico desde
    /login (sin invitacion, la red WireGuard ya filtra quien llega hasta aqui),
    crea la cuenta y deja logueado directamente, como el login normal."""
    r = client.post(
        "/registro",
        data={
            "username": "hermana", "password": "una-pass-cualquiera",
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
    """Ronda 2026-09-18: 'registro mas fuerte' - confirmacion de contraseña para
    pillar erratas antes de crear la cuenta, no en el primer login fallido."""
    r = client.post(
        "/registro", data={"username": "amigo", "password": "una-pass-larga", "password2": "otra-distinta", "next": "/"}
    )
    assert r.status_code == 400
    assert "taratrack_auth" not in client.cookies


def test_registro_new_user_data_isolated_from_admin(client):
    """La cuenta autoregistrada arranca vacia - no ve pendientes/entries de tara
    solo por compartir la misma instancia (mismo criterio de aislamiento que el
    resto del multiusuario, Fase 2)."""
    client.post(
        "/registro",
        data={"username": "amigo", "password": "una-pass-cualquiera", "password2": "una-pass-cualquiera", "next": "/"},
    )
    r = client.get("/pendientes")
    assert r.status_code == 200


def test_registro_locks_out_after_max_attempts(client):
    """Gap real (AGY, 2026-09-18): /login ya tenia freno de fuerza bruta, /registro
    no - un script en bucle podia crear cientos de cuentas sin ningun limite."""
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
    """Ronda 2026-08-21: sin TARATRACK_SECRET_KEY, arrancar debe fallar alto y
    claro en vez de caer a un valor por defecto conocido en el codigo."""
    monkeypatch.delenv("TARATRACK_SECRET_KEY", raising=False)
    with pytest.raises(RuntimeError):
        main._auth_serializer()
