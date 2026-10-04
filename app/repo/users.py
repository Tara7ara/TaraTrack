"""app.repo.users - cuentas de usuario.

Contraseñas con hashlib.pbkdf2_hmac y salt aleatorio por usuario, sin dependencias
extra."""
import hashlib
import hmac
import os
import re
import secrets
from datetime import datetime, timedelta, timezone

_PBKDF2_ITERATIONS = 200_000

USERNAME_REGEX = re.compile(r"^[a-z0-9_-]{1,20}$")
RESERVED_USERNAMES = frozenset({"admin", "administrador", "sistema", "root", "null", "api", "taratrack"})


def validate_username(username: str) -> str:
    norm = (username or "").strip().lower()
    if not USERNAME_REGEX.match(norm):
        raise ValueError("El usuario debe tener de 1 a 20 caracteres: minúsculas, números, guiones o guion bajo")
    if norm in RESERVED_USERNAMES:
        raise ValueError("Ese nombre de usuario está reservado")
    return norm


def hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    """Devuelve (hash_hex, salt_hex). Genera un salt nuevo si no se pasa uno
    (alta de usuario); se pasa el salt guardado al verificar un login."""
    salt = salt or os.urandom(16).hex()
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), _PBKDF2_ITERATIONS)
    return digest.hex(), salt


def verify_password(password: str, password_hash: str, password_salt: str) -> bool:
    candidate, _ = hash_password(password, password_salt)
    return hmac.compare_digest(candidate, password_hash)


_DUMMY_CREDENTIALS: tuple[str, str] | None = None


def burn_password_check(password: str) -> None:
    """PBKDF2 contra un hash de relleno cuando el usuario no existe o no tiene
    invitación, para que el tiempo de respuesta no revele qué cuentas existen."""
    global _DUMMY_CREDENTIALS
    if _DUMMY_CREDENTIALS is None:
        _DUMMY_CREDENTIALS = hash_password(os.urandom(16).hex())
    verify_password(password, *_DUMMY_CREDENTIALS)


def create_user(conn, username: str, password: str, is_admin: bool = False):
    """Crea la cuenta y su propia lista "Favoritos" (mismo default que ya tenia
    la app de siempre, ahora por-usuario en vez de una unica global) - para que
    un usuario nuevo no aterrice sin ningun sitio donde guardar favoritos."""
    username = validate_username(username)
    # Mínimo de 8 caracteres, compartido por todas las vías de alta.
    if len(password) < 8:
        raise ValueError("La contraseña debe tener al menos 8 caracteres")
    password_hash, password_salt = hash_password(password)
    cur = conn.execute(
        """INSERT INTO users (username, password_hash, password_salt, is_admin, comments_seen_at)
           VALUES (?, ?, ?, ?, datetime('now'))""",
        (username, password_hash, password_salt, int(is_admin)),
    )
    user_id = cur.lastrowid
    conn.execute(
        "INSERT INTO lists (user_id, name, is_default) VALUES (?, 'Favoritos', 1)", (user_id,)
    )
    return get_user(conn, user_id)


def get_user(conn, user_id: int):
    return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def get_user_by_username(conn, username: str):
    return conn.execute(
        "SELECT * FROM users WHERE username = ?", (username.strip().lower(),)
    ).fetchone()


def list_users(conn):
    return conn.execute(
        """SELECT id, username, is_admin, avatar_path, created_at, is_blocked,
                  invite_code_hash IS NOT NULL AS invite_pending, invite_expires_at
           FROM users ORDER BY created_at"""
    ).fetchall()


INVITE_DAYS = 7
_INVITE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # sin 0/O ni 1/I/L


def _normalize_code(code: str) -> str:
    return "".join(ch for ch in (code or "").upper() if ch.isalnum())


def _set_invite(conn, user_id: int) -> str:
    raw = "".join(secrets.choice(_INVITE_ALPHABET) for _ in range(10))
    code_hash, code_salt = hash_password(raw)
    # La contraseña anterior deja de valer: una cuenta con invitacion pendiente
    # solo se puede activar con el codigo.
    dead_hash, dead_salt = hash_password(secrets.token_hex(32))
    expires = (datetime.now(timezone.utc) + timedelta(days=INVITE_DAYS)).isoformat(timespec="seconds")
    conn.execute(
        """UPDATE users SET invite_code_hash = ?, invite_expires_at = ?,
                  password_hash = ?, password_salt = ? WHERE id = ?""",
        (f"{code_salt}${code_hash}", expires, dead_hash, dead_salt, user_id),
    )
    return f"{raw[:5]}-{raw[5:]}"


