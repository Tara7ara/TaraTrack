from app import repo


def _make_show_with_episode(conn, *, air_date="2020-01-01"):
    """Serie de prueba con una entry pending y un episodio T1E1 ya emitido."""
    title = repo.ensure_manual_title(conn, "show", "Test Show", 2020)
    conn.execute("INSERT INTO entries (title_id, status) VALUES (?, 'pending')", (title["id"],))
    entry = conn.execute("SELECT * FROM entries WHERE title_id = ?", (title["id"],)).fetchone()
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


def test_marking_episode_directly_promotes_pending_to_watched(conn):
    """Bug real encontrado en produccion: marcar un episodio suelto sin
    pasar por el boton 'Marcar vista' dejaba la entry en pending para siempre, aunque
    la serie estuviera vista entera. _promote_if_first_watch debe pasarla a watched."""
    title, entry, episode = _make_show_with_episode(conn)
    repo.toggle_episode(conn, episode["id"])
    assert _entry(conn, entry["id"])["status"] == "watched"
    assert _episode(conn, episode["id"])["watched_at"] is not None


def test_toggle_episode_off_reverts_to_pending_when_none_left(conn):
    """Desmarcar el unico episodio visto de una serie debe devolver la entry a pending."""
    title, entry, episode = _make_show_with_episode(conn)
    repo.toggle_episode(conn, episode["id"])
    assert _entry(conn, entry["id"])["status"] == "watched"

    repo.toggle_episode(conn, episode["id"])
    assert _entry(conn, entry["id"])["status"] == "pending"
    assert _episode(conn, episode["id"])["watched_at"] is None


def test_toggle_episode_off_keeps_watched_if_others_remain(conn):
    """Si quedan otros episodios vistos, desmarcar uno no debe tocar el status de la entry."""
    title, entry, ep1 = _make_show_with_episode(conn, air_date="2020-01-01")
    conn.execute(
        "INSERT INTO episodes (title_id, season_number, episode_number, air_date) VALUES (?, 1, 2, '2020-01-08')",
        (title["id"],),
    )
    ep2 = conn.execute(
        "SELECT * FROM episodes WHERE title_id = ? AND episode_number = 2", (title["id"],)
    ).fetchone()

    repo.toggle_episode(conn, ep1["id"])
    repo.toggle_episode(conn, ep2["id"])
    assert _entry(conn, entry["id"])["status"] == "watched"

    repo.toggle_episode(conn, ep1["id"])
    assert _entry(conn, entry["id"])["status"] == "watched"
    assert _episode(conn, ep2["id"])["watched_at"] is not None


def test_mark_watched_quick_marks_entry_and_first_episode(conn):
    title, entry, episode = _make_show_with_episode(conn)
    title_row = dict(title)
    repo.mark_watched_quick(conn, entry["id"], title_row)

    assert _entry(conn, entry["id"])["status"] == "watched"
    assert _episode(conn, episode["id"])["watched_at"] is not None


def test_mark_watched_quick_is_noop_if_already_watched(conn):
    """Llamarlo dos veces no debe re-marcar ni pisar la fecha del primer visionado."""
    title, entry, episode = _make_show_with_episode(conn)
    title_row = dict(title)
    repo.mark_watched_quick(conn, entry["id"], title_row)
    first_watched_at = _entry(conn, entry["id"])["watched_at"]

    repo.mark_watched_quick(conn, entry["id"], title_row)
    assert _entry(conn, entry["id"])["watched_at"] == first_watched_at


def test_undo_mark_watched_only_reverts_episodes_from_that_moment(conn):
    """Ronda 2026-08-20: undo_mark_watched ya NO desmarca todos los episodios del
    titulo a ciegas - solo los que comparten el watched_at exacto que puso
    mark_watched_quick, para no perder episodios vistos de verdad de antes."""
    title, entry, ep1 = _make_show_with_episode(conn, air_date="2020-01-01")
    conn.execute(
        "INSERT INTO episodes (title_id, season_number, episode_number, air_date) VALUES (?, 1, 2, '2020-01-08')",
        (title["id"],),
    )
    ep2 = conn.execute(
        "SELECT * FROM episodes WHERE title_id = ? AND episode_number = 2", (title["id"],)
    ).fetchone()

    # ep1 ya estaba visto de verdad, con su propia fecha, ANTES del marcado por error.
    repo._log_episode_watch(conn, ep1["id"], "2019-06-01T00:00:00Z")
    conn.execute("UPDATE entries SET status = 'watched', watched_at = '2019-06-01T00:00:00Z' WHERE id = ?", (entry["id"],))

    # Marcado por error via mark_watched_quick: como la entry YA estaba watched, no hace nada -
    # simulamos el caso real (Tougen Anki) marcando el entry watched a mano con un instante nuevo
    # y el propio mark_watched_quick habria marcado un episodio en ese mismo instante.
    conn.execute("UPDATE entries SET status = 'pending', watched_at = NULL WHERE id = ?", (entry["id"],))
    title_row = dict(title)
    repo.mark_watched_quick(conn, entry["id"], title_row)
    marked_at = _entry(conn, entry["id"])["watched_at"]
    # mark_watched_quick eligio el siguiente episodio sin ver (ep2, el unico que sigue pendiente)
    assert _episode(conn, ep2["id"])["watched_at"] == marked_at
    assert _episode(conn, ep1["id"])["watched_at"] == "2019-06-01T00:00:00Z"

    repo.undo_mark_watched(conn, entry["id"])
    assert _entry(conn, entry["id"])["status"] == "pending"
    # ep2 (marcado por error) se desmarca...
    assert _episode(conn, ep2["id"])["watched_at"] is None
    # ...pero ep1 (visto de verdad antes) no se toca.
    assert _episode(conn, ep1["id"])["watched_at"] == "2019-06-01T00:00:00Z"


