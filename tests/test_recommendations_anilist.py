from app import repo
from app.repo import recommendations as recs


def _title(conn, name, anilist_id, user_id, rating=None, cross=""):
    title = repo.ensure_manual_title(conn, "show", name, 2020)
    conn.execute(
        "UPDATE titles SET anilist_id = ?, anilist_cross_rec_ids = ? WHERE id = ?",
        (anilist_id, cross, title["id"]),
    )
    conn.execute(
        "INSERT INTO entries (title_id, user_id, status, rating) VALUES (?, ?, 'watched', ?)",
        (title["id"], user_id, rating),
    )


def test_community_candidates_sum_over_seeds_and_skip_known(conn, user_id):
    """Un anime recomendado por dos semillas puntua mas que uno recomendado por una,
    y lo que ya esta en la biblioteca no se recomienda."""
    _title(conn, "Semilla A", 1, user_id, 10.0, "900,901,3")
    _title(conn, "Semilla B", 2, user_id, 9.0, "900")
    _title(conn, "Ya vista", 3, user_id, 6.0)
    _title(conn, "Floja", 4, user_id, 5.0, "902")  # no es semilla (< 8.5)

    cands = recs._anilist_community_candidates(conn, user_id, {})
    ids = [c for c, _score, _seeds in cands]
    assert ids[0] == 900
    assert 901 in ids
    assert 3 not in ids and 902 not in ids
    top_seeds = [title for _w, title in cands[0][2]]
    assert top_seeds == ["Semilla A", "Semilla B"]  # la de mas peso primero


def test_community_candidates_respect_seed_ban_penalty(conn, user_id):
    _title(conn, "Mala recomendadora", 1, user_id, 10.0, "900")
    _title(conn, "Buena", 2, user_id, 9.0, "901")
    bans = {"Mala recomendadora": recs.SEED_BAN_THRESHOLD}

    cands = dict((c, s) for c, s, _ in recs._anilist_community_candidates(conn, user_id, bans))
    assert cands[901] > cands[900]


def test_list_recommendations_pages_by_score_and_wraps(conn, user_id):
    """Sin sorteo: la tanda 0 son siempre las de mas score, la siguiente continua, y
    al acabarse vuelve al principio."""
    for i in range(5):
        conn.execute(
            """INSERT INTO recommendations_cache (user_id, tmdb_id, type, title, seeds, score)
               VALUES (?, ?, 'show', ?, 'Semilla', ?)""",
            (user_id, 1000 + i, f"R{i}", float(i)),
        )
    first, pages = repo.list_recommendations(conn, user_id, limit=2, with_pages=True)
    assert pages == 3
    assert [r["tmdb_id"] for r in first] == [1004, 1003]
    assert [r["tmdb_id"] for r in repo.list_recommendations(conn, user_id, limit=2, tanda=1)] == [1002, 1001]
    assert [r["tmdb_id"] for r in repo.list_recommendations(conn, user_id, limit=2, tanda=3)] == [1004, 1003]
