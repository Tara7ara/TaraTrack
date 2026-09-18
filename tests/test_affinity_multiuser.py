from app import repo


def _rated_title(conn, name, anilist_id, genre, user_id, rating):
    title = repo.ensure_manual_title(conn, "movie", name, 2020)
    conn.execute(
        "UPDATE titles SET anilist_id = ?, original_language = 'ja', anilist_genres = ? WHERE id = ?",
        (anilist_id, genre, title["id"]),
    )
    conn.execute(
        "INSERT INTO entries (title_id, user_id, status, rating) VALUES (?, ?, 'watched', ?)",
        (title["id"], user_id, rating),
    )
    return title


def test_taste_profile_is_isolated_per_user(conn, user_id):
    """Multiusuario Fase 3 (2026-09-18): cada usuario recalcula y lee SU PROPIO
    taste_profile - antes era una unica tabla global mezclando el gusto de cualquiera."""
    other = repo.create_user(conn, "otro", "unaclave123")
    _rated_title(conn, "Comedia", 111, "Comedy", user_id, 9.0)
    _rated_title(conn, "Drama", 222, "Drama", user_id, 4.0)
    _rated_title(conn, "Comedia otro", 333, "Comedy", other["id"], 4.0)
    _rated_title(conn, "Drama otro", 444, "Drama", other["id"], 9.0)

    repo.recompute_taste_profile(conn, user_id)
    repo.recompute_taste_profile(conn, other["id"])

    mine = conn.execute(
        "SELECT afinidad FROM taste_profile WHERE user_id = ? AND attr_name = 'Comedy'", (user_id,)
    ).fetchone()
    theirs = conn.execute(
        "SELECT afinidad FROM taste_profile WHERE user_id = ? AND attr_name = 'Comedy'", (other["id"],)
    ).fetchone()
    assert mine is not None and theirs is not None
    assert mine["afinidad"] != theirs["afinidad"]


def test_recompute_only_touches_own_rows(conn, user_id):
    """Recalcular el perfil de un usuario no debe borrar ni tocar el de otro."""
    other = repo.create_user(conn, "otro", "unaclave123")
    _rated_title(conn, "Comedia", 111, "Comedy", user_id, 9.0)
    _rated_title(conn, "Drama", 222, "Drama", user_id, 4.0)
    _rated_title(conn, "Comedia otro", 333, "Comedy", other["id"], 7.0)
    _rated_title(conn, "Drama otro", 444, "Drama", other["id"], 7.0)

    repo.recompute_taste_profile(conn, other["id"])
    other_rows_before = conn.execute(
        "SELECT count(*) FROM taste_profile WHERE user_id = ?", (other["id"],)
    ).fetchone()[0]

    repo.recompute_taste_profile(conn, user_id)

    other_rows_after = conn.execute(
        "SELECT count(*) FROM taste_profile WHERE user_id = ?", (other["id"],)
    ).fetchone()[0]
    assert other_rows_before == other_rows_after > 0


def test_affinity_config_is_namespaced_per_user(conn, user_id):
    other = repo.create_user(conn, "otro", "unaclave123")
    cfg = repo.get_affinity_config(conn, user_id)
    cfg["appetite_weights"]["volumen"] = 0.99
    repo.set_affinity_config(conn, user_id, cfg)

    mine = repo.get_affinity_config(conn, user_id)
    theirs = repo.get_affinity_config(conn, other["id"])
    assert mine["appetite_weights"]["volumen"] == 0.99
    assert theirs["appetite_weights"]["volumen"] != 0.99


def test_waifus_are_isolated_per_user(conn, user_id):
    other = repo.create_user(conn, "otro", "unaclave123")
    title = repo.ensure_manual_title(conn, "show", "Test Show", 2020)
    conn.execute(
        "INSERT INTO entries (title_id, user_id, status) VALUES (?, ?, 'pending')", (title["id"], user_id)
    )
    entry_id = conn.execute(
        "SELECT id FROM entries WHERE title_id = ? AND user_id = ?", (title["id"], user_id)
    ).fetchone()["id"]
    conn.execute("INSERT INTO characters (title_id, tmdb_person_id, name) VALUES (?, 1, 'Char')", (title["id"],))
    char_id = conn.execute("SELECT id FROM characters WHERE title_id = ?", (title["id"],)).fetchone()["id"]
    repo.toggle_favorite_character(conn, entry_id, char_id)

    assert len(repo.list_waifus(conn, user_id)) == 1
    assert len(repo.list_waifus(conn, other["id"])) == 0


def test_duel_pool_is_isolated_per_user(conn, user_id):
    other = repo.create_user(conn, "otro", "unaclave123")
    title = repo.ensure_manual_title(conn, "movie", "Peli", 2020)
    for uid in (user_id, other["id"]):
        conn.execute(
            "INSERT INTO entries (title_id, user_id, status) VALUES (?, ?, 'watched')", (title["id"], uid)
        )
    mine = repo.list_watched_ids(conn, user_id)
    theirs = repo.list_watched_ids(conn, other["id"])
    assert len(mine) == 1 and len(theirs) == 1
    assert mine[0] != theirs[0]
