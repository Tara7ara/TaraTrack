from app import repo


def _make_show_with_episode(conn, user_id, *, air_date="2020-01-01"):
    """Serie de prueba con una entry pending (de `user_id`) y un episodio T1E1 ya emitido."""
    title = repo.ensure_manual_title(conn, "show", "Test Show", 2020)
    conn.execute(
        "INSERT INTO entries (title_id, user_id, status) VALUES (?, ?, 'pending')", (title["id"], user_id)
    )
    entry = conn.execute(
        "SELECT * FROM entries WHERE title_id = ? AND user_id = ?", (title["id"], user_id)
    ).fetchone()
    conn.execute(
        "INSERT INTO episodes (title_id, season_number, episode_number, air_date) VALUES (?, 1, 1, ?)",
        (title["id"], air_date),
    )
    episode = conn.execute("SELECT * FROM episodes WHERE title_id = ?", (title["id"],)).fetchone()
    return title, entry, episode


def _entry(conn, entry_id):
    return conn.execute("SELECT * FROM entries WHERE id = ?", (entry_id,)).fetchone()


def _episode(conn, episode_id):
    return conn.execute("SELECT * FROM episodes WHERE id = ?", (episode_id,)).fetchone()


def _watched_at(conn, episode_id, user_id):
    """La fecha de un episodio se mira en episode_watches, por usuario."""
    row = conn.execute(
        "SELECT max(watched_at) AS w FROM episode_watches WHERE episode_id = ? AND user_id = ?",
        (episode_id, user_id),
    ).fetchone()
    return row["w"]


def test_toggle_episode_returns_true_only_when_marking_watched(conn, user_id):
    """toggle_episode indica si se acaba de marcar visto (no desmarcado)."""
    _, _, episode = _make_show_with_episode(conn, user_id)

    assert repo.toggle_episode(conn, episode["id"], user_id) is True
    assert repo.toggle_episode(conn, episode["id"], user_id) is False


def test_marking_episode_directly_promotes_pending_to_watched(conn, user_id):
    """Marcar un episodio suelto sin "Marcar vista" pasa la entry a watched
    (_promote_if_first_watch)."""
    title, entry, episode = _make_show_with_episode(conn, user_id)
    repo.toggle_episode(conn, episode["id"], user_id)
    assert _entry(conn, entry["id"])["status"] == "watched"
    assert _watched_at(conn, episode["id"], user_id) is not None


def test_toggle_episode_off_reverts_to_pending_when_none_left(conn, user_id):
    """Desmarcar el unico episodio visto de una serie debe devolver la entry a pending."""
    title, entry, episode = _make_show_with_episode(conn, user_id)
    repo.toggle_episode(conn, episode["id"], user_id)
    assert _entry(conn, entry["id"])["status"] == "watched"

    repo.toggle_episode(conn, episode["id"], user_id)
    assert _entry(conn, entry["id"])["status"] == "pending"
    assert _watched_at(conn, episode["id"], user_id) is None


def test_toggle_episode_off_keeps_watched_if_others_remain(conn, user_id):
    """Si quedan otros episodios vistos, desmarcar uno no debe tocar el status de la entry."""
    title, entry, ep1 = _make_show_with_episode(conn, user_id, air_date="2020-01-01")
    conn.execute(
        "INSERT INTO episodes (title_id, season_number, episode_number, air_date) VALUES (?, 1, 2, '2020-01-08')",
        (title["id"],),
    )
    ep2 = conn.execute(
        "SELECT * FROM episodes WHERE title_id = ? AND episode_number = 2", (title["id"],)
    ).fetchone()

    repo.toggle_episode(conn, ep1["id"], user_id)
    repo.toggle_episode(conn, ep2["id"], user_id)
    assert _entry(conn, entry["id"])["status"] == "watched"

    repo.toggle_episode(conn, ep1["id"], user_id)
    assert _entry(conn, entry["id"])["status"] == "watched"
    assert _watched_at(conn, ep2["id"], user_id) is not None


def test_mark_watched_quick_marks_entry_and_first_episode(conn, user_id):
    title, entry, episode = _make_show_with_episode(conn, user_id)
    title_row = dict(title)
    repo.mark_watched_quick(conn, entry["id"], title_row)

    assert _entry(conn, entry["id"])["status"] == "watched"
    assert _watched_at(conn, episode["id"], user_id) is not None


