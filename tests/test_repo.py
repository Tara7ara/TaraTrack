from app import repo


def test_compute_weighted_rating_ignores_unset_categories():
    """Una categoría en None no cuenta ni en la nota ni en el peso (como 0 bajaría la
    media)."""
    categories = {"historia": 8, "animacion": None, "personajes": 9, "musica": None, "disfrute": 7}
    expected = round((8 * 1.0 + 9 * 1.0 + 7 * 1.5) / (1.0 + 1.0 + 1.5), 2)
    assert repo.compute_weighted_rating(categories) == expected


def test_compute_weighted_rating_all_unset_returns_none():
    assert repo.compute_weighted_rating({"historia": None, "disfrute": None}) is None


def test_next_unwatched_episode_skips_specials_and_future(conn, user_id):
    """El 'T1E6' de la tarjeta de inicio no debe ofrecer especiales (temporada 0) ni
    episodios que aun no han emitido, aunque esten antes en el orden de insercion."""
    title = repo.ensure_manual_title(conn, "show", "Test Show", 2020)
    conn.execute("INSERT INTO entries (title_id, user_id, status) VALUES (?, ?, 'pending')", (title["id"], user_id))
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
    result = repo.next_unwatched_episode(conn, title["id"], None, user_id)
    assert result is not None
    assert (result["season_number"], result["episode_number"]) == (1, 1)


def test_move_in_ranking_swaps_neighbours(conn, user_id):
    """_move_in_ranking la comparten listas y waifus - subir el segundo elemento debe
    intercambiarlo con el primero, no reordenar todo lo demas."""
    list_row = repo.create_list(conn, "Test list", user_id)
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
    """Abrir la ficha de una recomendación crea título y entry; al deshacer el
    pendiente, el título huérfano también desaparece. Si no, list_recommendations lo
    trataría como conocido para siempre."""
    title = repo.ensure_manual_title(conn, "movie", "Curioseada", 2020)
    conn.execute("INSERT INTO entries (title_id, status) VALUES (?, 'pending')", (title["id"],))
    entry_id = conn.execute("SELECT id FROM entries WHERE title_id = ?", (title["id"],)).fetchone()["id"]

    repo.remove_pending_entry(conn, entry_id)

    assert conn.execute("SELECT 1 FROM entries WHERE id = ?", (entry_id,)).fetchone() is None
    assert conn.execute("SELECT 1 FROM titles WHERE id = ?", (title["id"],)).fetchone() is None


def test_start_rewatch_unmarks_episodes_but_keeps_rating(conn, user_id):
    """start_rewatch no borra el historial de episode_watches: guarda cuándo empieza la
    ronda y next_unwatched_episode trata como pendiente lo visto antes. Tampoco toca
    nota ni comentario."""
    title = repo.ensure_manual_title(conn, "show", "Rewatch Show", 2020)
    conn.execute(
        """INSERT INTO entries (title_id, user_id, status, rating, watched_at)
           VALUES (?, ?, 'watched', 9.0, '2020-01-01T00:00:00Z')""",
        (title["id"], user_id),
    )
    entry_id = conn.execute(
        "SELECT id FROM entries WHERE title_id = ? AND user_id = ?", (title["id"], user_id)
    ).fetchone()["id"]
    conn.execute(
        "INSERT INTO episodes (title_id, season_number, episode_number, air_date) VALUES (?, 1, 1, '2020-01-01')",
        (title["id"],),
    )
    episode_id = conn.execute("SELECT id FROM episodes WHERE title_id = ?", (title["id"],)).fetchone()["id"]
    repo._log_episode_watch(conn, episode_id, user_id, "2020-01-01T00:00:00Z")

    repo.start_rewatch(conn, entry_id)

    watched_at = conn.execute(
        "SELECT watched_at FROM episode_watches WHERE episode_id = ? AND user_id = ?", (episode_id, user_id)
    ).fetchone()["watched_at"]
    entry = conn.execute("SELECT rating, rewatch_started_at FROM entries WHERE id = ?", (entry_id,)).fetchone()
    assert watched_at == "2020-01-01T00:00:00Z"
    assert entry["rewatch_started_at"] is not None
    next_ep = repo.next_unwatched_episode(conn, title["id"], entry["rewatch_started_at"], user_id)
    assert next_ep is not None and next_ep["season_number"] == 1 and next_ep["episode_number"] == 1
    assert entry["rating"] == 9.0
    assert repo.count_plays(conn, entry_id) == 2


