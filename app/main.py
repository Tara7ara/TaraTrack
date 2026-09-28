import asyncio
import logging
import time
import traceback
from datetime import date
from urllib.parse import quote

from fastapi import FastAPI, Form, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import anime, config, repo
from app.db import get_connection, init_db
from app.routers import (
    ajustes,
    buscar,
    calendario,
    duelo,
    estadisticas,
    historial,
    listas,
    pendientes,
    puntuar,
    recomendados,
    thumbs,
    titulo,
    vistas,
    waifus,
)
from app.routers.calendario import _SEASON_ORDER
from app.web import templates

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
# httpx registra cada peticion en INFO y las URLs de TMDB llevan la api_key: sin esto la
# clave acababa en `docker logs`.
logging.getLogger("httpx").setLevel(logging.WARNING)

# Raíz de composición: crea la app, monta estáticos y middleware, incluye los routers
# de app/routers/ y se queda con lo transversal (auth, arranque, tareas de fondo,
# errores).
app = FastAPI(title="TaraTrack")
# Acceso remoto (fuera de LAN): comprimir HTML/JSON abarata mucho cada pagina.
app.add_middleware(GZipMiddleware, minimum_size=500)
app.mount("/static", StaticFiles(directory="app/static"), name="static")

for _router_module in (
    pendientes, buscar, titulo, puntuar, recomendados, vistas, duelo,
    historial, listas, waifus, estadisticas, ajustes, calendario, thumbs,
):
    app.include_router(_router_module.router)


@app.exception_handler(StarletteHTTPException)
async def http_error(request: Request, exc: StarletteHTTPException):
    """404 y demas errores HTTP con la pinta de la app en vez de la pagina generica."""
    return templates.TemplateResponse(
        request, "error.html", {"code": exc.status_code, "message": exc.detail}, status_code=exc.status_code
    )


@app.exception_handler(Exception)
async def unhandled_error(request: Request, exc: Exception):
    """500 con el aspecto de la app; la traza sigue yendo a los logs del contenedor."""
    traceback.print_exc()
    return templates.TemplateResponse(
        request, "error.html", {"code": 500, "message": "Algo se ha roto de verdad."}, status_code=500
    )


@app.middleware("http")
async def static_cache_headers(request: Request, call_next):
    response = await call_next(request)
    # /thumbs/ lleva ?v= con la fecha de la portada original, así que también se puede
    # cachear a largo plazo.
    if request.url.path.startswith("/fondo/") and response.status_code == 200:
        # Los nombres de TMDB son únicos por imagen: nunca cambian, se pueden cachear un año.
        # La redirección a TMDB (fondo aún no guardado) no se cachea: cuando se guarde, que
        # el navegador lo pida al servidor.
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    elif request.url.path.startswith(("/static/", "/thumbs/")):
        response.headers["Cache-Control"] = "public, max-age=604800"
    else:
        response.headers["Cache-Control"] = "no-store"
    return response


# ---------- Login (cookie firmada de larga duración) ----------
# Nombres y límites en app/config.py.
AUTH_COOKIE = config.AUTH_COOKIE
AUTH_MAX_AGE = config.AUTH_MAX_AGE

# Freno a la fuerza bruta en /login, por usuario intentado: los fallos contra una
# cuenta no bloquean a las demás. No es por IP porque detrás del proxy inverso
# request.client.host sería siempre la misma.
LOGIN_MAX_ATTEMPTS = config.LOGIN_MAX_ATTEMPTS
LOGIN_WINDOW_SECONDS = config.LOGIN_WINDOW_SECONDS
_login_failures_by_user: dict[str, list[float]] = {}


def _prune(bucket: list[float], window_seconds: float) -> list[float]:
    now = time.time()
    while bucket and now - bucket[0] > window_seconds:
        bucket.pop(0)
    return bucket


def _login_locked_out(username: str) -> bool:
    bucket = _login_failures_by_user.get(username, [])
    return len(_prune(bucket, LOGIN_WINDOW_SECONDS)) >= LOGIN_MAX_ATTEMPTS


def _record_login_failure(username: str) -> None:
    _login_failures_by_user.setdefault(username, []).append(time.time())


