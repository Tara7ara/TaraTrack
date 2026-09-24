"""Marca de "ya en tu lista" en el calendario de temporada, tira de estado de la ficha,
personajes de anime en vez de actores de voz y filtro por tags de /pendientes."""
from app import anime, repo, tmdb
from app.routers.pendientes import _matches_tags, _parse_tags
from app.web import show_status_ribbon


def _title(conn, tmdb_id, title, **cols):
    cols = {"type": "show", "genres": "Animación", "original_language": "ja", **cols}
    names = ", ".join(["tmdb_id", "title", *cols])
    conn.execute(
        f"INSERT INTO titles ({names}) VALUES ({', '.join('?' * (len(cols) + 2))})",
        (tmdb_id, title, *cols.values()),
    )
    return conn.execute("SELECT * FROM titles WHERE tmdb_id = ?", (tmdb_id,)).fetchone()


def _entry(conn, title_row, user_id, status="pending"):
    cur = conn.execute(
        "INSERT INTO entries (title_id, user_id, status) VALUES (?, ?, ?)",
        (title_row["id"], user_id, status),
    )
    return cur.lastrowid


# --- calendario de temporada -------------------------------------------------

def test_calendar_cards_know_what_is_already_in_library(conn, user_id):
    frieren = _title(conn, 1, "Frieren", anilist_id=154587)
    grand_blue = _title(conn, 2, "Grand Blue", original_title="ぐらんぶる")
    kaiju = _title(conn, 3, "Kaiju No. 8")
    _entry(conn, frieren, user_id)
    _entry(conn, grand_blue, user_id, status="watched")
    _entry(conn, kaiju, user_id)
    repo.link_calendar_card(conn, 999, 3, "show")  # tarjeta "Kaiju No. 8 Season 2"

    items = [
        {"anilist_id": 154587, "title": "Frieren: Beyond Journey's End"},
        {"anilist_id": 111, "title": "Grand Blue Season 3", "title_romaji": "ぐらんぶる"},
        {"anilist_id": 999, "title": "Kaiju No. 8 Season 2"},
        {"anilist_id": 555, "title": "Algo que no sigo"},
    ]
    status = repo.library_status_for_cards(conn, user_id, items)

    assert status == {154587: "pending", 111: "watched", 999: "pending"}


def test_calendar_cards_ignore_other_users_library(conn, user_id):
    other = repo.create_user(conn, "amigo", "unaclave123")
    _entry(conn, _title(conn, 1, "Frieren", anilist_id=154587), other["id"])

    status = repo.library_status_for_cards(conn, user_id, [{"anilist_id": 154587, "title": "Frieren"}])

    assert status == {}


# --- tira de estado ------------------------------------------------------------

def test_status_ribbon_uses_next_episode_for_airing():
    base = {"type": "show", "next_episode_air_date": None}
    assert show_status_ribbon({**base, "show_status": "Ended"}) == ("Finalizada", "ribbon-ended")
    assert show_status_ribbon({**base, "show_status": "Canceled"}) == ("Cancelada", "ribbon-canceled")
    assert show_status_ribbon({**base, "show_status": "Returning Series"})[1] == "ribbon-returning"
    airing = {**base, "show_status": "Returning Series", "next_episode_air_date": "2026-09-29"}
    assert show_status_ribbon(airing) == ("En emisión", "ribbon-airing")
    assert show_status_ribbon({**base, "type": "movie", "show_status": "Released"}) is None


# --- personajes ----------------------------------------------------------------

def test_anime_never_falls_back_to_voice_actors(conn, monkeypatch):
    title = _title(conn, 10, "Wistoria: varita y espada")
    monkeypatch.setattr(anime, "get_characters", lambda *a, **k: [])
    monkeypatch.setattr(tmdb, "get_credits", lambda *a: [
        {"tmdb_person_id": 1, "name": "Actor", "character_name": "Will (voice)", "profile_path": None}
    ])

    repo.sync_characters(conn, title)

    assert conn.execute("SELECT count(*) FROM characters").fetchone()[0] == 0


def test_western_animation_keeps_tmdb_cast(conn, monkeypatch):
    title = _title(conn, 11, "Hey Duggee", original_language="en")
    monkeypatch.setattr(tmdb, "get_credits", lambda *a: [
        {"tmdb_person_id": 1, "name": "Actor", "character_name": "Duggee (voice)", "profile_path": None}
    ])

    repo.sync_characters(conn, title)

    assert conn.execute("SELECT count(*) FROM characters").fetchone()[0] == 1


def test_anilist_lookup_tries_id_then_cleaned_original_title(monkeypatch):
    calls = []

    def fake(query=None, anilist_id=None):
        calls.append(anilist_id or query)
        return [{"id": 1, "name": "Reze", "image_url": None}] if query == "チェンソーマン レゼ篇" else []

    monkeypatch.setattr(anime, "get_characters", fake)
    from app.repo.characters import _anilist_characters
    row = {"anilist_id": 42, "original_title": "劇場版 チェンソーマン レゼ篇", "title": "Chainsaw Man - La película"}

    class Row(dict):
        def keys(self):
            return super().keys()

    assert _anilist_characters(Row(row))[0]["name"] == "Reze"
    assert calls[:3] == [42, "劇場版 チェンソーマン レゼ篇", "チェンソーマン レゼ篇"]


