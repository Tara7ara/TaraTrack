import httpx

from app import repo
from app.repo import merge as merge_mod
from app.repo import sync as sync_mod


def _show(conn, name, tmdb_id, seasons):
    """seasons = {season_number: [air_date, ...]}, un episodio por fecha."""
    conn.execute(
        "INSERT INTO titles (tmdb_id, type, title, year) VALUES (?, 'show', ?, 2022)", (tmdb_id, name)
    )
    title = conn.execute("SELECT * FROM titles WHERE tmdb_id = ?", (tmdb_id,)).fetchone()
    for season, dates in seasons.items():
        for n, air in enumerate(dates, start=1):
            conn.execute(
                "INSERT INTO episodes (title_id, season_number, episode_number, air_date) VALUES (?, ?, ?, ?)",
                (title["id"], season, n, air),
            )
    return title


def _entry(conn, title, user_id, status, rating=None):
    conn.execute(
        "INSERT INTO entries (title_id, user_id, status, rating) VALUES (?, ?, ?, ?)",
        (title["id"], user_id, status, rating),
    )
    return conn.execute(
        "SELECT * FROM entries WHERE title_id = ? AND user_id = ?", (title["id"], user_id)
    ).fetchone()


def _ep(conn, title, season, number):
    return conn.execute(
        "SELECT id FROM episodes WHERE title_id = ? AND season_number = ? AND episode_number = ?",
        (title["id"], season, number),
    ).fetchone()["id"]


def _edgerunners(conn):
    target = _show(conn, "Edgerunners", 105248, {1: ["2022-09-13", "2022-09-13"], 2: ["2026-10-20", "2026-10-20"]})
    orphan = _show(conn, "Edgerunners 2", 326788, {1: ["2026-10-20", "2026-10-20"]})
    return target, orphan


def test_merge_moves_entries_season_ratings_and_watches(conn, user_id):
    target, orphan = _edgerunners(conn)
    amigo = repo.create_user(conn, "amigo", "unaclave123")
    otro = repo.create_user(conn, "otro", "unaclave123")
    mine_target = _entry(conn, target, user_id, "watched", 9.0)
    _entry(conn, orphan, user_id, "pending")
    theirs = _entry(conn, orphan, amigo["id"], "watched", 9.0)
    _entry(conn, orphan, otro["id"], "pending")
    conn.execute(
        "INSERT INTO season_ratings (entry_id, season_number, rating) VALUES (?, 1, 9.0)", (theirs["id"],)
    )
    conn.execute(
        "INSERT INTO episode_watches (episode_id, user_id, watched_at) VALUES (?, ?, '2026-10-21T10:00:00Z')",
        (_ep(conn, orphan, 1, 1), amigo["id"]),
    )

    assert repo.merge_gone_title(conn, orphan["id"], target["id"]) is True

    assert conn.execute("SELECT count(*) FROM titles WHERE id = ?", (orphan["id"],)).fetchone()[0] == 0
    entries = conn.execute(
        "SELECT id, user_id, status, rating FROM entries WHERE title_id = ? ORDER BY user_id", (target["id"],)
    ).fetchall()
    assert [(e["user_id"], e["status"]) for e in entries] == [
        (user_id, "watched"), (amigo["id"], "watched"), (otro["id"], "pending")
    ]
    assert entries[0]["id"] == mine_target["id"] and entries[0]["rating"] == 9.0
    # La T1 de la huérfana es la T2 de la ficha buena (empiezan el mismo día).
    assert conn.execute(
        "SELECT season_number, rating FROM season_ratings WHERE entry_id = ?", (theirs["id"],)
    ).fetchone()[:] == (2, 9.0)
    watch = conn.execute(
        "SELECT episode_id FROM episode_watches WHERE user_id = ?", (amigo["id"],)
    ).fetchone()
    assert watch["episode_id"] == _ep(conn, target, 2, 1)
    assert conn.execute(
        "SELECT watched_at FROM episodes WHERE id = ?", (_ep(conn, target, 2, 1),)
    ).fetchone()[0] == "2026-10-21T10:00:00Z"


