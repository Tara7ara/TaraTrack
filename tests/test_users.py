from app import repo


def test_hash_and_verify_password_roundtrip():
    password_hash, salt = repo.hash_password("correcto-caballo-batería")
    assert repo.verify_password("correcto-caballo-batería", password_hash, salt)
    assert not repo.verify_password("otra-cosa", password_hash, salt)


def test_hash_password_uses_a_fresh_salt_each_time():
    hash1, salt1 = repo.hash_password("misma-contraseña")
    hash2, salt2 = repo.hash_password("misma-contraseña")
    assert salt1 != salt2
    assert hash1 != hash2


def test_create_user_also_creates_default_favorites_list(conn):
    user = repo.create_user(conn, "hermana", "unaclave123")
    assert user["username"] == "hermana"
    assert user["is_admin"] == 0
    default_list = repo.get_default_list(conn, user["id"])
    assert default_list is not None
    assert default_list["name"] == "Favoritos"


def test_create_user_admin_flag(conn):
    admin = repo.create_user(conn, "amigo-admin", "unaclave123", is_admin=True)
    assert admin["is_admin"] == 1


def test_username_is_normalized_to_lowercase(conn):
    repo.create_user(conn, "  Amigo  ", "unaclave123")
    found = repo.get_user_by_username(conn, "AMIGO")
    assert found is not None
    assert found["username"] == "amigo"


def test_create_user_rejects_reserved_username(conn):
    """AGY, 2026-09-18: 'validacion profesional' - nombres reservados bloqueados
    para no confundir a nadie con una cuenta llamada "admin" que no es admin."""
    import pytest

    with pytest.raises(ValueError):
        repo.create_user(conn, "admin", "unaclave123")


def test_create_user_rejects_invalid_characters(conn):
    import pytest

    with pytest.raises(ValueError):
        repo.create_user(conn, "usuario raro!", "unaclave123")


def test_create_user_rejects_short_password(conn):
    """AGY, 2026-09-18: 'exigir minlength=8 tanto en /registro como en
    /ajustes/usuarios' - centralizado en create_user para que las dos vias compartan
    la misma regla."""
    import pytest

    with pytest.raises(ValueError):
        repo.create_user(conn, "amigo", "corta")


def test_duplicate_username_raises(conn):
    repo.create_user(conn, "duplicado", "unaclave123")
    import sqlite3

    import pytest

    with pytest.raises(sqlite3.IntegrityError):
        repo.create_user(conn, "duplicado", "otraclave")


def test_migration_bootstraps_admin_from_env(conn, user_id):
    """db._migrate_multiuser (llamada por init_db(), ver conftest.py) crea la
    primera cuenta a partir de TARATRACK_PASSWORD/TARATRACK_ADMIN_USERNAME - debe
    poder loguearse con esas mismas credenciales y quedar como admin."""
    admin = repo.get_user(conn, user_id)
    assert admin["username"] == "tara"
    assert admin["is_admin"] == 1
    assert repo.verify_password("test-password", admin["password_hash"], admin["password_salt"])


def test_ensure_entry_is_scoped_per_user(conn, user_id):
    """Bloqueador estructural resuelto en la Fase 1 del multiusuario: antes
    entries.title_id era UNIQUE en toda la instancia - dos usuarios no podian tener
    cada uno su propia entry para el mismo titulo."""
    other = repo.create_user(conn, "otro-usuario", "unaclave123")
    title = repo.ensure_manual_title(conn, "movie", "Compartida", 2020)

    entry_a = repo.ensure_entry(conn, title["tmdb_id"], "movie", user_id)
    entry_b = repo.ensure_entry(conn, title["tmdb_id"], "movie", other["id"])

    assert entry_a["id"] != entry_b["id"]
    assert entry_a["user_id"] == user_id
    assert entry_b["user_id"] == other["id"]


def test_create_list_is_scoped_per_user(conn, user_id):
    """Bloqueador estructural resuelto en la Fase 1: antes lists.name era UNIQUE
    global - el segundo usuario en crear "Top 10" se habria enganchado sin querer
    a la lista del primero."""
    other = repo.create_user(conn, "otro-usuario", "unaclave123")

    list_a = repo.create_list(conn, "Top 10", user_id)
    list_b = repo.create_list(conn, "Top 10", other["id"])

    assert list_a["id"] != list_b["id"]