def _clear_login_failures(username: str) -> None:
    _login_failures_by_user.pop(username, None)


IP_MAX_FAILURES = 20
_failures_by_ip: dict[str, list[float]] = {}


def _client_ip(request: Request) -> str:
    return request.headers.get("cf-connecting-ip") or (request.client.host if request.client else "?")


def _ip_locked_out(ip: str) -> bool:
    return len(_prune(_failures_by_ip.get(ip, []), LOGIN_WINDOW_SECONDS)) >= IP_MAX_FAILURES


def _record_ip_failure(ip: str) -> None:
    _failures_by_ip.setdefault(ip, []).append(time.time())


def _auth_serializer() -> URLSafeTimedSerializer:
    secret = config.get_secret_key()
    if not secret:
        raise RuntimeError("Falta TARATRACK_SECRET_KEY en el entorno - obligatoria, sin valor por defecto.")
    return URLSafeTimedSerializer(secret, salt="taratrack-auth")


def _safe_next(next_url: str) -> str:
    """Solo rutas internas para el redirect post-login - '/x' vale, 'https://evil.com'
    o '//evil.com' (protocol-relative) no, evita que un link manipulado con
    ?next=https://... redirija tras el login a un sitio ajeno (open redirect)."""
    if next_url and next_url.startswith("/") and not next_url.startswith("//"):
        return next_url
    return "/"


def _current_user_id(request: Request) -> int | None:
    """La cookie firma solo el user_id; username/is_admin se leen de la BBDD en cada
    petición vía request.state."""
    token = request.cookies.get(AUTH_COOKIE)
    if not token:
        return None
    try:
        payload = _auth_serializer().loads(token, max_age=AUTH_MAX_AGE)
        return int(payload)
    except (BadSignature, SignatureExpired, TypeError, ValueError):
        return None


# iOS pide el icono de la pantalla de inicio tambien en la raiz, sin sesion.
_TOUCH_ICON_PATHS = ("/apple-touch-icon.png", "/apple-touch-icon-precomposed.png")


@app.get("/apple-touch-icon.png", include_in_schema=False)
@app.get("/apple-touch-icon-precomposed.png", include_in_schema=False)
def apple_touch_icon():
    return FileResponse("app/static/img/apple-touch-icon.png", media_type="image/png")


@app.middleware("http")
async def require_login(request: Request, call_next):
    path = request.url.path
    if path in ("/login", "/registro", "/activar", *_TOUCH_ICON_PATHS) or path.startswith("/static/"):
        return await call_next(request)
    user_id = _current_user_id(request)
    if user_id is None:
        if request.method == "GET":
            return RedirectResponse(f"/login?next={quote(path)}", status_code=303)
        return RedirectResponse("/login", status_code=303)
    with get_connection() as conn:
        user = repo.get_user(conn, user_id)
    if not user or user["is_blocked"]:
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(AUTH_COOKIE)
        return resp
    request.state.user_id = user["id"]
    request.state.username = user["username"]
    request.state.is_admin = bool(user["is_admin"])
    request.state.avatar_path = user["avatar_path"]
    return await call_next(request)


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str = "/"):
    return templates.TemplateResponse(request, "login.html", {"next": _safe_next(next), "error": None})


@app.post("/login", response_class=HTMLResponse)
def login_submit(request: Request, username: str = Form(...), password: str = Form(...), next: str = Form("/")):
    username_norm = username.strip().lower()
    ip = _client_ip(request)
    if _login_locked_out(username_norm) or _ip_locked_out(ip):
        return templates.TemplateResponse(
            request, "login.html",
            {"next": _safe_next(next), "error": "Demasiados intentos. Espera unos minutos e inténtalo de nuevo."},
            status_code=429,
        )
    with get_connection() as conn:
        user = repo.get_user_by_username(conn, username_norm)
    if user and repo.verify_password(password, user["password_hash"], user["password_salt"]):
        if user["is_blocked"]:
            return templates.TemplateResponse(
                request, "login.html",
                {"next": _safe_next(next), "error": "Esta cuenta está bloqueada. Habla con quien te invitó."},
                status_code=403,
            )
        _clear_login_failures(username_norm)
        resp = RedirectResponse(_safe_next(next), status_code=303)
        resp.set_cookie(
            AUTH_COOKIE, _auth_serializer().dumps(str(user["id"])),
            max_age=AUTH_MAX_AGE, httponly=True, secure=True, samesite="lax",
        )
        return resp
    _record_login_failure(username_norm)
    _record_ip_failure(ip)
    return templates.TemplateResponse(
        request, "login.html", {"next": _safe_next(next), "error": "Usuario o contraseña incorrectos"}, status_code=401
    )


