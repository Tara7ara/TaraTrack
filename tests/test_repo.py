from app import repo


def test_compute_weighted_rating_ignores_unset_categories():
    """Una categoria en None no debe contar ni en la nota ni en el peso - si contara
    como 0 la media saldria mal por debajo sin que Tara lo pidiera."""
    categories = {"historia": 8, "animacion": None, "personajes": 9, "musica": None, "disfrute": 7}
    expected = round((8 * 1.0 + 9 * 1.0 + 7 * 1.5) / (1.0 + 1.0 + 1.5), 2)
    assert repo.compute_weighted_rating(categories) == expected


def test_compute_weighted_rating_all_unset_returns_none():
    assert repo.compute_weighted_rating({"historia": None, "disfrute": None}) is None


def test_next_unwatched_episode_skips_specials_and_future(conn):
    """El 'T1E6' de la tarjeta de inicio no debe ofrecer especiales (temporada 0) ni
    episodios que aun no han emitido, aunque esten antes en el orden de insercion."""
    title = repo.ensure_manual_title(conn, "show", "Test Show", 2020)
    conn.execute("INSERT INTO entries (title_id, status) VALUES (?, 'pending')", (title["id"],))
    conn.execute(
        "INSERT INTO episodes (title_id, season_number, episode_number, air_date) VALUES (?, 0, 1, '2020-01-01')",
        (title["id"],),
    )
    conn.execute(
        "INSERT INTO episodes (title_id, season_number, episode_number, air_date) VALUES (?, 1, 2, '2099-01-01')",
        (title["id"],),
    )
    conn.execute(
        "INSERT INTO episodes (title_id, season_number, episode_number, air_date) VALUES (?, 1, 1, '2020-01-02')",
        (title["id"],),
    )
    result = repo.next_unwatched_episode(conn, title["id"])
    assert result is not None
    assert (result["season_number"], result["episode_number"]) == (1, 1)


def test_move_in_ranking_swaps_neighbours(conn):
    """_move_in_ranking la comparten listas y waifus - subir el segundo elemento debe
    intercambiarlo con el primero, no reordenar todo lo demas."""
    list_row = repo.create_list(conn, "Test list")
    title1 = repo.ensure_manual_title(conn, "movie", "Uno", 2020)
    title2 = repo.ensure_manual_title(conn, "movie", "Dos", 2021)
    conn.execute("INSERT INTO entries (title_id, status) VALUES (?, 'pending')", (title1["id"],))
    conn.execute("INSERT INTO entries (title_id, status) VALUES (?, 'pending')", (title2["id"],))
    entry1 = conn.execute("SELECT id FROM entries WHERE title_id = ?", (title1["id"],)).fetchone()["id"]
    entry2 = conn.execute("SELECT id FROM entries WHERE title_id = ?", (title2["id"],)).fetchone()["id"]
    repo.add_entry_to_list(conn, list_row["id"], entry1)
    repo.add_entry_to_list(conn, list_row["id"], entry2)
    item1 = conn.execute("SELECT id FROM list_items WHERE entry_id = ?", (entry1,)).fetchone()["id"]
    item2 = conn.execute("SELECT id FROM list_items WHERE entry_id = ?", (entry2,)).fetchone()["id"]

    repo.move_list_item(conn, list_row["id"], item2, "subir")

    ordered = [i["item_id"] for i in repo.list_items_in_list(conn, list_row["id"])]
    assert ordered == [item2, item1]


def test_remove_pending_entry_deletes_orphan_title(conn):
    """El bug real que se quejaba Tara: abrir la ficha de una recomendacion crea
    title+entry; al deshacer el pendiente, el titulo huerfano debe desaparecer tambien
    - si no, list_recommendations lo trata como 'ya conocido' para siempre sin haberlo
    baneado nunca (una recomendacion mirada por curiosidad se quemaba en silencio)."""
    title = repo.ensure_manual_title(conn, "movie", "Curioseada", 2020)
    conn.execute("INSERT INTO entries (title_id, status) VALUES (?, 'pending')", (title["id"],))
    entry_id = conn.execute("SELECT id FROM entries WHERE title_id = ?", (title["id"],)).fetchone()["id"]

    repo.remove_pending_entry(conn, entry_id)

    assert conn.execute("SELECT 1 FROM entries WHERE id = ?", (entry_id,)).fetchone() is None
    assert conn.execute("SELECT 1 FROM titles WHERE id = ?", (title["id"],)).fetchone() is None


def test_start_rewatch_unmarks_episodes_but_keeps_rating(conn):
    """start_rewatch estilo Trakt (2026-08-20): ya NO pone watched_at = NULL en
    episodios (esa fecha vieja no se pierde nunca) - en vez de eso, guarda CUANDO
    empezo la ronda nueva (entries.rewatch_started_at) y next_unwatched_episode trata
    como pendiente lo visto ANTES de esa fecha. Confirma que la fecha del episodio
    sigue intacta, que se guarda la fecha de la ronda, y que no toca nota ni comentario."""
    title = repo.ensure_manual_title(conn, "show", "Rewatch Show", 2020)
    conn.execute(
        """INSERT INTO entries (title_id, status, rating, watched_at)
           VALUES (?, 'watched', 9.0, '2020-01-01T00:00:00Z')""",
        (title["id"],),
    )
    entry_id = conn.execute("SELECT id FROM entries WHERE title_id = ?", (title["id"],)).fetchone()["id"]
    conn.execute(
        """INSERT INTO episodes (title_id, season_number, episode_number, air_date, watched_at)
           VALUES (?, 1, 1, '2020-01-01', '2020-01-01T00:00:00Z')""",
        (title["id"],),
    )

    repo.start_rewatch(conn, entry_id)

    episode = conn.execute("SELECT watched_at FROM episodes WHERE title_id = ?", (title["id"],)).fetchone()
    entry = conn.execute("SELECT rating, rewatch_started_at FROM entries WHERE id = ?", (entry_id,)).fetchone()
    assert episode["watched_at"] == "2020-01-01T00:00:00Z"
    assert entry["rewatch_started_at"] is not None
    next_ep = repo.next_unwatched_episode(conn, title["id"], entry["rewatch_started_at"])
    assert next_ep is not None and next_ep["season_number"] == 1 and next_ep["episode_number"] == 1
    assert entry["rating"] == 9.0
    assert repo.count_plays(conn, entry_id) == 2
