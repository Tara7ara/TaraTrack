"""Miniaturas de las portadas locales (/static/posters/...).

Las portadas se guardan a 500px de ancho (~100 KB), pero el mosaico y las filas de las
listas las pintan a menos de 200px: servir una miniatura ahorra la mayor parte de la
descarga, que en datos móviles sale por la subida de la conexión del servidor. Se
generan la primera vez que se piden (routers/thumbs.py) y se regeneran si la portada
original cambia (portada subida a mano)."""
import os
import re

from PIL import Image

from app import config

# Tamaño que piden las plantillas (mismo nombre que en TMDB) -> ancho de la miniatura.
THUMB_WIDTHS = {"w185": 240, "w342": 400}

LOCAL_PREFIX = "/static/posters/"

_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+\.(?:jpg|jpeg|png|webp)$")


def _posters_dir():
    return config.POSTERS_DIR


def thumb_url(poster_path, size):
    """URL de la miniatura de una portada local, o None si no aplica (portada de TMDB,
    placeholder, tamaño sin miniatura o fichero inexistente). ?v= cambia cuando cambia
    la portada original, para que la caché del navegador no enseñe la vieja."""
    width = THUMB_WIDTHS.get(size)
    if not width or not poster_path or not poster_path.startswith(LOCAL_PREFIX):
        return None
    name = poster_path[len(LOCAL_PREFIX):].split("?", 1)[0]
    if not _NAME_RE.match(name):
        return None
    try:
        mtime = int(os.stat(os.path.join(_posters_dir(), name)).st_mtime)
    except OSError:
        return None
    return f"/thumbs/{width}/{name}?v={mtime}"


def ensure_thumb(width, name):
    """Ruta en disco de la miniatura (la crea si falta o está desfasada), o None si la
    petición no es válida o no existe la portada original."""
    if width not in THUMB_WIDTHS.values() or not _NAME_RE.match(name):
        return None
    src = os.path.join(_posters_dir(), name)
    if not os.path.isfile(src):
        return None
    dest_dir = os.path.join(_posters_dir(), "thumbs", str(width))
    dest = os.path.join(dest_dir, os.path.splitext(name)[0] + ".jpg")
    if os.path.exists(dest) and os.path.getmtime(dest) >= os.path.getmtime(src):
        return dest
    os.makedirs(dest_dir, exist_ok=True)
    with Image.open(src) as im:
        im = im.convert("RGB")
        if im.width > width:
            im = im.resize((width, round(im.height * width / im.width)), Image.LANCZOS)
        tmp = f"{dest}.{os.getpid()}.tmp"
        im.save(tmp, "JPEG", quality=80, optimize=True, progressive=True)
    os.replace(tmp, dest)
    return dest