def create_invited_user(conn, username: str, is_admin: bool = False):
    """Crea la cuenta sin contraseña utilizable y devuelve (usuario, codigo). El
    codigo solo se devuelve aqui: en la BBDD queda su hash."""
    username = validate_username(username)
    if get_user_by_username(conn, username):
        raise ValueError("Ese usuario ya existe")
    user = create_user(conn, username, secrets.token_hex(32), is_admin=is_admin)
    code = _set_invite(conn, user["id"])
    return get_user(conn, user["id"]), code


def regenerate_invite(conn, user_id: int) -> str:
    if not get_user(conn, user_id):
        raise ValueError("Usuario no encontrado")
    return _set_invite(conn, user_id)


def activate_invite(conn, username: str, code: str, password: str, password2: str):
    """Canjea el codigo: pone la contraseña elegida y borra la invitacion. Mismo
    error para usuario inexistente y codigo incorrecto (no dar pistas)."""
    user = get_user_by_username(conn, username or "")
    stored = user["invite_code_hash"] if user else None
    ok = False
    if stored and "$" in stored:
        salt, expected = stored.split("$", 1)
        ok = verify_password(_normalize_code(code), expected, salt)
    else:
        burn_password_check(_normalize_code(code))
    if not ok:
        raise ValueError("Usuario o código no válidos")
    if user["is_blocked"]:
        raise ValueError("Esta cuenta está bloqueada")
    if user["invite_expires_at"] and datetime.fromisoformat(user["invite_expires_at"]) < datetime.now(timezone.utc):
        raise ValueError("El código ha caducado. Pide uno nuevo a quien te invitó")
    if len(password) < 8:
        raise ValueError("La contraseña debe tener al menos 8 caracteres")
    if password != password2:
        raise ValueError("Las contraseñas no coinciden")
    new_hash, new_salt = hash_password(password)
    conn.execute(
        """UPDATE users SET password_hash = ?, password_salt = ?,
                  invite_code_hash = NULL, invite_expires_at = NULL WHERE id = ?""",
        (new_hash, new_salt, user["id"]),
    )
    return get_user(conn, user["id"])


def set_username(conn, user_id: int, new_username: str):
    """Cambia el nombre de usuario, con la misma validación que el alta."""
    new_username = validate_username(new_username)
    existing = get_user_by_username(conn, new_username)
    if existing and existing["id"] != user_id:
        raise ValueError("Ese usuario ya existe")
    conn.execute("UPDATE users SET username = ? WHERE id = ?", (new_username, user_id))


def set_avatar_path(conn, user_id: int, avatar_path: str | None):
    conn.execute("UPDATE users SET avatar_path = ? WHERE id = ?", (avatar_path, user_id))


def change_password(conn, user_id: int, old_password: str, new_password: str):
    """Cambia la propia contraseña; pide la actual."""
    user = get_user(conn, user_id)
    if not user:
        raise ValueError("Usuario no encontrado")
    if not verify_password(old_password, user["password_hash"], user["password_salt"]):
        raise ValueError("La contraseña actual no es correcta")
    if len(new_password) < 8:
        raise ValueError("La nueva contraseña debe tener al menos 8 caracteres")
    new_hash, new_salt = hash_password(new_password)
    conn.execute(
        "UPDATE users SET password_hash = ?, password_salt = ? WHERE id = ?",
        (new_hash, new_salt, user_id),
    )


def admin_reset_password(conn, user_id: int, new_password: str):
    """Un admin pone una contraseña nueva a quien la haya perdido (no hay email en un
    self-host pequeño). A diferencia de change_password, no pide la actual."""
    user = get_user(conn, user_id)
    if not user:
        raise ValueError("Usuario no encontrado")
    if len(new_password) < 8:
        raise ValueError("La nueva contraseña debe tener al menos 8 caracteres")
    new_hash, new_salt = hash_password(new_password)
    conn.execute(
        "UPDATE users SET password_hash = ?, password_salt = ? WHERE id = ?",
        (new_hash, new_salt, user_id),
    )


