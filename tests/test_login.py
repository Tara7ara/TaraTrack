import os

os.environ.setdefault("TARATRACK_SECRET_KEY", "test-secret-para-tests")
os.environ.setdefault("TARATRACK_PASSWORD", "test-password")

import pytest
from fastapi.testclient import TestClient

from app import db, main


@pytest.fixture
def client(tmp_path, monkeypatch):
    """TestClient real contra una BBDD sqlite temporal. base_url en https:// a
    proposito (no http://) - la cookie de sesion es Secure=True y un cliente
    hablando por http nunca la reenviaria en peticiones siguientes, igual que un
    navegador real jamas la manda salvo por HTTPS (tara.series fuerza SSL)."""
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test.db"))
    main._login_failures.clear()
    with TestClient(main.app, base_url="https://testserver") as c:
        yield c
    main._login_failures.clear()


def test_login_wrong_password_rejected(client):
    r = client.post("/login", data={"password": "mala", "next": "/"})
    assert r.status_code == 401
    assert "taratrack_auth" not in client.cookies


def test_login_correct_password_sets_cookie_and_redirects(client):
    r = client.post("/login", data={"password": "test-password", "next": "/pendientes"})
    assert r.status_code == 200  # TestClient sigue el 303 -> termina en /pendientes
    assert r.url == "https://testserver/pendientes"
    assert "taratrack_auth" in client.cookies


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
        r = client.post("/login", data={"password": "mala", "next": "/"})
        assert r.status_code == 401

    r = client.post("/login", data={"password": "test-password", "next": "/"})
    assert r.status_code == 429
    assert "taratrack_auth" not in client.cookies


def test_missing_secret_key_fails_fast(monkeypatch):
    """Ronda 2026-08-21: sin TARATRACK_SECRET_KEY, arrancar debe fallar alto y
    claro en vez de caer a un valor por defecto conocido en el codigo."""
    monkeypatch.delenv("TARATRACK_SECRET_KEY", raising=False)
    with pytest.raises(RuntimeError):
        main._auth_serializer()