def test_merge_keeps_existing_entry_and_brings_missing_data(conn, user_id):
    target, orphan = _edgerunners(conn)
    keep = _entry(conn, target, user_id, "pending")
    gone = _entry(conn, orphan, user_id, "watched", 8.0)
    conn.execute("INSERT INTO season_ratings (entry_id, season_number, rating) VALUES (?, 1, 8.0)", (gone["id"],))

    assert repo.merge_gone_title(conn, orphan["id"], target["id"]) is True

    row = conn.execute("SELECT status, rating FROM entries WHERE id = ?", (keep["id"],)).fetchone()
    assert (row["status"], row["rating"]) == ("watched", 8.0)
    assert conn.execute("SELECT count(*) FROM entries WHERE id = ?", (gone["id"],)).fetchone()[0] == 0
    assert conn.execute(
        "SELECT season_number FROM season_ratings WHERE entry_id = ?", (keep["id"],)
    ).fetchone()[0] == 2


def test_merge_refuses_when_user_data_has_nowhere_to_go(conn, user_id):
    target = _show(conn, "Otra serie", 1000, {1: ["2020-01-01"]})
    orphan = _show(conn, "Sin relacion", 2000, {1: ["2026-10-20"]})
    _entry(conn, orphan, user_id, "watched")
    conn.execute(
        "INSERT INTO episode_watches (episode_id, user_id, watched_at) VALUES (?, ?, '2026-10-21T10:00:00Z')",
        (_ep(conn, orphan, 1, 1), user_id),
    )

    assert repo.merge_gone_title(conn, orphan["id"], target["id"]) is False
    assert conn.execute("SELECT count(*) FROM entries WHERE title_id = ?", (orphan["id"],)).fetchone()[0] == 1


def test_gone_title_without_replacement_is_flagged(conn, user_id, monkeypatch):
    orphan = _show(conn, "Woody", 315842, {1: ["1957-01-01"]})
    _entry(conn, orphan, user_id, "watched")
    monkeypatch.setattr(merge_mod.tmdb, "search", lambda q: [])

    assert repo.handle_gone_title(conn, orphan) is None
    assert conn.execute("SELECT tmdb_gone FROM titles WHERE id = ?", (orphan["id"],)).fetchone()[0] == 1


def test_gone_title_merges_into_same_anilist_title(conn, user_id, monkeypatch):
    target, orphan = _edgerunners(conn)
    conn.execute("UPDATE titles SET anilist_id = 195539 WHERE id IN (?, ?)", (target["id"], orphan["id"]))
    _entry(conn, orphan, user_id, "pending")
    monkeypatch.setattr(merge_mod, "sync_episodes", lambda c, t: None)
    monkeypatch.setattr(merge_mod.tmdb, "search", lambda q: [])
    orphan = conn.execute("SELECT * FROM titles WHERE id = ?", (orphan["id"],)).fetchone()

    assert repo.handle_gone_title(conn, orphan) == target["id"]
    assert conn.execute("SELECT count(*) FROM entries WHERE title_id = ?", (target["id"],)).fetchone()[0] == 1


def test_base_names_strip_trailing_season_number():
    row = {"original_title": "Cyberpunk: Edgerunners 2", "title": "Cyberpunk: Edgerunners 2"}
    assert merge_mod._base_names(row) == ["Cyberpunk: Edgerunners"]


def test_sync_handles_404_instead_of_counting_an_error(conn, user_id, monkeypatch):
    orphan = _show(conn, "Woody", 315842, {1: ["1957-01-01"]})
    _entry(conn, orphan, user_id, "watched")
    conn.commit()
    response = httpx.Response(404, request=httpx.Request("GET", "https://api.themoviedb.org/3/tv/315842"))

    def gone(c, row):
        raise httpx.HTTPStatusError("404", request=response.request, response=response)

    handled = []
    monkeypatch.setattr(sync_mod, "refresh_metadata", gone)
    monkeypatch.setattr(sync_mod, "handle_gone_title", lambda c, row: handled.append(row["tmdb_id"]))

    assert sync_mod._sync_library_body(repo.list_trackable_titles(conn)) == 0
    assert handled == [315842]