def count_admins(conn) -> int:
    return conn.execute("SELECT count(*) FROM users WHERE is_admin = 1").fetchone()[0]


def set_admin(conn, user_id: int, is_admin: bool):
    """Da o quita el rol de admin. No deja quitárselo al último admin que queda."""
    if not is_admin and count_admins(conn) <= 1:
        row = conn.execute("SELECT is_admin FROM users WHERE id = ?", (user_id,)).fetchone()
        if row and row["is_admin"]:
            raise ValueError("No puedes quitar el admin a la única cuenta administradora.")
    conn.execute("UPDATE users SET is_admin = ? WHERE id = ?", (int(is_admin), user_id))


def set_blocked(conn, user_id: int, blocked: bool):
    user = get_user(conn, user_id)
    if not user:
        raise ValueError("Usuario no encontrado")
    if blocked and user["is_admin"] and count_admins(conn) <= 1:
        raise ValueError("No puedes bloquear a la única cuenta administradora.")
    conn.execute("UPDATE users SET is_blocked = ? WHERE id = ?", (int(blocked), user_id))


# Claves de app_settings con sufijo ":<user_id>" (ver affinity/waifus/season_cache).
# Lista cerrada a proposito: otras claves usan otros ids con el mismo formato
# (characters_topup:<title_id>) y un LIKE '%:<id>' se las llevaria.
_USER_SETTING_PREFIXES = (
    "affinity_config", "affinity_recompute_running", "affinity_recompute_finished_at",
    "affinity_accuracy", "waifus_order_mode", "show_anime_calendar",
)


def delete_user(conn, user_id: int):
    """Borra la cuenta y todo lo suyo. El catálogo compartido (titles, episodes,
    characters) se queda: lo pueden estar usando otras cuentas."""
    user = get_user(conn, user_id)
    if not user:
        raise ValueError("Usuario no encontrado")
    if user["is_admin"] and count_admins(conn) <= 1:
        raise ValueError("No puedes borrar a la única cuenta administradora.")
    entry_ids = [r[0] for r in conn.execute("SELECT id FROM entries WHERE user_id = ?", (user_id,))]
    list_ids = [r[0] for r in conn.execute("SELECT id FROM lists WHERE user_id = ?", (user_id,))]

    def ids_sql(ids):
        return ",".join(str(int(i)) for i in ids) or "NULL"

    e, l_ = ids_sql(entry_ids), ids_sql(list_ids)
    li_ids = [r[0] for r in conn.execute(f"SELECT id FROM list_items WHERE list_id IN ({l_}) OR entry_id IN ({e})")]
    fc_ids = [r[0] for r in conn.execute(f"SELECT id FROM favorite_characters WHERE entry_id IN ({e})")]
    for table, ids in (("entries", entry_ids), ("list_items", li_ids), ("favorite_characters", fc_ids)):
        s_ = ids_sql(ids)
        conn.execute(f"DELETE FROM duels WHERE table_name = ? AND (winner_id IN ({s_}) OR loser_id IN ({s_}))", (table,))
        conn.execute(f"DELETE FROM elo_snapshots WHERE table_name = ? AND item_id IN ({s_})", (table,))
    for table in ("rating_history", "season_ratings", "watch_sessions", "favorite_characters"):
        conn.execute(f"DELETE FROM {table} WHERE entry_id IN ({e})")
    conn.execute(f"DELETE FROM list_items WHERE list_id IN ({l_}) OR entry_id IN ({e})")
    conn.execute(f"DELETE FROM lists WHERE id IN ({l_})")
    conn.execute(f"DELETE FROM entries WHERE id IN ({e})")
    for table in ("episode_comments", "episode_user_state", "episode_watches", "profile_history", "user_calendar_links",
                  "recommendations_cache", "rejected_recommendations", "score_distribution", "taste_profile"):
        conn.execute(f"DELETE FROM {table} WHERE user_id = ?", (user_id,))
    for prefix in _USER_SETTING_PREFIXES:
        conn.execute("DELETE FROM app_settings WHERE key = ?", (f"{prefix}:{user_id}",))
    conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    return user
