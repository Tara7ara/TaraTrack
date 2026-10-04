"""Cookie de sesión: user_id firmado y una huella del salt de la contraseña. Cambiar
la contraseña (o generar una invitación nueva) cambia el salt, así que las sesiones
abiertas con la anterior dejan de valer."""

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app import config

AUTH_COOKIE = config.AUTH_COOKIE
AUTH_MAX_AGE = config.AUTH_MAX_AGE
_STAMP_LEN = 12


def _auth_serializer() -> URLSafeTimedSerializer:
    secret = config.get_secret_key()
    if not secret:
        raise RuntimeError("Falta TARATRACK_SECRET_KEY en el entorno - obligatoria, sin valor por defecto.")
    return URLSafeTimedSerializer(secret, salt="taratrack-auth")


def read_session(token: str | None) -> tuple[int, str] | None:
    """(user_id, huella) de una cookie válida, o None."""
    if not token:
        return None
    try:
        user_id, stamp = _auth_serializer().loads(token, max_age=AUTH_MAX_AGE).split(".", 1)
        return int(user_id), stamp
    except (BadSignature, SignatureExpired, TypeError, ValueError):
        return None


def session_matches(user, stamp: str) -> bool:
    return (user["password_salt"] or "")[:_STAMP_LEN] == stamp


def set_session_cookie(resp, user):
    token = _auth_serializer().dumps(f"{user['id']}.{(user['password_salt'] or '')[:_STAMP_LEN]}")
    resp.set_cookie(AUTH_COOKIE, token, max_age=AUTH_MAX_AGE, httponly=True, secure=True, samesite="lax")
    return resp