def test_start_rewatch_does_not_erase_watched_at(conn):
    """Estilo Trakt (2026-08-20): 'Volver a ver' nunca borra episodes.watched_at -
    solo guarda cuando empezo la ronda nueva."""
    title, entry, episode = _make_show_with_episode(conn)
    repo.toggle_episode(conn, episode["id"])
    original_watched_at = _episode(conn, episode["id"])["watched_at"]

    repo.start_rewatch(conn, entry["id"])

    assert _episode(conn, episode["id"])["watched_at"] == original_watched_at
    assert _entry(conn, entry["id"])["rewatch_started_at"] is not None
    sessions = repo.list_watch_sessions(conn, entry["id"])
    assert len(sessions) == 1


def _mark_old_watch(conn, title_id, episode_id, when="2020-06-01T00:00:00Z"):
    """Marca un episodio visto en una fecha claramente pasada (no 'ahora') - para los
    tests de rewatch, donde hace falta que el visionado original sea de forma
    inequivoca ANTERIOR al inicio de la ronda nueva (start_rewatch usa la hora real
    del sistema); con dos marcados en el mismo segundo, _pending_clause ('<') no
    distinguiria cual es cual."""
    repo._log_episode_watch(conn, episode_id, when)
    repo._promote_if_first_watch(conn, title_id)


def test_rewatch_makes_old_episode_pending_again(conn):
    """Tras 'Volver a ver', el episodio visto antes de la ronda debe volver a
    contar como pendiente de re-ver (_pending_clause), aunque su fecha vieja siga ahi."""
    title, entry, episode = _make_show_with_episode(conn)
    _mark_old_watch(conn, title["id"], episode["id"])
    assert repo.next_unwatched_episode(conn, title["id"]) is None  # nada pendiente todavia

    repo.start_rewatch(conn, entry["id"])
    rewatch_started_at = _entry(conn, entry["id"])["rewatch_started_at"]

    next_ep = repo.next_unwatched_episode(conn, title["id"], rewatch_started_at)
    assert next_ep is not None
    assert next_ep["id"] == episode["id"]
    # la fecha del visionado anterior sigue intacta, nada se ha perdido
    assert _episode(conn, episode["id"])["watched_at"] == "2020-06-01T00:00:00Z"


def test_rewatch_remark_keeps_full_history(conn):
    """Re-marcar un episodio durante un rewatch en curso debe dejar las DOS fechas en
    episode_watches (nunca se borra el historial), con watched_at como cache de la mas reciente."""
    title, entry, episode = _make_show_with_episode(conn)
    _mark_old_watch(conn, title["id"], episode["id"])

    repo.start_rewatch(conn, entry["id"])
    repo.toggle_episode(conn, episode["id"])  # se re-marca, round-aware -> contaba como "no visto ahora"

    new_watched_at = _episode(conn, episode["id"])["watched_at"]
    assert new_watched_at > "2020-06-01T00:00:00Z"

    history = conn.execute(
        "SELECT watched_at FROM episode_watches WHERE episode_id = ? ORDER BY watched_at", (episode["id"],)
    ).fetchall()
    assert len(history) == 2
    assert history[0]["watched_at"] == "2020-06-01T00:00:00Z"


def test_rewatch_unmark_recent_restores_old_date_not_null(conn):
    """Desmarcar un episodio recien re-marcado en un rewatch activo debe devolverlo a
    su fecha vieja (visto antes), NO a NULL - 'nada se pierde' tambien significa esto."""
    title, entry, episode = _make_show_with_episode(conn)
    _mark_old_watch(conn, title["id"], episode["id"])

    repo.start_rewatch(conn, entry["id"])
    repo.toggle_episode(conn, episode["id"])  # re-marcado
    repo.toggle_episode(conn, episode["id"])  # desmarcado otra vez

    assert _episode(conn, episode["id"])["watched_at"] == "2020-06-01T00:00:00Z"
