"""app.repo.users - cuentas de usuario.

Contraseñas con hashlib.pbkdf2_hmac y salt aleatorio por usuario, sin dependencias
extra."""
import hashlib
import hmac
import os
import re

_PBKDF2_ITERATIONS = 200_000

# Validación de nombre de usuario, única para create_user, set_username y registro.
# Solo minúsculas, dígitos, guion y guion bajo: se usa en URLs y en el nombre del
# fichero del avatar.
USERNAME_REGEX = re.compile(r"^[a-z0-9_-]{3,20}$")
RESERVED_USERNAMES = frozenset({"admin", "administrador", "sistema", "root", "null", "api", "taratrack"})


def validate_username(username: str) -> str:
    norm = (username or "").strip().lower()
    if not USERNAME_REGEX.match(norm):
        raise ValueError("El usuario debe tener 3-20 caracteres: minúsculas, números, guiones o guion bajo")
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
        "SELECT id, username, is_admin, avatar_path, created_at FROM users ORDER BY created_at"
    ).fetchall()


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
