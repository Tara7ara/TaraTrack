"""Configuracion centralizada - unico sitio del proyecto que lee variables de
entorno directamente. Antes cada modulo leia la suya con os.environ.get() suelto
(db.py, tmdb.py, main.py x2), sin ningun sitio unico donde ver "que hace falta
para arrancar esto" - pedido tras la revision externa del codigo (2026-08-21):
"un config.py que lea y valide secret, contraseña, rutas, limites de subida y
entorno al arrancar... no cambia comportamiento, pero hace el proyecto mas
previsible".

db.py y tmdb.py siguen exponiendo su propio DB_PATH/API_KEY como atributo de
modulo normal (no una funcion) para no romper el patron de tests ya establecido
en conftest.py (monkeypatch.setattr(db, "DB_PATH", ...)) - simplemente los
importan de aqui al cargar, este fichero es donde vive la lectura real.
TARATRACK_SECRET_KEY/TARATRACK_PASSWORD siguen leyendose EN VIVO (get_secret_key/
get_password, no una constante congelada) a proposito: _auth_serializer las
llama en cada peticion que toca la cookie, para que un despliegue sin la
variable falle ahi mismo sin depender de que validate() se haya llamado antes."""
import os

TMDB_API_KEY = os.environ.get("TMDB_API_KEY", "")
DB_PATH = os.environ.get("TARATRACK_DB_PATH", "/data/taratrack.db")

POSTERS_DIR = "app/static/posters"
PROFILES_DIR = "app/static/profiles"

# Subida de portada manual (ronda 2026-08-21, seguridad): antes se leia el
# archivo entero a memoria sin comprobar tipo ni tamaño.
MAX_POSTER_BYTES = 8 * 1024 * 1024  # 8 MB, de sobra para una portada
POSTER_CONTENT_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})

AUTH_COOKIE = "taratrack_auth"
AUTH_MAX_AGE = 60 * 60 * 24 * 365  # 1 año - "recuerdame" de verdad, ver Seguridad en CLAUDE.md

# Freno a fuerza bruta contra /login (ronda 2026-08-21) - global, no por IP: ver
# el porque en el comentario junto a su uso en main.py (_login_locked_out).
LOGIN_MAX_ATTEMPTS = 5
LOGIN_WINDOW_SECONDS = 15 * 60  # tambien la duracion efectiva del bloqueo

SYNC_INTERVAL_HOURS = 12
SEASON_CACHE_INTERVAL_HOURS = 24

REQUIRED_ENV_VARS = ("TMDB_API_KEY", "TARATRACK_SECRET_KEY", "TARATRACK_PASSWORD")


def get_secret_key() -> str:
    """Lectura en vivo (no una constante congelada al importar este modulo) -
    _auth_serializer la llama en cada peticion que toca la cookie de sesion.
    Sin fallback: un valor por defecto conocido en el codigo permitiria a
    cualquiera fabricar una cookie valida si el contenedor arrancase sin la
    variable puesta (bug real, arreglado en la ronda de seguridad 2026-08-21)."""
    return os.environ.get("TARATRACK_SECRET_KEY", "")


def get_password() -> str:
    return os.environ.get("TARATRACK_PASSWORD", "")


def get_admin_username() -> str:
    """Solo se usa una vez, al crear la primera cuenta (admin) en la migracion
    multiusuario (db._migrate_multiuser) a partir de TARATRACK_PASSWORD - de ahi
    en adelante las cuentas se gestionan desde /ajustes, esta variable deja de
    tener efecto."""
    return os.environ.get("TARATRACK_ADMIN_USERNAME", "tara")


def validate(env=None) -> None:
    """Falla alto y claro si falta algo obligatorio - llamada una vez en
    on_startup (main.py), para descubrirlo en "docker logs" justo tras un
    despliegue en vez de en la primera peticion real de Tara. `env` es
    inyectable para los tests (monkeypatch de variables de entorno) sin
    depender de cuando se importo este modulo por primera vez."""
    env = os.environ if env is None else env
    missing = [name for name in REQUIRED_ENV_VARS if not env.get(name)]
    if missing:
        raise RuntimeError(f"Faltan variables de entorno obligatorias: {', '.join(missing)}")
