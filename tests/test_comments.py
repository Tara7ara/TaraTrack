from app import repo


def _episode(conn):
    """Serie + episodio de prueba, sin entry de nadie en concreto - el debate es
    sobre el episodio (catalogo compartido), no sobre la entry de un usuario."""
    title = repo.ensure_manual_title(conn, "show", "Test Show", 2020)
    conn.execute(
        "INSERT INTO episodes (title_id, season_number, episode_number, air_date) VALUES (?, 1, 1, '2020-01-01')",
        (title["id"],),
    )
    return conn.execute("SELECT * FROM episodes WHERE title_id = ?", (title["id"],)).fetchone()


def test_comment_visible_to_other_user(conn, user_id):
    """El hilo de debate es la unica pieza pensada para verse ENTRE usuarios (Fase 4,
    2026-09-18) - a diferencia de todo lo demas del multiusuario, que esta aislado."""
    other = repo.create_user(conn, "amigo", "unaclave123")
    ep = _episode(conn)

    repo.add_episode_comment(conn, ep["id"], user_id, "Menudo capitulo")

    mine = repo.list_episode_comments(conn, ep["id"])
    theirs = repo.list_episode_comments(conn, ep["id"])
    assert len(mine) == 1
    assert mine[0]["body"] == "Menudo capitulo"
    assert theirs == mine
    assert repo.count_episode_comments(conn, ep["id"]) == 1
    _ = other


def test_comments_are_ordered_chronologically(conn, user_id):
    other = repo.create_user(conn, "amigo", "unaclave123")
    ep = _episode(conn)

    repo.add_episode_comment(conn, ep["id"], user_id, "primero")
    repo.add_episode_comment(conn, ep["id"], other["id"], "segundo")

    comments = repo.list_episode_comments(conn, ep["id"])
    assert [c["body"] for c in comments] == ["primero", "segundo"]
    assert [c["username"] for c in comments] == ["tara", "amigo"]


def test_empty_comment_is_ignored(conn, user_id):
    ep = _episode(conn)
    repo.add_episode_comment(conn, ep["id"], user_id, "   ")
    assert repo.count_episode_comments(conn, ep["id"]) == 0


def test_delete_episode_comment_by_owner(conn, user_id):
    ep = _episode(conn)
    repo.add_episode_comment(conn, ep["id"], user_id, "mio")
    comment_id = repo.list_episode_comments(conn, ep["id"])[0]["id"]

    repo.delete_episode_comment(conn, comment_id, user_id, is_admin=False)

    assert repo.count_episode_comments(conn, ep["id"]) == 0


def test_delete_episode_comment_by_another_user_is_a_no_op(conn, user_id):
    other = repo.create_user(conn, "amigo", "unaclave123")
    ep = _episode(conn)
    repo.add_episode_comment(conn, ep["id"], user_id, "de tara")
    comment_id = repo.list_episode_comments(conn, ep["id"])[0]["id"]

    repo.delete_episode_comment(conn, comment_id, other["id"], is_admin=False)

    assert repo.count_episode_comments(conn, ep["id"]) == 1


def test_delete_episode_comment_by_admin_removes_anyones(conn, user_id):
    """Fase 5 (Tara, 2026-09-18: 'eliminar mensajes de todos, cada uno el suyo pero
    admin el total')."""
    other = repo.create_user(conn, "amigo", "unaclave123")
    ep = _episode(conn)
    repo.add_episode_comment(conn, ep["id"], other["id"], "del amigo")
    comment_id = repo.list_episode_comments(conn, ep["id"])[0]["id"]

    repo.delete_episode_comment(conn, comment_id, user_id, is_admin=True)

    assert repo.count_episode_comments(conn, ep["id"]) == 0


def _seen_long_ago(conn, user_id):
    """comments_seen_at es datetime('now') a precision de segundo (SQL, no Python) -
    fijarlo muy en el pasado evita que estos tests dependan de que create_user y el
    comentario de prueba caigan en el mismo segundo de reloj (flakiness real, no
    solo teorica, con maquinas rapidas)."""
    conn.execute("UPDATE users SET comments_seen_at = '2000-01-01 00:00:00' WHERE id = ?", (user_id,))


def test_own_comments_never_count_as_unseen_for_yourself(conn, user_id):
    """Aviso del nav (Tara, 2026-09-18: "estilo tvtime") - no hace falta avisarte
    de lo que tu mismo acabas de escribir."""
    _seen_long_ago(conn, user_id)
    ep = _episode(conn)
    repo.add_episode_comment(conn, ep["id"], user_id, "mio")
    assert repo.count_unseen_comments(conn, user_id) == 0


def test_comment_from_another_user_counts_as_unseen(conn, user_id):
    other = repo.create_user(conn, "amigo", "unaclave123")
    _seen_long_ago(conn, user_id)
    ep = _episode(conn)
    repo.add_episode_comment(conn, ep["id"], other["id"], "del amigo")
    assert repo.count_unseen_comments(conn, user_id) == 1


def test_mark_comments_seen_resets_the_counter(conn, user_id):
    other = repo.create_user(conn, "amigo", "unaclave123")
    _seen_long_ago(conn, user_id)
    ep = _episode(conn)
    repo.add_episode_comment(conn, ep["id"], other["id"], "del amigo")
    assert repo.count_unseen_comments(conn, user_id) == 1

    repo.mark_comments_seen(conn, user_id)

    assert repo.count_unseen_comments(conn, user_id) == 0


def test_get_oldest_unseen_comment_points_to_the_right_episode(conn, user_id):
    other = repo.create_user(conn, "amigo", "unaclave123")
    _seen_long_ago(conn, user_id)
    ep = _episode(conn)
    repo.add_episode_comment(conn, ep["id"], other["id"], "del amigo")

    target = repo.get_oldest_unseen_comment(conn, user_id)

    assert target["episode_id"] == ep["id"]


def test_comments_scoped_to_their_own_episode(conn, user_id):
    """Un comentario en el episodio 1 no debe aparecer al listar el episodio 2."""
    title = repo.ensure_manual_title(conn, "show", "Otra serie", 2020)
    conn.execute(
        "INSERT INTO episodes (title_id, season_number, episode_number, air_date) VALUES (?, 1, 1, '2020-01-01')",
        (title["id"],),
    )
    conn.execute(
        "INSERT INTO episodes (title_id, season_number, episode_number, air_date) VALUES (?, 1, 2, '2020-01-08')",
        (title["id"],),
    )
    ep1, ep2 = conn.execute("SELECT * FROM episodes WHERE title_id = ? ORDER BY episode_number", (title["id"],)).fetchall()

    repo.add_episode_comment(conn, ep1["id"], user_id, "solo del primero")

    assert repo.count_episode_comments(conn, ep1["id"]) == 1
    assert repo.count_episode_comments(conn, ep2["id"]) == 0
