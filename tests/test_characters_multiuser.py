from app import repo


def _title_with_character_and_entry(conn, title_name, user_id, char_name="Kurisu"):
    title = repo.ensure_manual_title(conn, "show", title_name, 2020)
    conn.execute(
        "INSERT INTO entries (title_id, user_id, status) VALUES (?, ?, 'watched')", (title["id"], user_id)
    )
    conn.execute(
        "INSERT OR IGNORE INTO characters (title_id, tmdb_person_id, name) VALUES (?, 1, ?)",
        (title["id"], char_name),
    )
    return title


def test_search_characters_local_only_sees_your_own_library(conn, user_id):
    """Buscar un personaje de un título que solo tiene otro usuario no devuelve su
    entry_id."""
    other = repo.create_user(conn, "amigo", "unaclave123")
    _title_with_character_and_entry(conn, "Solo del amigo", other["id"], "Kurisu")

    mine = repo.search_characters_local(conn, "Kurisu", user_id)
    theirs = repo.search_characters_local(conn, "Kurisu", other["id"])

    assert mine == []
    assert len(theirs) == 1


def test_search_characters_local_returns_your_own_entry_id(conn, user_id):
    other = repo.create_user(conn, "amigo", "unaclave123")
    title = _title_with_character_and_entry(conn, "Compartida", user_id, "Kurisu")
    _title_with_character_and_entry(conn, "Compartida", other["id"], "Kurisu")

    mine = repo.search_characters_local(conn, "Kurisu", user_id)

    my_entry_id = conn.execute(
        "SELECT id FROM entries WHERE title_id = ? AND user_id = ?", (title["id"], user_id)
    ).fetchone()["id"]
    assert len(mine) == 1
    assert mine[0]["entry_id"] == my_entry_id


def test_match_library_title_only_matches_your_own_library(conn, user_id):
    other = repo.create_user(conn, "amigo", "unaclave123")
    repo.ensure_manual_title(conn, "show", "Solo del amigo", 2020)
    title = conn.execute("SELECT * FROM titles WHERE title = 'Solo del amigo'").fetchone()
    conn.execute(
        "INSERT INTO entries (title_id, user_id, status) VALUES (?, ?, 'watched')", (title["id"], other["id"])
    )

    assert repo.match_library_title(conn, ["Solo del amigo"], user_id) is None
    assert repo.match_library_title(conn, ["Solo del amigo"], other["id"]) is not None
