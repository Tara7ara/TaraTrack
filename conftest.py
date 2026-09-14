import os

import pytest

os.environ.setdefault("TMDB_API_KEY", "test")

from app import db


@pytest.fixture
def conn(tmp_path, monkeypatch):
    """BBDD sqlite real en un fichero temporal por test - init_db() completo (schema +
    migraciones + indices), asi los tests corren contra el mismo esquema que produccion."""
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test.db"))
    db.init_db()
    with db.get_connection() as c:
        yield c
