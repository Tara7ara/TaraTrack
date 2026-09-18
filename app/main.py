import asyncio
import logging
import time
import traceback
from datetime import date
from urllib.parse import quote

from fastapi import FastAPI, Form, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse
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
    titulo,
    vistas,
    waifus,
)
from app.routers.calendario import _SEASON_ORDER
from app.web import templates

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# main.py es la raiz de composicion (ronda 2026-08-21, split de modulos): crea la
# app, monta estatico/middleware, incluye los routers por dominio (app/routers/*.py)
# y se queda con lo que de verdad es transversal a toda la app - auth, arranque,
# tareas de fondo, manejo de errores. El resto de rutas vive en su propio router.
app = FastAPI(title="TaraTrack")
# Acceso remoto (fuera de LAN): comprimir HTML/JSON abarata mucho cada pagina.
app.add_middleware(GZipMiddleware, minimum_size=500)
app.mount("/static", StaticFiles(directory="app/static"), name="static")

for _router_module in (
    pendientes, buscar, titulo, puntuar, recomendados, vistas, duelo,
    historial, listas, waifus, estadisticas, ajustes, calendario,
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
    """500 con la pinta de la app - la traza sigue yendo a los logs del contenedor
    (docker logs taratrack), esto solo cambia lo que ve Tara en el navegador."""
    traceback.print_exc()
    return templates.TemplateResponse(
        request, "error.html", {"code": 500, "message": "Algo se ha roto de verdad."}, status_code=500
    )


@app.middleware("http")
async def static_cache_headers(request: Request, call_next):
    """Posters/fotos/JS no cambian: cache larga en el navegador = menos viajes al servidor.

    Todo lo demas (paginas reales, partials de htmx) es justo lo contrario: datos que
    cambian con cada accion de cualquiera de los usuarios. Sin un Cache-Control
    explicito, un navegador (sobre todo Safari/iOS, con el back-forward cache mas
    agresivo) puede enseñar una version vieja de /pendientes o /waifus al volver
    atras despues de una accion, dando la sensacion de "esto no se ha guardado" hasta
    recargar a mano (Tara, 2026-09-18: varios "he tenido que recargar la web" seguidos
    tras quitar un pendiente o añadir un personaje favorito)."""
    response = await call_next(request)
    if request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "public, max-age=604800"
    else:
        response.headers["Cache-Control"] = "no-store"
    return response


# ---------- Login (cookie larga, sustituye al Access List de NPM para tracker.midominio.com) ----------
# Tara, 2026-08-20: "el pass... se puede guardar en la coockie o algo? como hacen las
# grandes apps?" - antes la unica auth era el Access List de Nginx Proxy Manager (basic
# auth HTTP delante del proxy), sin "recuerdame", asi que el navegador volvia a pedirla
# a menudo. Login propio con cookie firmada de un año - "como hacen las grandes apps".
# Nombres/limites en app/config.py (ronda 2026-08-21, config centralizada).
AUTH_COOKIE = config.AUTH_COOKIE
AUTH_MAX_AGE = config.AUTH_MAX_AGE

# Freno a fuerza bruta contra /login - antes (monousuario, sin cuentas) era un
# contador GLOBAL en memoria: con una unica contraseña compartida no habia nada mas
# fino que hacer. Con cuentas reales (2026-09-17, multiusuario) pasa a ser POR
# USUARIO INTENTADO - 5 fallos contra "tara" no deben bloquear el login de su
# hermana/amigo, que no han fallado ni una vez. Sigue sin ser por IP: detras de NPM
# sin X-Forwarded-For de confianza configurado, request.client.host seria siempre la
# misma IP del proxy.
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


# Freno a /registro (gap real, AGY 2026-09-18): /login ya tenia freno de fuerza bruta,
# /registro no tenia ninguno - un script en bucle podia crear cientos de cuentas.
# Global (no por username, a diferencia del login) porque aqui lo que hay que frenar
# es EL RITMO de altas nuevas, no los fallos contra una cuenta concreta - una ventana
# generosa (no son intentos de acceso, son altas reales de gente de confianza detras
# del WireGuard) que igualmente para en seco un bucle automatizado.
REGISTRO_MAX_ATTEMPTS = 20
REGISTRO_WINDOW_SECONDS = 3600
_registro_attempts: list[float] = []


def _registro_locked_out() -> bool:
    return len(_prune(_registro_attempts, REGISTRO_WINDOW_SECONDS)) >= REGISTRO_MAX_ATTEMPTS


def _record_registro_attempt() -> None:
    _registro_attempts.append(time.time())


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
    """Antes (monousuario) la cookie era un candado sin identidad, un simple string
    "ok" firmado. Ahora (2026-09-17, multiusuario) el payload es el user_id - sigue
    siendo solo un entero firmado, no hace falta guardar mas en la cookie en si, el
    resto (username/is_admin) se lee de la BBDD en cada peticion via request.state."""
    token = request.cookies.get(AUTH_COOKIE)
    if not token:
        return None
    try:
        payload = _auth_serializer().loads(token, max_age=AUTH_MAX_AGE)
        return int(payload)
    except (BadSignature, SignatureExpired, TypeError, ValueError):
        return None


@app.middleware("http")
async def require_login(request: Request, call_next):
    path = request.url.path
    if path in ("/login", "/registro") or path.startswith("/static/"):
        return await call_next(request)
    user_id = _current_user_id(request)
    if user_id is None:
        if request.method == "GET":
            return RedirectResponse(f"/login?next={quote(path)}", status_code=303)
        return RedirectResponse("/login", status_code=303)
    with get_connection() as conn:
        user = repo.get_user(conn, user_id)
    if not user:
        # Cookie valida (firma correcta) pero el usuario ya no existe - no debería
        # pasar en uso normal (no hay borrado de cuentas todavía), pero si pasa,
        # tratarlo como no autenticado en vez de petar en el resto de la ruta.
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
    if _login_locked_out(username_norm):
        return templates.TemplateResponse(
            request, "login.html",
            {"next": _safe_next(next), "error": "Demasiados intentos. Espera unos minutos e inténtalo de nuevo."},
            status_code=429,
        )
    with get_connection() as conn:
        user = repo.get_user_by_username(conn, username_norm)
    if user and repo.verify_password(password, user["password_hash"], user["password_salt"]):
        _clear_login_failures(username_norm)
        resp = RedirectResponse(_safe_next(next), status_code=303)
        resp.set_cookie(
            AUTH_COOKIE, _auth_serializer().dumps(str(user["id"])),
            max_age=AUTH_MAX_AGE, httponly=True, secure=True, samesite="lax",
        )
        return resp
    _record_login_failure(username_norm)
    return templates.TemplateResponse(
        request, "login.html", {"next": _safe_next(next), "error": "Usuario o contraseña incorrectos"}, status_code=401
    )


@app.post("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(AUTH_COOKIE)
    return resp


# ---------- Registro (Tara, 2026-09-18: "lo tienen que hacer ellos, no quiero saber la
# pass en ningun momento") - autoregistro publico desde /login, sin invitacion: el acceso
# a /login ya esta acotado por su red WireGuard, asi que no hace falta un filtro aparte
# aqui. Cuentas creadas asi siempre is_admin=False - el alta manual de /ajustes (Fase 1)
# se queda para uso de la propia Tara si algun dia le hace falta, pero deja de ser el
# camino que usan su hermana/amigo.
@app.get("/registro", response_class=HTMLResponse)
def registro_form(request: Request, next: str = "/"):
    return templates.TemplateResponse(request, "registro.html", {"next": _safe_next(next), "error": None})


@app.post("/registro", response_class=HTMLResponse)
def registro_submit(
    request: Request, username: str = Form(...), password: str = Form(...),
    password2: str = Form(""), next: str = Form("/"),
):
    if _registro_locked_out():
        return templates.TemplateResponse(
            request, "registro.html",
            {"next": _safe_next(next), "error": "Demasiadas cuentas creadas seguidas. Espera unos minutos e inténtalo de nuevo."},
            status_code=429,
        )
    _record_registro_attempt()
    # Registro mas exigente (Tara, 2026-09-18: "registro mas fuerte") - contraseña de
    # al menos 8 y confirmacion para pillar erratas al escribirla (antes no habia
    # forma de saber si te habias equivocado hasta el primer intento de login
    # fallido). El nombre de usuario se valida con repo.validate_username, la misma
    # regla que create_user/set_username (AGY, 2026-09-18: "validacion profesional").
    error = None
    try:
        username_norm = repo.validate_username(username)
    except ValueError as e:
        error = str(e)
        username_norm = None
    if not error and len(password) < 8:
        error = "La contraseña debe tener al menos 8 caracteres"
    elif not error and password != password2:
        error = "Las contraseñas no coinciden"
    if error:
        return templates.TemplateResponse(
            request, "registro.html", {"next": _safe_next(next), "error": error}, status_code=400,
        )
    with get_connection() as conn:
        if repo.get_user_by_username(conn, username_norm):
            return templates.TemplateResponse(
                request, "registro.html",
                {"next": _safe_next(next), "error": "Ese usuario ya existe"},
                status_code=409,
            )
        user = repo.create_user(conn, username_norm, password)
    resp = RedirectResponse(_safe_next(next), status_code=303)
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
    """Refresca la cache de las 4 temporadas del año actual una vez al dia (Tara,
    2026-08-13: "que solo lo actualice 1 vez al dia" + "¿qué tan costoso es que
    guarde más tiempo?") - las 4 en vez de solo la actual porque el coste real es
    minimo (4 peticiones a AniList una vez al dia, nada frente a su limite de ~90/min)
    y asi cualquier pestaña de temporada esta siempre al dia sin depender de que Tara
    la visite primero. Los años/temporadas que NO son el año actual (fichas antiguas
    que ya vio) no se tocan aqui - se quedan con lo ya cacheado la ultima vez que se
    visitaron, sin caducar nunca solas (nada las borra); si algun dia hace falta
    refrescarlas, se refrescan solas al visitarlas si estan caducadas (>24h)."""
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
    # Falla el arranque, no la primera peticion, si falta algo obligatorio (secret,
    # contraseña, API key de TMDB) - mas facil de detectar en "docker logs" justo
    # tras un despliegue que un 500 silencioso a medio usar (ronda 2026-08-21,
    # config centralizada: antes esto solo comprobaba el secret, aqui a mano).
    config.validate()
    init_db()
    asyncio.create_task(sync_loop())
    asyncio.create_task(season_cache_loop())


@app.get("/healthz")
def healthz():
    return {"status": "ok"}
