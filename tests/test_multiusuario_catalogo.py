from datetime import date

from app import repo
from app.repo import recommendations as recs_mod
from app.routers import calendario


def test_marking_an_episode_creates_own_entry_when_title_is_already_in_catalog(conn, user_id):
    """Un título que ya tiene otro usuario no crea entry al abrir la ficha: marcar un
    episodio tiene que crearla, o la serie no llega a Continuar viendo ni a /vistas."""
    other = repo.create_user(conn, "amigo", "unaclave123")
    title = repo.ensure_manual_title(conn, "show", "Serie compartida", 2020)
    conn.execute(
        "INSERT INTO entries (title_id, user_id, status) VALUES (?, ?, 'pending')", (title["id"], user_id)
    )
    conn.execute("INSERT INTO episodes (title_id, season_number, episode_number) VALUES (?, 1, 1)", (title["id"],))
    ep = conn.execute("SELECT id FROM episodes WHERE title_id = ?", (title["id"],)).fetchone()

    repo.toggle_episode(conn, ep["id"], other["id"])

    theirs = conn.execute(
        "SELECT status FROM entries WHERE title_id = ? AND user_id = ?", (title["id"], other["id"])
    ).fetchone()
    mine = conn.execute(
        "SELECT status FROM entries WHERE title_id = ? AND user_id = ?", (title["id"], user_id)
    ).fetchone()
    assert theirs["status"] == "watched"
    assert mine["status"] == "pending"


def test_removing_last_pending_cleans_calendar_links(conn, user_id):
    title = repo.ensure_manual_title(conn, "show", "Desde el calendario", 2020)
    conn.execute("INSERT INTO calendar_links (anilist_id, title_id) VALUES (4242, ?)", (title["id"],))
    conn.execute(
        "INSERT INTO entries (title_id, user_id, status) VALUES (?, ?, 'pending')", (title["id"], user_id)
    )
    entry = conn.execute("SELECT id FROM entries WHERE title_id = ?", (title["id"],)).fetchone()

    repo.remove_pending_entry(conn, entry["id"])

    assert conn.execute("SELECT count(*) FROM titles WHERE id = ?", (title["id"],)).fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM calendar_links").fetchone()[0] == 0


def test_waifu_match_uses_romaji_and_english_titles(conn, user_id):
    title = repo.ensure_manual_title(conn, "show", "Ataque a los titanes", 2013)
    conn.execute(
        "UPDATE titles SET anilist_title_romaji = 'Shingeki no Kyojin', anilist_title_english = 'Attack on Titan' WHERE id = ?",
        (title["id"],),
    )
    conn.execute(
        "INSERT INTO entries (title_id, user_id, status) VALUES (?, ?, 'watched')", (title["id"], user_id)
    )

    assert repo.match_library_title(conn, ["Shingeki no Kyojin"], user_id)["title_id"] == title["id"]
    assert repo.match_library_title(conn, ["Attack on Titan Season 2"], user_id)["title_id"] == title["id"]


def test_december_is_next_years_winter(monkeypatch):
    class Dic(date):
        @classmethod
        def today(cls):
            return date(2026, 12, 5)

    monkeypatch.setattr(calendario, "date", Dic)
    assert calendario._current_season() == ("WINTER", 2027)


def test_seeds_with_commas_survive_the_cache():
    semillas = ["Kaguya-sama: Love Is War, Ultra Romantic", "Frieren"]
    import json

    assert recs_mod._load_seeds(json.dumps(semillas)) == semillas
    assert recs_mod._load_seeds("Frieren,Bleach") == ["Frieren", "Bleach"]


def test_reset_elo_puts_unrated_entries_back_to_1500(conn, user_id):
    title = repo.ensure_manual_title(conn, "show", "Sin nota", 2020)
    conn.execute(
        "INSERT INTO entries (title_id, user_id, status, elo, rd) VALUES (?, ?, 'watched', 1720, 90)",
        (title["id"], user_id),
    )
    entry = conn.execute("SELECT id FROM entries WHERE title_id = ?", (title["id"],)).fetchone()

    repo.reset_elo(conn, "entries", [entry["id"]], user_id)

    row = conn.execute("SELECT elo, rd FROM entries WHERE id = ?", (entry["id"],)).fetchone()
    assert row["elo"] == 1500
    assert row["rd"] > 90


def test_specials_do_not_keep_a_show_in_continue_watching(conn, user_id):
    title = repo.ensure_manual_title(conn, "show", "Con especiales", 2020)
    conn.execute(
        "INSERT INTO entries (title_id, user_id, status) VALUES (?, ?, 'watched')", (title["id"], user_id)
    )
    for season, number in ((0, 1), (1, 1)):
        conn.execute(
            "INSERT INTO episodes (title_id, season_number, episode_number, air_date) VALUES (?, ?, ?, '2020-01-01')",
            (title["id"], season, number),
        )
    regular = conn.execute(
        "SELECT id FROM episodes WHERE title_id = ? AND season_number = 1", (title["id"],)
    ).fetchone()
    repo.toggle_episode(conn, regular["id"], user_id)

    assert all(c["title_id"] != title["id"] for c in repo.list_continue_watching(conn, user_id))


