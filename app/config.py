"""Configuración centralizada: único módulo que lee variables de entorno.

db.py y tmdb.py exponen DB_PATH/API_KEY como atributos de módulo para que los tests
puedan sustituirlos con monkeypatch. La clave de sesión y la contraseña se leen en
vivo en cada petición, así un despliegue sin ellas falla en el acto."""
import os

TMDB_API_KEY = os.environ.get("TMDB_API_KEY", "")
DB_PATH = os.environ.get("TARATRACK_DB_PATH", "/data/taratrack.db")

POSTERS_DIR = "app/static/posters"
PROFILES_DIR = "app/static/profiles"

# Subida de portada manual: tipos admitidos y tamaño máximo.
MAX_POSTER_BYTES = 8 * 1024 * 1024  # 8 MB, de sobra para una portada
POSTER_CONTENT_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})

AUTH_COOKIE = "taratrack_auth"
AUTH_MAX_AGE = 60 * 60 * 24 * 365  # 1 año

# Freno a la fuerza bruta en /login (ver _login_locked_out en main.py).
LOGIN_MAX_ATTEMPTS = 5
LOGIN_WINDOW_SECONDS = 15 * 60  # tambien la duracion efectiva del bloqueo

SYNC_INTERVAL_HOURS = 12
SEASON_CACHE_INTERVAL_HOURS = 24

REQUIRED_ENV_VARS = ("TMDB_API_KEY", "TARATRACK_SECRET_KEY", "TARATRACK_PASSWORD")


def get_secret_key() -> str:
    """Se lee en vivo y sin valor por defecto: un secreto conocido en el código
    permitiría fabricar cookies de sesión válidas."""
    return os.environ.get("TARATRACK_SECRET_KEY", "")


def get_password() -> str:
    return os.environ.get("TARATRACK_PASSWORD", "")


def get_admin_username() -> str:
    """Solo se usa una vez, al crear la primera cuenta (admin) en la migracion
    multiusuario (db._migrate_multiuser) a partir de TARATRACK_PASSWORD - de ahi
    en adelante las cuentas se gestionan desde /ajustes, esta variable deja de
    tener efecto."""
    return os.environ.get("TARATRACK_ADMIN_USERNAME", "principal")


def validate(env=None) -> None:
    """Falla al arrancar si falta alguna variable obligatoria. `env` es inyectable
    para los tests."""
    env = os.environ if env is None else env
    missing = [name for name in REQUIRED_ENV_VARS if not env.get(name)]
    if missing:
        raise RuntimeError(f"Faltan variables de entorno obligatorias: {', '.join(missing)}")