@app.post("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(AUTH_COOKIE)
    return resp


@app.get("/registro")
def registro_redirect():
    return RedirectResponse("/activar", status_code=303)


@app.get("/activar", response_class=HTMLResponse)
def activar_form(request: Request, usuario: str = ""):
    return templates.TemplateResponse(request, "activar.html", {"usuario": usuario, "error": None})


@app.post("/activar", response_class=HTMLResponse)
def activar_submit(
    request: Request, username: str = Form(...), code: str = Form(...),
    password: str = Form(...), password2: str = Form(""),
):
    username_norm = username.strip().lower()
    key, ip = f"activar:{username_norm}", _client_ip(request)

    def fail(error: str, status: int):
        return templates.TemplateResponse(
            request, "activar.html", {"usuario": username_norm, "error": error}, status_code=status
        )

    if _login_locked_out(key) or _ip_locked_out(ip):
        return fail("Demasiados intentos. Espera unos minutos e inténtalo de nuevo.", 429)
    with get_connection() as conn:
        try:
            user = repo.activate_invite(conn, username_norm, code, password, password2)
        except ValueError as e:
            if str(e) == "Usuario o código no válidos":
                _record_login_failure(key)
                _record_ip_failure(ip)
            return fail(str(e), 400)
    _clear_login_failures(key)
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(
        AUTH_COOKIE, _auth_serializer().dumps(str(user["id"])),
        max_age=AUTH_MAX_AGE, httponly=True, secure=True, samesite="lax",
    )
    return resp


SYNC_INTERVAL_HOURS = config.SYNC_INTERVAL_HOURS


async def sync_loop():
    """Sincronizacion automatica: refresca metadatos/episodios y recalcula recomendados
    cada SYNC_INTERVAL_HOURS. Corre en hilo aparte para no bloquear el servidor (httpx
    sincrono). Antes no dejaba ni rastro en los logs - si empezaba a fallar de forma
    sistematica no habia forma de saberlo sin adivinar (docker logs no decia nada)."""
    while True:
        logging.info("sync de fondo: empieza")
        try:
            await asyncio.to_thread(repo.sync_library)
            await asyncio.to_thread(repo.refresh_recommendations_cache)
            logging.info("sync de fondo: terminada sin errores")
        except Exception:
            logging.exception("sync de fondo: fallo sin capturar")
        await asyncio.sleep(SYNC_INTERVAL_HOURS * 3600)


SEASON_CACHE_INTERVAL_HOURS = config.SEASON_CACHE_INTERVAL_HOURS


async def season_cache_loop():
    """Refresca una vez al día la caché de las 4 temporadas del año actual (son 4
    peticiones a AniList). Las temporadas de otros años se refrescan al visitarlas si
    tienen más de 24 h."""
    while True:
        for season in _SEASON_ORDER:
            try:
                year = date.today().year
                items = await asyncio.to_thread(anime.get_seasonal_anime, season, year)
                with get_connection() as conn:
                    repo.save_season_cache(conn, season, year, items)
                logging.info("season_cache: %s %s refrescada", season, year)
            except Exception:
                logging.warning("season_cache: fallo al refrescar %s", season)
        await asyncio.sleep(SEASON_CACHE_INTERVAL_HOURS * 3600)


@app.on_event("startup")
def on_startup():
    # Si falta alguna variable obligatoria, que falle el arranque y no la primera
    # petición.
    config.validate()
    init_db()
    asyncio.create_task(sync_loop())
    asyncio.create_task(season_cache_loop())


@app.get("/healthz")
def healthz():
    return {"status": "ok"}