def test_remove_list_item_only_touches_that_list(conn, user_id):
    lista = repo.create_list(conn, "Romance", user_id)
    otra = repo.create_list(conn, "Otra", user_id)
    entry = repo.ensure_entry(conn, repo.ensure_manual_title(conn, "show", "Por error", 2020)["tmdb_id"], "show", user_id)
    repo.add_entry_to_list(conn, lista["id"], entry["id"])
    repo.add_entry_to_list(conn, otra["id"], entry["id"])
    item = conn.execute("SELECT id FROM list_items WHERE list_id = ?", (lista["id"],)).fetchone()

    repo.remove_list_item(conn, otra["id"], item["id"])
    assert len(repo.list_items_in_list(conn, lista["id"])) == 1

    repo.remove_list_item(conn, lista["id"], item["id"])
    assert repo.list_items_in_list(conn, lista["id"]) == []
    assert len(repo.list_items_in_list(conn, otra["id"])) == 1


def test_delete_list_cleans_its_duels(conn, user_id):
    lista = repo.create_list(conn, "Para borrar", user_id)
    for name in ("A", "B"):
        entry = repo.ensure_entry(conn, repo.ensure_manual_title(conn, "show", name, 2020)["tmdb_id"], "show", user_id)
        repo.add_entry_to_list(conn, lista["id"], entry["id"])
    a, b = [r["id"] for r in conn.execute("SELECT id FROM list_items WHERE list_id = ?", (lista["id"],))]
    repo.record_duel(conn, "list_items", a, b, user_id)
    assert conn.execute("SELECT count(*) FROM duels").fetchone()[0] == 1

    repo.delete_list(conn, lista["id"])

    assert conn.execute("SELECT count(*) FROM duels").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM elo_snapshots WHERE table_name = 'list_items'").fetchone()[0] == 0


def test_season_rating_reseeds_elo(conn, user_id, monkeypatch):
    llamadas = []
    monkeypatch.setattr("app.repo.titles.reseed_undueled_entries_elo", lambda c, uid: llamadas.append(uid))
    entry = repo.ensure_entry(conn, repo.ensure_manual_title(conn, "show", "Por temporadas", 2020)["tmdb_id"], "show", user_id)

    repo.set_season_rating(conn, entry["id"], 1, 8.0, "bien")

    assert llamadas == [user_id]


def test_empty_recommendations_are_not_recomputed_on_every_visit(conn, user_id, monkeypatch):
    llamadas = []
    monkeypatch.setattr(recs_mod, "refresh_recommendations_cache", lambda user_id: llamadas.append(user_id))

    repo.list_recommendations(conn, user_id)
    repo.list_recommendations(conn, user_id)

    assert llamadas == [user_id]


def test_unfavoriting_cleans_its_duels(conn, user_id):
    fav = repo.get_or_create_default_list(conn, user_id)
    ids = []
    for name in ("A", "B"):
        entry = repo.ensure_entry(conn, repo.ensure_manual_title(conn, "show", name, 2020)["tmdb_id"], "show", user_id)
        repo.toggle_list_item(conn, fav["id"], entry["id"])
        ids.append(entry["id"])
    a, b = [r["id"] for r in conn.execute("SELECT id FROM list_items WHERE list_id = ?", (fav["id"],))]
    repo.record_duel(conn, "list_items", a, b, user_id)

    repo.toggle_list_item(conn, fav["id"], ids[0])

    assert conn.execute("SELECT count(*) FROM duels").fetchone()[0] == 0


def test_stats_average_ignores_quick_fives(conn, user_id):
    for name, rating, comment in (("A", 9.0, "genial"), ("B", 5.0, "-")):
        t = repo.ensure_manual_title(conn, "show", name, 2020)
        conn.execute(
            "INSERT INTO entries (title_id, user_id, status, rating, comment) VALUES (?, ?, 'watched', ?, ?)",
            (t["id"], user_id, rating, comment),
        )
    totals = repo.get_stats(conn, user_id)["totals"]
    assert totals["avg_rating"] == 9.0 and totals["rated"] == 1


def test_prediction_with_all_weights_at_zero_does_not_crash():
    from app.repo import affinity

    weights = {k: 0.0 for k in affinity.PREDICT_SIGNAL_WEIGHTS}
    profile = {
        "predict_weights": weights, "last_rating_by_id": {}, "rating_by_id": {},
        "studio_avg": {"MAPPA": 80.0}, "tag_avg": {}, "tag_idf": {}, "genre_avg": {"Acción": 70.0},
    }
    item = {"studio": "MAPPA", "genres": ["Acción"], "tags": []}
    assert affinity._raw_predict_score(item, profile) == (None, 0.0)


def test_affinity_form_rejects_negative_and_nan_weights():
    from app.routers.ajustes import _peso

    assert _peso("0", 1.0) == 0.0
    assert _peso("-3", 1.0) == 1.0
    assert _peso("nan", 1.0) == 1.0
    assert _peso("inf", 1.0) == 1.0
    assert _peso("2,5", 1.0) == 1.0


def test_impossible_anilist_start_date_has_no_weekday():
    from app import anime

    assert anime._start_weekday({"year": 2026, "month": 6, "day": 31}) is None
    assert anime._start_weekday({"year": 2026, "month": 10, "day": 2}) == 4
    assert anime._start_weekday(None) is None