def test_set_season_rating_updates_entry_average(conn, user_id):
    """La nota general de una serie puntuada por temporadas es la media simple de sus
    temporadas, actualizada en cada guardado."""
    title = repo.ensure_manual_title(conn, "show", "Serie por temporadas", 2020)
    conn.execute(
        "INSERT INTO entries (title_id, user_id, status) VALUES (?, ?, 'watched')",
        (title["id"], user_id),
    )
    entry_id = conn.execute("SELECT id FROM entries WHERE title_id = ?", (title["id"],)).fetchone()["id"]

    repo.set_season_rating(conn, entry_id, 1, 8.0, "")
    assert conn.execute("SELECT rating FROM entries WHERE id = ?", (entry_id,)).fetchone()["rating"] == 8.0

    repo.set_season_rating(conn, entry_id, 2, 6.0, "")
    assert conn.execute("SELECT rating FROM entries WHERE id = ?", (entry_id,)).fetchone()["rating"] == 7.0

    # Reeditar una temporada ya puntuada recalcula, no acumula una tercera nota.
    repo.set_season_rating(conn, entry_id, 1, 10.0, "")
    assert conn.execute("SELECT rating FROM entries WHERE id = ?", (entry_id,)).fetchone()["rating"] == 8.0


def test_mark_watched_seeds_season_rating_when_show_ongoing_with_one_season(conn, user_id):
    """Puntuar con el examen general una serie en emisión con una sola temporada
    siembra season_ratings[1], para que vuelva a /puntuar cuando se complete la
    temporada 2."""
    title = repo.ensure_manual_title(conn, "show", "Serie en emision, S1", 2020)
    conn.execute("UPDATE titles SET show_status = 'Returning Series' WHERE id = ?", (title["id"],))
    conn.execute(
        "INSERT INTO episodes (title_id, season_number, episode_number, air_date) VALUES (?, 1, 1, '2020-01-01')",
        (title["id"],),
    )
    conn.execute(
        "INSERT INTO entries (title_id, user_id, status) VALUES (?, ?, 'watched')",
        (title["id"], user_id),
    )
    entry_id = conn.execute("SELECT id FROM entries WHERE title_id = ?", (title["id"],)).fetchone()["id"]

    repo.mark_watched(conn, entry_id, 8.5, "genial")

    season_rating = repo.get_season_rating(conn, entry_id, 1)
    assert season_rating is not None and season_rating["rating"] == 8.5
    assert conn.execute("SELECT rating FROM entries WHERE id = ?", (entry_id,)).fetchone()["rating"] == 8.5

    # Sale la temporada 2: ahora SI debe resurgir en /puntuar para esa temporada.
    conn.execute(
        "INSERT INTO episodes (title_id, season_number, episode_number, air_date) VALUES (?, 2, 1, '2020-06-01')",
        (title["id"],),
    )
    s2e1 = conn.execute(
        "SELECT id FROM episodes WHERE title_id = ? AND season_number = 2", (title["id"],)
    ).fetchone()["id"]
    repo._log_episode_watch(conn, s2e1, user_id, "2020-06-02T00:00:00Z")

    item = repo.next_review_item(conn, user_id, [])
    assert item is not None and item["id"] == entry_id and item["season_number"] == 2


def test_mark_watched_does_not_seed_season_rating_when_show_finished(conn, user_id):
    """Contraparte del test de arriba: una serie ya terminada (show_status distinto de
    'Returning Series') puntuada por el examen general no necesita season_ratings -
    nunca le va a salir una temporada nueva, sembrarla ahi seria ruido sin proposito."""
    title = repo.ensure_manual_title(conn, "show", "Serie terminada", 2020)
    conn.execute("UPDATE titles SET show_status = 'Ended' WHERE id = ?", (title["id"],))
    conn.execute(
        "INSERT INTO episodes (title_id, season_number, episode_number, air_date) VALUES (?, 1, 1, '2020-01-01')",
        (title["id"],),
    )
    conn.execute(
        "INSERT INTO entries (title_id, user_id, status) VALUES (?, ?, 'watched')",
        (title["id"], user_id),
    )
    entry_id = conn.execute("SELECT id FROM entries WHERE title_id = ?", (title["id"],)).fetchone()["id"]

    repo.mark_watched(conn, entry_id, 8.5, "genial")

    assert repo.get_season_rating(conn, entry_id, 1) is None


def test_shift_date_applies_signed_offset():
    from app.repo.titles import _shift_date

    assert _shift_date("2026-09-22", -1) == "2026-09-21"
    assert _shift_date("2026-09-22T10:00:00Z", 1) == "2026-09-23T10:00:00Z"
    assert _shift_date(None, -1) is None
    assert _shift_date("2026-09-22", 0) == "2026-09-22"


