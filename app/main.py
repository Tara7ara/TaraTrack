import asyncio
import logging
import threading
import time
import traceback
from datetime import date
from urllib.parse import quote

from fastapi import FastAPI, Form, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
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
from app.routers.calendario import _SEASON_ORDER, _current_season
from app.sesion import _auth_serializer, read_session, session_matches, set_session_cookie  # noqa: F401
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


# Lo único de /static sin sesión: lo que necesitan el login, la activación y la PWA.
# Pósters, retratos, subidas y avatares piden sesión.
_PUBLIC_STATIC = ("/static/css/", "/static/js/", "/static/img/", "/static/manifest.webmanifest")


@app.middleware("http")
async def static_cache_headers(request: Request, call_next):
    """Pósters, fotos y JS no cambian: caché larga en el navegador. Las páginas y los
    parciales de htmx van con no-store, porque Safari enseñaba versiones viejas al volver atrás."""
    response = await call_next(request)
    # /thumbs/ lleva ?v= con la fecha de la portada original, así que también se puede
    # cachear a largo plazo.
    if request.url.path.startswith("/fondo/") and response.status_code == 200:
        # Los nombres de TMDB son únicos por imagen: nunca cambian, se pueden cachear un año.
        # La redirección a TMDB (fondo aún no guardado) no se cachea: cuando se guarde, que
        # el navegador lo pida al servidor.
        response.headers["Cache-Control"] = "private, max-age=31536000, immutable"
    elif request.url.path.startswith(_PUBLIC_STATIC):
        response.headers["Cache-Control"] = "public, max-age=604800"
    elif request.url.path.startswith(("/static/", "/thumbs/")) and response.status_code == 200:
        # "private": que ningún proxy guarde una copia y la sirva sin sesión.
        response.headers["Cache-Control"] = "private, max-age=604800"
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


# Comprobar el límite y apuntar el intento en un solo paso: con peticiones simultáneas
# en el threadpool, todas pasaban la comprobación antes de anotar ningún fallo. El
# intento se reserva antes de verificar y se devuelve si no era un fallo.
_attempts_lock = threading.Lock()


def _reserve_attempt(key: str, ip: str) -> bool:
    with _attempts_lock:
        if _login_locked_out(key) or _ip_locked_out(ip):
            return False
        _record_login_failure(key)
        _record_ip_failure(ip)
        return True


def _release_attempt(key: str, ip: str, clear_key: bool = False) -> None:
    with _attempts_lock:
        if clear_key:
            _clear_login_failures(key)
        elif _login_failures_by_user.get(key):
            _login_failures_by_user[key].pop()
        if _failures_by_ip.get(ip):
            _failures_by_ip[ip].pop()


def _safe_next(next_url: str) -> str:
    """Solo rutas internas para el redirect post-login - '/x' vale, 'https://evil.com'
    o '//evil.com' (protocol-relative) no, evita que un link manipulado con
    ?next=https://... redirija tras el login a un sitio ajeno (open redirect)."""
    if next_url and next_url.startswith("/") and not next_url.startswith("//"):
        return next_url
    return "/"


# iOS pide el icono de la pantalla de inicio tambien en la raiz, sin sesion.
_TOUCH_ICON_PATHS = ("/apple-touch-icon.png", "/apple-touch-icon-precomposed.png")


@app.get("/apple-touch-icon.png", include_in_schema=False)
@app.get("/apple-touch-icon-precomposed.png", include_in_schema=False)
def apple_touch_icon():
    return FileResponse("app/static/img/apple-touch-icon.png", media_type="image/png")


def _to_login(request: Request):
    """htmx sigue un 303 por AJAX y pintaba el login dentro del botón pulsado: a una
    petición htmx se le pide que recargue la página completa (HX-Redirect)."""
    target = "/login"
    if request.method == "GET" and not request.headers.get("HX-Request"):
        path = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        target = f"/login?next={quote(path)}"
    # no-store: la redirección de un fichero privado no debe quedarse en ninguna caché.
    if request.headers.get("HX-Request"):
        return Response(status_code=200, headers={"HX-Redirect": "/login", "Cache-Control": "no-store"})
    return RedirectResponse(target, status_code=303, headers={"Cache-Control": "no-store"})


@app.middleware("http")
async def require_login(request: Request, call_next):
    path = request.url.path
    if path in ("/login", "/registro", "/activar", *_TOUCH_ICON_PATHS) or (path.startswith(_PUBLIC_STATIC) and ".." not in path):
        return await call_next(request)
    session = read_session(request.cookies.get(AUTH_COOKIE))
    if session is None:
        return _to_login(request)
    user_id, stamp = session
    with get_connection() as conn:
        user = repo.get_user(conn, user_id)
    if not user or user["is_blocked"] or not session_matches(user, stamp):
        resp = _to_login(request)
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
    if not _reserve_attempt(username_norm, ip):
        return templates.TemplateResponse(
            request, "login.html",
            {"next": _safe_next(next), "error": "Demasiados intentos. Espera unos minutos e inténtalo de nuevo."},
            status_code=429,
        )
    with get_connection() as conn:
        user = repo.get_user_by_username(conn, username_norm)
    if user:
        ok = repo.verify_password(password, user["password_hash"], user["password_salt"])
    else:
        repo.burn_password_check(password)
        ok = False
    if ok:
        _release_attempt(username_norm, ip, clear_key=True)
        if user["is_blocked"]:
            return templates.TemplateResponse(
                request, "login.html",
                {"next": _safe_next(next), "error": "Esta cuenta está bloqueada. Habla con quien te invitó."},
                status_code=403,
            )
        return set_session_cookie(RedirectResponse(_safe_next(next), status_code=303), user)
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

    if not _reserve_attempt(key, ip):
        return fail("Demasiados intentos. Espera unos minutos e inténtalo de nuevo.", 429)
    with get_connection() as conn:
        try:
            user = repo.activate_invite(conn, username_norm, code, password, password2)
        except ValueError as e:
            if str(e) != "Usuario o código no válidos":
                _release_attempt(key, ip)
            return fail(str(e), 400)
    _release_attempt(key, ip, clear_key=True)
    return set_session_cookie(RedirectResponse("/", status_code=303), user)


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
        targets = [(season, date.today().year) for season in _SEASON_ORDER]
        if _current_season() not in targets:
            targets.append(_current_season())
        for season, year in targets:
            try:
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
