from app import db, repo


def _table_columns(conn, table):
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


EXPECTED_ENTRIES_COLUMNS = {
    "id", "title_id", "status", "rating", "comment", "added_at", "watched_at",
    "cat_historia", "cat_animacion", "cat_personajes", "cat_musica", "cat_disfrute",
    "elo", "predicted_score", "is_habit", "auto_watch", "rd", "rewatch_started_at",
}
EXPECTED_SEASON_RATINGS_COLUMNS = {
    "id", "entry_id", "season_number", "rating", "comment",
    "cat_historia", "cat_animacion", "cat_personajes", "cat_musica", "cat_disfrute", "rated_at",
}


def test_fresh_db_has_every_migrated_column(tmp_path, monkeypatch):
    """_migrate_rating_scale_0_10 reconstruye entries/season_ratings con su propia
    lista de columnas: si una columna nueva de MIGRATIONS no se replica ahí, una
    instalación nueva la pierde. Este test lo detecta."""
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "fresh.db"))
    db.init_db()
    with db.get_connection() as conn:
        assert EXPECTED_ENTRIES_COLUMNS <= _table_columns(conn, "entries")
        assert EXPECTED_SEASON_RATINGS_COLUMNS <= _table_columns(conn, "season_ratings")

        flag = conn.execute("SELECT value FROM app_settings WHERE key = 'rating_scale_0_10'").fetchone()
        assert flag["value"] == "1"


def test_init_db_is_idempotent(tmp_path, monkeypatch):
    """Reiniciar el contenedor (init_db() se llama en cada arranque, on_startup)
    no debe romper nada ni re-ejecutar la reconstruccion destructiva de
    entries/season_ratings una segunda vez sobre datos ya reales."""
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "fresh.db"))
    db.init_db()
    with db.get_connection() as conn:
        title = repo.ensure_manual_title(conn, "show", "Test Show", 2020)
        conn.execute(
            "INSERT INTO entries (title_id, status, rating) VALUES (?, 'watched', 8.5)", (title["id"],)
        )

    db.init_db()  # segunda vez, no deberia petar ni perder la fila de arriba

    with db.get_connection() as conn:
        row = conn.execute("SELECT rating FROM entries WHERE title_id = ?", (title["id"],)).fetchone()
        assert row["rating"] == 8.5
