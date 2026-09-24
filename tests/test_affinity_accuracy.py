from app import repo
from app.repo import affinity


def _rated_title(conn, name, anilist_id, genre, user_id, rating, comment=None):
    title = repo.ensure_manual_title(conn, "movie", name, 2020)
    conn.execute(
        "UPDATE titles SET anilist_id = ?, original_language = 'ja', anilist_genres = ? WHERE id = ?",
        (anilist_id, genre, title["id"]),
    )
    conn.execute(
        "INSERT INTO entries (title_id, user_id, status, rating, comment) VALUES (?, ?, 'watched', ?, ?)",
        (title["id"], user_id, rating, comment),
    )
    return title


def test_no_me_acuerdo_filler_is_not_a_rating_for_the_engine(conn, user_id):
    """El atajo "No me acuerdo, 5 y ya" (5.0 + comentario "-") no es una opinion -
    el motor lo trata como sin nota; un 5.0 con comentario real si cuenta."""
    _rated_title(conn, "Relleno", 111, "Comedy", user_id, 5.0, "-")
    _rated_title(conn, "Cinco de verdad", 222, "Drama", user_id, 5.0, "meh, floja")

    raw = affinity._load_affinity_raw(conn, user_id)
    ratings = {t["anilist_id"]: t["rating"] for t in raw["titles"]}
    assert ratings[111] is None
    assert ratings[222] == 5.0

    repo.recompute_taste_profile(conn, user_id)
    profile = repo.build_taste_profile(conn, user_id)
    assert 111 not in profile["rating_by_id"]
    assert profile["rating_by_id"][222] == 5.0


def test_low_confidence_attribute_shrinks_to_neutral_not_zero(conn, user_id):
    """Un genero con un solo 10/10 se encoge hacia el neutro (50), no hacia 0 - antes
    la afinidad se multiplicaba por la confianza (1/(1+3) = 0.25 con un titulo), asi
    que no podia pasar de 25 por mucho que ese titulo fuera un 10."""
    _rated_title(conn, "Unica joya", 111, "Fantasy", user_id, 10.0)
    for i in range(6):
        _rated_title(conn, f"Relleno {i}", 200 + i, "Action", user_id, 5.0 + i * 0.5)

    raw = affinity._load_affinity_raw(conn, user_id)
    rows = affinity._aggregate_affinity(raw, repo.get_affinity_config(conn, user_id))
    fantasy = next(r for (kind, name), r in rows.items() if name == "Fantasy")
    assert fantasy["n_titulos"] == 1
    assert fantasy["afinidad"] > 25.0
    assert abs(fantasy["afinidad"] - 50.0) < 25.0


def test_spearman_basics():
    assert affinity._spearman([1, 2, 3, 4], [10, 20, 30, 40]) == 1.0
    assert affinity._spearman([1, 2, 3, 4], [40, 30, 20, 10]) == -1.0
    assert affinity._spearman([1, 2], [1, 2]) is None


def test_recompute_saves_one_accuracy_point_per_day(conn, user_id):
    for i in range(5):
        _rated_title(conn, f"T{i}", 100 + i, "Comedy" if i % 2 else "Drama", user_id, 4.0 + i * 1.5)

    repo.recompute_taste_profile(conn, user_id)
    repo.recompute_taste_profile(conn, user_id)

    history = repo.get_accuracy_history(conn, user_id)
    assert len(history) == 1
    point = history[0]
    assert point["n"] == 5
    assert point["masterpieces"] == 1  # solo el 10.0 llega a 9.5
    assert 0 <= point["masterpieces_hit"] <= point["masterpieces"]


def test_spearman_band_contains_value_and_is_stable():
    pairs = [(i + (i % 7) * 3.0, i / 10) for i in range(60)]
    rho = affinity._spearman([s for s, _ in pairs], [r for _, r in pairs])
    lo, hi = affinity._spearman_band(pairs)
    assert lo <= rho <= hi
    assert affinity._spearman_band(pairs) == (lo, hi)  # semilla fija
