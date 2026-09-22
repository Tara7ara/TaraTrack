from app import repo


def _insert_cache_row(conn, user_id, tmdb_id, title="Rec de prueba"):
    conn.execute(
        """INSERT INTO recommendations_cache
           (user_id, tmdb_id, type, title, year, poster_url, overview, vote_average,
            popularity, is_adult, seeds, score)
           VALUES (?, ?, 'show', ?, 2020, NULL, NULL, 8.0, 10.0, 0, 'Semilla', 1.0)""",
        (user_id, tmdb_id, title),
    )


def test_list_recommendations_only_sees_own_cache(conn, user_id):
    """Bug real (2026-09-18, el usuario: 'recomienda full anime a la cuenta random'):
    recommendations_cache era una unica tabla global calculada de las entries de
    TODA la instancia - una cuenta nueva veia los recomendados de otra."""
    other = repo.create_user(conn, "amigo", "unaclave123")
    _insert_cache_row(conn, user_id, 111, "Solo de tara")
    _insert_cache_row(conn, other["id"], 222, "Solo del amigo")

    mine = repo.list_recommendations(conn, user_id)
    theirs = repo.list_recommendations(conn, other["id"])

    assert [r["tmdb_id"] for r in mine] == [111]
    assert [r["tmdb_id"] for r in theirs] == [222]


def test_refresh_with_no_seeds_only_clears_own_cache(conn, user_id):
    """Un usuario sin nada puntuado (8.5+) ni en Favoritos se queda sin recomendados
    (correcto - no hay de donde tirar), pero eso no debe tocar la cache de otro."""
    other = repo.create_user(conn, "amigo", "unaclave123")
    _insert_cache_row(conn, other["id"], 333, "Del amigo")
    conn.commit()

    # refresh_recommendations_cache abre su propia conexion (la usa tambien la sync de
    # fondo, sin un `conn` a mano) - hay que confirmar la escritura de arriba en la
    # conexion del fixture antes, o la segunda conexion se queda esperando el
    # busy_timeout entero contra una transaccion abierta y sin confirmar.
    repo.refresh_recommendations_cache(user_id=user_id)

    assert repo.list_recommendations(conn, user_id) == []
    theirs = conn.execute(
        "SELECT tmdb_id FROM recommendations_cache WHERE user_id = ?", (other["id"],)
    ).fetchall()
    assert [r["tmdb_id"] for r in theirs] == [333]


def test_known_titles_are_scoped_per_user_not_the_whole_catalog(conn, user_id):
    """Bug real critico (AGY, 2026-09-18): "known" comprobaba TODO el catalogo
    compartido (titles), no lo que ESTE usuario tiene en su biblioteca. Con el
    catalogo del usuario lleno de cientos de titulos, una cuenta nueva se quedaba sin
    poder recibir NINGUNO de ellos como recomendacion, solo por existir en `titles`,
    aunque esa cuenta nunca los hubiera visto."""
    other = repo.create_user(conn, "amigo", "unaclave123")
    # El usuario tiene Frieren en su biblioteca (existe en el catalogo compartido `titles`).
    title = repo.ensure_manual_title(conn, "show", "Frieren", 2023)
    conn.execute(
        "INSERT INTO entries (title_id, user_id, status) VALUES (?, ?, 'watched')", (title["id"], user_id)
    )
    # El amigo NO la tiene - pre-poblamos su cache con Frieren como recomendacion.
    _insert_cache_row(conn, other["id"], title["tmdb_id"], "Frieren")

    theirs = repo.list_recommendations(conn, other["id"])

    assert [r["tmdb_id"] for r in theirs] == [title["tmdb_id"]]


def test_rejecting_a_recommendation_does_not_hide_it_for_other_users(conn, user_id):
    other = repo.create_user(conn, "amigo", "unaclave123")
    _insert_cache_row(conn, user_id, 444, "Compartido en cache pero rechazado por tara")
    _insert_cache_row(conn, other["id"], 444, "Compartido en cache pero rechazado por tara")

    repo.reject_recommendation(conn, 444, "show", "Titulo", None, user_id)

    assert repo.list_recommendations(conn, user_id) == []
    assert [r["tmdb_id"] for r in repo.list_recommendations(conn, other["id"])] == [444]