def test_mark_watched_quick_is_noop_if_already_watched(conn, user_id):
    """Llamarlo dos veces no debe re-marcar ni pisar la fecha del primer visionado."""
    title, entry, episode = _make_show_with_episode(conn, user_id)
    title_row = dict(title)
    repo.mark_watched_quick(conn, entry["id"], title_row)
    first_watched_at = _entry(conn, entry["id"])["watched_at"]

    repo.mark_watched_quick(conn, entry["id"], title_row)
    assert _entry(conn, entry["id"])["watched_at"] == first_watched_at


def test_undo_mark_watched_only_reverts_episodes_from_that_moment(conn, user_id):
    """undo_mark_watched solo desmarca los episodios con el watched_at exacto que puso
    mark_watched_quick."""
    title, entry, ep1 = _make_show_with_episode(conn, user_id, air_date="2020-01-01")
    conn.execute(
        "INSERT INTO episodes (title_id, season_number, episode_number, air_date) VALUES (?, 1, 2, '2020-01-08')",
        (title["id"],),
    )
    ep2 = conn.execute(
        "SELECT * FROM episodes WHERE title_id = ? AND episode_number = 2", (title["id"],)
    ).fetchone()

    # ep1 ya estaba visto, con su propia fecha, antes del marcado por error.
    repo._log_episode_watch(conn, ep1["id"], user_id, "2019-06-01T00:00:00Z")
    conn.execute("UPDATE entries SET status = 'watched', watched_at = '2019-06-01T00:00:00Z' WHERE id = ?", (entry["id"],))

    # Marcado por error via mark_watched_quick: como la entry YA estaba watched, no hace nada -
    # se simula marcando la entry watched a mano con un instante nuevo
    # y el propio mark_watched_quick habria marcado un episodio en ese mismo instante.
    conn.execute("UPDATE entries SET status = 'pending', watched_at = NULL WHERE id = ?", (entry["id"],))
    title_row = dict(title)
    repo.mark_watched_quick(conn, entry["id"], title_row)
    marked_at = _entry(conn, entry["id"])["watched_at"]
    # mark_watched_quick eligio el siguiente episodio sin ver (ep2, el unico que sigue pendiente)
    assert _watched_at(conn, ep2["id"], user_id) == marked_at
    assert _watched_at(conn, ep1["id"], user_id) == "2019-06-01T00:00:00Z"

    repo.undo_mark_watched(conn, entry["id"])
    assert _entry(conn, entry["id"])["status"] == "pending"
    # ep2 (marcado por error) se desmarca...
    assert _watched_at(conn, ep2["id"], user_id) is None
    # ...pero ep1 (visto antes) no se toca.
    assert _watched_at(conn, ep1["id"], user_id) == "2019-06-01T00:00:00Z"


def test_start_rewatch_does_not_erase_watched_at(conn, user_id):
    """"Volver a ver" no borra el historial de episode_watches."""
    title, entry, episode = _make_show_with_episode(conn, user_id)
    repo.toggle_episode(conn, episode["id"], user_id)
    original_watched_at = _watched_at(conn, episode["id"], user_id)

    repo.start_rewatch(conn, entry["id"])

    assert _watched_at(conn, episode["id"], user_id) == original_watched_at
    assert _entry(conn, entry["id"])["rewatch_started_at"] is not None
    sessions = repo.list_watch_sessions(conn, entry["id"])
    assert len(sessions) == 1


def _mark_old_watch(conn, title_id, episode_id, user_id, when="2020-06-01T00:00:00Z"):
    """Marca un episodio visto en una fecha claramente pasada, para que en los tests de
    rewatch sea inequívocamente anterior al inicio de la ronda (start_rewatch usa la
    hora del sistema; en el mismo segundo _pending_clause no los distinguiría)."""
    repo._log_episode_watch(conn, episode_id, user_id, when)
    repo._promote_if_first_watch(conn, title_id, user_id)


def test_rewatch_makes_old_episode_pending_again(conn, user_id):
    """Tras "Volver a ver", lo visto antes de la ronda cuenta como pendiente, sin perder
    su fecha."""
    title, entry, episode = _make_show_with_episode(conn, user_id)
    _mark_old_watch(conn, title["id"], episode["id"], user_id)
    assert repo.next_unwatched_episode(conn, title["id"], None, user_id) is None  # nada pendiente todavia

    repo.start_rewatch(conn, entry["id"])
    rewatch_started_at = _entry(conn, entry["id"])["rewatch_started_at"]

    next_ep = repo.next_unwatched_episode(conn, title["id"], rewatch_started_at, user_id)
    assert next_ep is not None
    assert next_ep["id"] == episode["id"]
    # la fecha del visionado anterior sigue intacta, nada se ha perdido
    assert _watched_at(conn, episode["id"], user_id) == "2020-06-01T00:00:00Z"