def test_sync_episodes_applies_air_date_offset(conn, monkeypatch):
    """El desfase de set_air_date_offset se aplica en sync_episodes a la fecha real de
    los episodios, y se reaplica en cada sync."""
    from app.repo import titles as titles_mod

    title = repo.ensure_manual_title(conn, "show", "Serie con desfase", 2020)
    conn.execute("UPDATE titles SET tmdb_id = 999999 WHERE id = ?", (title["id"],))
    title = repo.get_title(conn, 999999)
    repo.set_air_date_offset(conn, title["id"], -1)
    title = repo.get_title(conn, 999999)

    monkeypatch.setattr(
        titles_mod.tmdb, "get_season_episodes",
        lambda tmdb_id, season_number: [
            {"season_number": season_number, "episode_number": 12, "name": "Ep 12", "air_date": "2026-09-22"}
        ],
    )
    monkeypatch.setattr(titles_mod.tmdb, "get_details", lambda tmdb_id, media_type: {"seasons": [3]})

    titles_mod.sync_episodes(conn, title)

    ep = conn.execute(
        "SELECT air_date FROM episodes WHERE title_id = ? AND season_number = 3 AND episode_number = 12",
        (title["id"],),
    ).fetchone()
    assert ep["air_date"] == "2026-09-21"


def test_next_review_item_ignores_stale_next_episode_date_already_past(conn, user_id):
    """Una fecha de "próximo episodio" ya pasada (caché sin refrescar) no saca la serie
    de /puntuar si no queda nada pendiente."""
    title = repo.ensure_manual_title(conn, "show", "Serie semanal", 2020)
    conn.execute("UPDATE titles SET next_episode_air_date = date('now') WHERE id = ?", (title["id"],))
    conn.execute(
        "INSERT INTO entries (title_id, user_id, status) VALUES (?, ?, 'watched')",
        (title["id"], user_id),
    )
    entry_id = conn.execute("SELECT id FROM entries WHERE title_id = ?", (title["id"],)).fetchone()["id"]
    conn.execute(
        "INSERT INTO episodes (title_id, season_number, episode_number, air_date) VALUES (?, 1, 1, '2020-01-01')",
        (title["id"],),
    )
    episode_id = conn.execute("SELECT id FROM episodes WHERE title_id = ?", (title["id"],)).fetchone()["id"]
    repo._log_episode_watch(conn, episode_id, user_id, "2020-01-01T00:00:00Z")

    item = repo.next_review_item(conn, user_id, [])
    assert item is not None and item["id"] == entry_id
    assert repo.count_review_queue(conn, user_id) == 1


def test_new_completed_season_resurfaces_in_review_queue(conn, user_id):
    """Una serie ya puntuada por temporadas vuelve a /puntuar cuando se completa una
    temporada nueva, con `season_number` de la nueva."""
    title = repo.ensure_manual_title(conn, "show", "Serie con temporada nueva", 2020)
    conn.execute(
        "INSERT INTO entries (title_id, user_id, status) VALUES (?, ?, 'watched')",
        (title["id"], user_id),
    )
    entry_id = conn.execute("SELECT id FROM entries WHERE title_id = ?", (title["id"],)).fetchone()["id"]

    conn.execute(
        "INSERT INTO episodes (title_id, season_number, episode_number, air_date) VALUES (?, 1, 1, '2020-01-01')",
        (title["id"],),
    )
    s1e1 = conn.execute("SELECT id FROM episodes WHERE title_id = ? AND season_number = 1", (title["id"],)).fetchone()["id"]
    repo._log_episode_watch(conn, s1e1, user_id, "2020-01-02T00:00:00Z")
    repo.set_season_rating(conn, entry_id, 1, 8.0, "")

    # Solo hay una temporada, ya puntuada - no deberia salir nada en la cola.
    assert repo.count_review_queue(conn, user_id) == 0
    assert repo.next_review_item(conn, user_id, []) is None
    assert conn.execute("SELECT rating FROM entries WHERE id = ?", (entry_id,)).fetchone()["rating"] == 8.0

    # Sale (y se ve entera) una temporada 2 nueva, todavia sin puntuar.
    conn.execute(
        "INSERT INTO episodes (title_id, season_number, episode_number, air_date) VALUES (?, 2, 1, '2026-01-01')",
        (title["id"],),
    )
    s2e1 = conn.execute("SELECT id FROM episodes WHERE title_id = ? AND season_number = 2", (title["id"],)).fetchone()["id"]
    repo._log_episode_watch(conn, s2e1, user_id, "2026-01-02T00:00:00Z")

    assert repo.count_review_queue(conn, user_id) == 1
    item = repo.next_review_item(conn, user_id, [])
    assert item is not None
    assert item["id"] == entry_id
    assert item["season_number"] == 2

    # Puntuarla la saca de la cola otra vez y la nota general pasa a ser la media de las 2.
    repo.set_season_rating(conn, entry_id, 2, 6.0, "")
    assert repo.count_review_queue(conn, user_id) == 0
    assert repo.next_review_item(conn, user_id, []) is None
    assert conn.execute("SELECT rating FROM entries WHERE id = ?", (entry_id,)).fetchone()["rating"] == 7.0
