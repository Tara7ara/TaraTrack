"""Regresion de los bugs reportados por la auditoria de AGY (2026-09-18)."""
from app import repo


def test_create_manual_entry_is_scoped_to_user(conn, user_id):
    """Bug real: create_manual_entry insertaba la entry sin user_id - no aparecia
    en los pendientes de nadie ("entries.user_id" quedaba NULL)."""
    other = repo.create_user(conn, "amigo", "unaclave123")
    repo.create_manual_entry(conn, "show", "Serie manual de tara", 2020, None, user_id)
    repo.create_manual_entry(conn, "show", "Serie manual del amigo", 2020, None, other["id"])

    mine = conn.execute("SELECT title_id, user_id FROM entries WHERE user_id = ?", (user_id,)).fetchall()
    theirs = conn.execute("SELECT title_id, user_id FROM entries WHERE user_id = ?", (other["id"],)).fetchall()
    assert len(mine) == 1
    assert len(theirs) == 1
    assert conn.execute("SELECT count(*) FROM entries WHERE user_id IS NULL").fetchone()[0] == 0


def test_remove_pending_entry_with_episode_comments_does_not_crash(conn, user_id):
    """Bug real: episode_comments se quedo fuera del cascade de borrado - FOREIGN KEY
    constraint failed al borrar `episodes` en cuanto un episodio tenia un comentario."""
    title = repo.ensure_manual_title(conn, "show", "Serie con debate", 2020)
    conn.execute(
        "INSERT INTO entries (title_id, user_id, status) VALUES (?, ?, 'pending')", (title["id"], user_id)
    )
    entry = conn.execute("SELECT * FROM entries WHERE title_id = ?", (title["id"],)).fetchone()
    conn.execute(
        "INSERT INTO episodes (title_id, season_number, episode_number) VALUES (?, 1, 1)", (title["id"],)
    )
    ep = conn.execute("SELECT * FROM episodes WHERE title_id = ?", (title["id"],)).fetchone()
    repo.add_episode_comment(conn, ep["id"], user_id, "Comentario de debate")

    repo.remove_pending_entry(conn, entry["id"])

    assert conn.execute("SELECT count(*) FROM entries WHERE id = ?", (entry["id"],)).fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM episode_comments").fetchone()[0] == 0


def test_get_or_create_default_list_recovers_missing_list(conn, user_id):
    conn.execute("DELETE FROM lists WHERE user_id = ? AND is_default = 1", (user_id,))
    assert repo.get_default_list(conn, user_id) is None

    recovered = repo.get_or_create_default_list(conn, user_id)

    assert recovered is not None
    assert recovered["is_default"] == 1


def test_record_duel_rejects_ids_owned_by_another_user(conn, user_id):
    """Gap real: un POST manipulado a /duelo con a_id/b_id de otro usuario podia
    tocar su Elo - record_duel ahora exige que ambos sean del user_id que vota."""
    other = repo.create_user(conn, "amigo", "unaclave123")
    title_a = repo.ensure_manual_title(conn, "movie", "Peli A", 2020)
    title_b = repo.ensure_manual_title(conn, "movie", "Peli B", 2021)
    conn.execute("INSERT INTO entries (title_id, user_id, status) VALUES (?, ?, 'watched')", (title_a["id"], other["id"]))
    conn.execute("INSERT INTO entries (title_id, user_id, status) VALUES (?, ?, 'watched')", (title_b["id"], other["id"]))
    a = conn.execute("SELECT id, elo FROM entries WHERE title_id = ?", (title_a["id"],)).fetchone()
    b = conn.execute("SELECT id, elo FROM entries WHERE title_id = ?", (title_b["id"],)).fetchone()

    repo.record_duel(conn, "entries", a["id"], b["id"], user_id, 1.0)

    a_after = conn.execute("SELECT elo FROM entries WHERE id = ?", (a["id"],)).fetchone()
    assert a_after["elo"] == a["elo"]


def test_rename_list_to_a_duplicate_name_does_not_crash(conn, user_id):
    """Bug real (AGY, 2026-09-18): lists tiene UNIQUE(user_id, name) - renombrar a
    un nombre que ya usa otra lista TUYA reventaba con sqlite3.IntegrityError (500)
    sin capturar."""
    repo.create_list(conn, "Top 10", user_id)
    lista_b = repo.create_list(conn, "Por ver", user_id)

    repo.rename_list(conn, lista_b["id"], user_id, "Top 10")

    unchanged = conn.execute("SELECT name FROM lists WHERE id = ?", (lista_b["id"],)).fetchone()
    assert unchanged["name"] == "Por ver"


def test_rename_list_same_name_on_another_users_list_is_fine(conn, user_id):
    """El UNIQUE es (user_id, name) - dos usuarios SI pueden tener cada uno una
    lista con el mismo nombre, esto no debe bloquearse por error."""
    other = repo.create_user(conn, "amigo", "unaclave123")
    mine = repo.create_list(conn, "Por ver", user_id)
    repo.create_list(conn, "Top 10", other["id"])

    repo.rename_list(conn, mine["id"], user_id, "Top 10")

    renamed = conn.execute("SELECT name FROM lists WHERE id = ?", (mine["id"],)).fetchone()
    assert renamed["name"] == "Top 10"


