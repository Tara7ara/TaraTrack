import os

import pytest

os.environ.setdefault("TMDB_API_KEY", "test")
# init_db() bootstrapea la primera cuenta (admin) a partir de estas dos variables
# (db._migrate_multiuser, 2026-09-17) - sin TARATRACK_PASSWORD ningun test tendria
# usuario contra el que probar nada que dependa de user_id.
os.environ.setdefault("TARATRACK_PASSWORD", "test-password")
os.environ.setdefault("TARATRACK_ADMIN_USERNAME", "tara")

from app import db


@pytest.fixture
def conn(tmp_path, monkeypatch):
    """BBDD sqlite real en un fichero temporal por test - init_db() completo (schema +
    migraciones + indices), asi los tests corren contra el mismo esquema que produccion."""
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test.db"))
    db.init_db()
    with db.get_connection() as c:
        yield c


@pytest.fixture
def user_id(conn):
    """El id de la cuenta admin creada por init_db() al arrancar (ver arriba) - para
    los tests que necesitan pasar un user_id explicito a repo.ensure_entry/create_list/
    reject_recommendation/etc."""
    return conn.execute("SELECT id FROM users ORDER BY id LIMIT 1").fetchone()["id"]