def test_voice_cast_heals_on_visit_and_keeps_favorites(conn, user_id, monkeypatch):
    title = _title(conn, 12, "Masamune-kun's Revenge")
    entry_id = _entry(conn, title, user_id, status="watched")
    conn.executemany(
        "INSERT INTO characters (title_id, tmdb_person_id, name, character_name) VALUES (?, ?, ?, ?)",
        [(title["id"], 1, "Actor A", "Masamune (voice)"), (title["id"], 2, "Actor B", "Aki (voice)")],
    )
    fav_id = conn.execute("SELECT id FROM characters WHERE tmdb_person_id = 2").fetchone()[0]
    repo.toggle_favorite_character(conn, entry_id, fav_id)
    monkeypatch.setattr(anime, "get_characters", lambda *a, **k: [
        {"id": 500, "name": "Masamune Makabe", "image_url": None},
        {"id": 501, "name": "Aki Adagaki", "image_url": None},
    ])

    repo.sync_characters(conn, title)

    names = {r[0] for r in conn.execute("SELECT name FROM characters WHERE title_id = ?", (title["id"],))}
    assert names == {"Masamune Makabe", "Aki Adagaki", "Actor B"}  # el favorito no se toca
    assert conn.execute("SELECT count(*) FROM favorite_characters").fetchone()[0] == 1


def test_reload_characters_keeps_favorites(conn, user_id, monkeypatch):
    title = _title(conn, 13, "Frieren")
    entry_id = _entry(conn, title, user_id, status="watched")
    conn.execute(
        "INSERT INTO characters (title_id, tmdb_person_id, name, character_name) VALUES (?, 7, 'Fern', 'Fern')",
        (title["id"],),
    )
    fern = conn.execute("SELECT id FROM characters WHERE name = 'Fern'").fetchone()[0]
    repo.toggle_favorite_character(conn, entry_id, fern)
    monkeypatch.setattr(anime, "get_characters", lambda *a, **k: [
        {"id": 7, "name": "Fern", "image_url": None}, {"id": 8, "name": "Stark", "image_url": None},
    ])

    repo.resync_characters(conn, title)

    assert conn.execute("SELECT count(*) FROM favorite_characters").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM characters WHERE title_id = ?", (title["id"],)).fetchone()[0] == 2


# --- filtro por tags -------------------------------------------------------------

def test_tag_filter_include_and_exclude():
    romance = {"anilist_genres": "Romance,Fantasy", "anilist_tags": "Female Protagonist"}
    harem = {"anilist_genres": "Romance,Fantasy", "anilist_tags": "Harem,Isekai"}
    sin_tags = {"anilist_genres": None, "anilist_tags": None}
    tokens = _parse_tags(" fantasy , -Harem,, -")

    assert tokens == ["fantasy", "-Harem"]
    assert _matches_tags(romance, tokens)
    assert not _matches_tags(harem, tokens)
    assert not _matches_tags(sin_tags, tokens)
    assert _matches_tags(sin_tags, [])


# --- estados de temporada, tira de estado en listados e icono de la PWA --------------

def test_calendar_matches_next_season_by_anilist_names(conn, user_id):
    blue_box = _title(conn, 20, "La caja azul", original_title="アオのハコ",
                      anilist_title_romaji="Ao no Hako", anilist_title_english="Blue Box")
    _entry(conn, blue_box, user_id)

    status = repo.library_status_for_cards(
        conn, user_id, [{"anilist_id": 189123, "title": "Blue Box Season 2", "title_romaji": "Ao no Hako Season 2"}]
    )

    assert status == {189123: "pending"}


def test_status_ribbon_premiere_and_pause():
    base = {"type": "show", "show_status": "Returning Series"}
    premiere = {**base, "next_episode_air_date": "2026-10-04", "next_episode_label": "T2E1 — Episodio 1"}
    weekly = {**base, "next_episode_air_date": "2026-10-04", "next_episode_label": "T2E11 — Episodio 11"}
    assert show_status_ribbon(premiere)[0] == "Próximamente"
    assert show_status_ribbon(weekly)[0] == "En emisión"
    assert show_status_ribbon({**base, "next_episode_air_date": None, "next_episode_label": None})[0] == "En pausa"


def test_sparse_anime_cast_is_topped_up_once(conn, user_id, monkeypatch):
    title = _title(conn, 21, "My Dress-Up Darling")
    entry_id = _entry(conn, title, user_id, status="watched")
    conn.execute(
        "INSERT INTO characters (title_id, tmdb_person_id, name, character_name) VALUES (?, 1, 'Marin', 'Marin')",
        (title["id"],),
    )
    marin = conn.execute("SELECT id FROM characters WHERE name = 'Marin'").fetchone()[0]
    repo.toggle_favorite_character(conn, entry_id, marin)
    calls = []

    def fake(*a, **k):
        calls.append(1)
        return [{"id": 1, "name": "Marin", "image_url": None}, {"id": 2, "name": "Gojo", "image_url": None}]

    monkeypatch.setattr(anime, "get_characters", fake)

    repo.sync_characters(conn, title)
    repo.sync_characters(conn, title)  # segunda visita: ya no vuelve a pedir

    assert conn.execute("SELECT count(*) FROM characters WHERE title_id = ?", (title["id"],)).fetchone()[0] == 2
    assert conn.execute("SELECT count(*) FROM favorite_characters").fetchone()[0] == 1
    assert len(calls) == 1