def test_get_available_years_ignores_malformed_watched_at(conn, user_id):
    """Bug real (AGY, 2026-09-18): int(r["y"]) sin proteger tumbaba /resumen entero
    si algun watched_at no empezaba por un año de verdad."""
    title = repo.ensure_manual_title(conn, "movie", "Peli con fecha rara", 2020)
    conn.execute(
        "INSERT INTO entries (title_id, user_id, status, watched_at) VALUES (?, ?, 'watched', ?)",
        (title["id"], user_id, "fecha-invalida"),
    )
    title2 = repo.ensure_manual_title(conn, "movie", "Peli con fecha buena", 2020)
    conn.execute(
        "INSERT INTO entries (title_id, user_id, status, watched_at) VALUES (?, ?, 'watched', ?)",
        (title2["id"], user_id, "2024-05-01T00:00:00Z"),
    )

    years = repo.get_available_years(conn, user_id)

    assert years == [2024]


def test_duel_pool_falls_back_to_all_watched_when_no_anime(conn, user_id):
    """El usuario, 2026-09-18: 'el duelo era para anime pero para las otras personas no
    se como adaptarlo' - con menos de 2 titulos de anime, el duelo cae a TODO lo
    visto en vez de quedarse vacio."""
    for i, name in enumerate(["Breaking Bad", "Dark", "The Wire"]):
        title = repo.ensure_manual_title(conn, "show", name, 2020 + i)
        conn.execute("UPDATE titles SET original_language = 'en' WHERE id = ?", (title["id"],))
        conn.execute(
            "INSERT INTO entries (title_id, user_id, status) VALUES (?, ?, 'watched')", (title["id"], user_id)
        )

    ids, es_anime = repo.duel_pool_for_user(conn, user_id)

    assert es_anime is False
    assert len(ids) == 3


def test_duel_pool_uses_anime_when_there_is_enough(conn, user_id):
    for i, name in enumerate(["Frieren", "Steins;Gate"]):
        title = repo.ensure_manual_title(conn, "movie", name, 2020 + i)
        conn.execute("UPDATE titles SET original_language = 'ja' WHERE id = ?", (title["id"],))
        conn.execute(
            "INSERT INTO entries (title_id, user_id, status) VALUES (?, ?, 'watched')", (title["id"], user_id)
        )

    ids, es_anime = repo.duel_pool_for_user(conn, user_id)

    assert es_anime is True
    assert len(ids) == 2


def test_user_has_anime(conn, user_id):
    assert repo.user_has_anime(conn, user_id) is False

    title = repo.ensure_manual_title(conn, "movie", "Frieren", 2023)
    conn.execute("UPDATE titles SET original_language = 'ja' WHERE id = ?", (title["id"],))
    conn.execute(
        "INSERT INTO entries (title_id, user_id, status) VALUES (?, ?, 'watched')", (title["id"], user_id)
    )

    assert repo.user_has_anime(conn, user_id) is True


def test_show_anime_calendar_defaults_to_the_given_default(conn, user_id):
    """El usuario, 2026-09-18: 'deberia de haber una etiqueta en conf' - sin tocarlo
    nunca, manda el default (normalmente repo.user_has_anime)."""
    assert repo.get_show_anime_calendar(conn, user_id, default=True) is True
    assert repo.get_show_anime_calendar(conn, user_id, default=False) is False


def test_show_anime_calendar_explicit_choice_overrides_default(conn, user_id):
    repo.set_show_anime_calendar(conn, user_id, False)
    assert repo.get_show_anime_calendar(conn, user_id, default=True) is False

    repo.set_show_anime_calendar(conn, user_id, True)
    assert repo.get_show_anime_calendar(conn, user_id, default=False) is True


def test_record_duel_works_for_the_owner(conn, user_id):
    title_a = repo.ensure_manual_title(conn, "movie", "Peli A", 2020)
    title_b = repo.ensure_manual_title(conn, "movie", "Peli B", 2021)
    conn.execute("INSERT INTO entries (title_id, user_id, status) VALUES (?, ?, 'watched')", (title_a["id"], user_id))
    conn.execute("INSERT INTO entries (title_id, user_id, status) VALUES (?, ?, 'watched')", (title_b["id"], user_id))
    a = conn.execute("SELECT id, elo FROM entries WHERE title_id = ?", (title_a["id"],)).fetchone()
    b = conn.execute("SELECT id, elo FROM entries WHERE title_id = ?", (title_b["id"],)).fetchone()

    repo.record_duel(conn, "entries", a["id"], b["id"], user_id, 1.0)

    a_after = conn.execute("SELECT elo FROM entries WHERE id = ?", (a["id"],)).fetchone()
    assert a_after["elo"] != a["elo"]