def test_rewatch_remark_keeps_full_history(conn, user_id):
    """Re-marcar un episodio durante un rewatch en curso debe dejar las DOS fechas en
    episode_watches (nunca se borra el historial)."""
    title, entry, episode = _make_show_with_episode(conn, user_id)
    _mark_old_watch(conn, title["id"], episode["id"], user_id)

    repo.start_rewatch(conn, entry["id"])
    repo.toggle_episode(conn, episode["id"], user_id)  # se re-marca, round-aware -> contaba como "no visto ahora"

    new_watched_at = _watched_at(conn, episode["id"], user_id)
    assert new_watched_at > "2020-06-01T00:00:00Z"

    history = conn.execute(
        "SELECT watched_at FROM episode_watches WHERE episode_id = ? AND user_id = ? ORDER BY watched_at",
        (episode["id"], user_id),
    ).fetchall()
    assert len(history) == 2
    assert history[0]["watched_at"] == "2020-06-01T00:00:00Z"


def test_rewatch_unmark_recent_restores_old_date_not_null(conn, user_id):
    """Desmarcar un episodio recien re-marcado en un rewatch activo debe devolverlo a
    su fecha vieja (visto antes), NO a NULL - 'nada se pierde' tambien significa esto."""
    title, entry, episode = _make_show_with_episode(conn, user_id)
    _mark_old_watch(conn, title["id"], episode["id"], user_id)

    repo.start_rewatch(conn, entry["id"])
    repo.toggle_episode(conn, episode["id"], user_id)  # re-marcado
    repo.toggle_episode(conn, episode["id"], user_id)  # desmarcado otra vez

    assert _watched_at(conn, episode["id"], user_id) == "2020-06-01T00:00:00Z"


def test_episode_watch_is_isolated_per_user(conn, user_id):
    """Marcar un episodio no lo marca como visto para otro usuario del mismo título."""
    other = repo.create_user(conn, "otro", "unaclave123")
    title, entry_a, episode = _make_show_with_episode(conn, user_id)
    conn.execute(
        "INSERT INTO entries (title_id, user_id, status) VALUES (?, ?, 'pending')", (title["id"], other["id"])
    )
    entry_b = conn.execute(
        "SELECT * FROM entries WHERE title_id = ? AND user_id = ?", (title["id"], other["id"])
    ).fetchone()

    repo.toggle_episode(conn, episode["id"], user_id)

    assert _entry(conn, entry_a["id"])["status"] == "watched"
    assert _entry(conn, entry_b["id"])["status"] == "pending"
    assert _watched_at(conn, episode["id"], user_id) is not None
    assert _watched_at(conn, episode["id"], other["id"]) is None


def test_library_version_changes_when_marking_and_unmarking(conn, user_id):
    """La huella de /pendientes cambia al marcar y al desmarcar un episodio, y no la
    toca lo que hace otro usuario."""
    other = repo.create_user(conn, "otra", "unaclave123")
    title = repo.ensure_manual_title(conn, "show", "Serie huella", 2020)
    conn.execute(
        "INSERT INTO episodes (title_id, season_number, episode_number, air_date) VALUES (?, 1, 1, '2020-01-01')",
        (title["id"],),
    )
    ep = conn.execute("SELECT * FROM episodes WHERE title_id = ?", (title["id"],)).fetchone()
    repo.ensure_entry(conn, title["tmdb_id"], "show", user_id)
    repo.ensure_entry(conn, title["tmdb_id"], "show", other["id"])

    v0 = repo.library_version(conn, user_id)
    repo.toggle_episode(conn, ep["id"], other["id"])
    assert repo.library_version(conn, user_id) == v0
    repo.toggle_episode(conn, ep["id"], user_id)
    v1 = repo.library_version(conn, user_id)
    assert v1 != v0
    repo.toggle_episode(conn, ep["id"], user_id)
    assert repo.library_version(conn, user_id) not in (v1,)