def test_set_admin_promotes_and_demotes(conn, user_id):
    """Fase 5 (Tara, 2026-09-18: 'yo como administrador debería de poder poner
    admin a quien quiera')."""
    other = repo.create_user(conn, "amigo", "unaclave123")
    repo.set_admin(conn, other["id"], True)
    assert repo.get_user(conn, other["id"])["is_admin"] == 1

    repo.set_admin(conn, other["id"], False)
    assert repo.get_user(conn, other["id"])["is_admin"] == 0


def test_set_admin_refuses_to_remove_the_last_admin(conn, user_id):
    """No se puede dejar la instancia sin ningun admin - ni siquiera el propio
    admin quitandose el rol a si mismo."""
    import pytest

    with pytest.raises(ValueError):
        repo.set_admin(conn, user_id, False)
    assert repo.get_user(conn, user_id)["is_admin"] == 1


def test_set_admin_allows_demoting_when_another_admin_remains(conn, user_id):
    other = repo.create_user(conn, "amigo", "unaclave123", is_admin=True)
    repo.set_admin(conn, user_id, False)
    assert repo.get_user(conn, user_id)["is_admin"] == 0
    assert repo.get_user(conn, other["id"])["is_admin"] == 1


def test_set_username_changes_it(conn, user_id):
    repo.set_username(conn, user_id, "TaraNueva")
    assert repo.get_user(conn, user_id)["username"] == "taranueva"


def test_set_username_rejects_taken_name(conn, user_id):
    import pytest

    other = repo.create_user(conn, "amigo", "unaclave123")
    with pytest.raises(ValueError):
        repo.set_username(conn, user_id, "amigo")
    assert repo.get_user(conn, other["id"])["username"] == "amigo"


def test_set_username_allows_keeping_your_own_name(conn, user_id):
    """No debe reventar por 'ya existe' al re-guardar el mismo nombre que ya
    tenias (el check de duplicado excluye al propio usuario)."""
    repo.set_username(conn, user_id, "tara")
    assert repo.get_user(conn, user_id)["username"] == "tara"


def test_change_password_with_correct_old_password(conn, user_id):
    repo.change_password(conn, user_id, "test-password", "una-pass-nueva")
    user = repo.get_user(conn, user_id)
    assert repo.verify_password("una-pass-nueva", user["password_hash"], user["password_salt"])
    assert not repo.verify_password("test-password", user["password_hash"], user["password_salt"])


def test_change_password_rejects_wrong_old_password(conn, user_id):
    import pytest

    with pytest.raises(ValueError):
        repo.change_password(conn, user_id, "esta-mal", "una-pass-nueva")
    user = repo.get_user(conn, user_id)
    assert repo.verify_password("test-password", user["password_hash"], user["password_salt"])


def test_change_password_rejects_short_new_password(conn, user_id):
    import pytest

    with pytest.raises(ValueError):
        repo.change_password(conn, user_id, "test-password", "corta")


def test_admin_reset_password_does_not_need_the_old_one(conn, user_id):
    """Tara, 2026-09-18: 'si pierdo la pass como lo recupero, un fallo para el usr
    final' - a diferencia de change_password, esto es justo para cuando NO la
    tienes."""
    other = repo.create_user(conn, "amigo", "unaclave123")
    repo.admin_reset_password(conn, other["id"], "una-pass-nueva-cualquiera")
    refreshed = repo.get_user(conn, other["id"])
    assert repo.verify_password("una-pass-nueva-cualquiera", refreshed["password_hash"], refreshed["password_salt"])


def test_admin_reset_password_rejects_short_password(conn, user_id):
    import pytest

    other = repo.create_user(conn, "amigo", "unaclave123")
    with pytest.raises(ValueError):
        repo.admin_reset_password(conn, other["id"], "corta")


def test_set_avatar_path(conn, user_id):
    repo.set_avatar_path(conn, user_id, "/static/avatars/1.jpg")
    assert repo.get_user(conn, user_id)["avatar_path"] == "/static/avatars/1.jpg"


def test_reject_recommendation_is_scoped_per_user(conn, user_id):
    other = repo.create_user(conn, "otro-usuario", "unaclave123")

    repo.reject_recommendation(conn, 12345, "movie", "Alguna peli", None, user_id)
    repo.reject_recommendation(conn, 12345, "movie", "Alguna peli", None, other["id"])

    rows = conn.execute("SELECT user_id FROM rejected_recommendations WHERE tmdb_id = 12345").fetchall()
    assert {r["user_id"] for r in rows} == {user_id, other["id"]}
