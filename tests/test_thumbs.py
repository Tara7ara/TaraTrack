import os

from PIL import Image

from app import config, thumbs, web


def _poster(tmp_path, monkeypatch, name="12.jpg", size=(500, 750)):
    monkeypatch.setattr(config, "POSTERS_DIR", str(tmp_path))
    Image.new("RGB", size, (200, 120, 60)).save(tmp_path / name, "JPEG", quality=95)
    return f"/static/posters/{name}"


def test_poster_size_maps_local_posters_to_thumbs(tmp_path, monkeypatch):
    path = _poster(tmp_path, monkeypatch)
    assert web.poster_size(path, "w185").startswith("/thumbs/240/12.jpg?v=")
    assert web.poster_size(path, "w342").startswith("/thumbs/400/12.jpg?v=")
    assert web.poster_size(path, "w500") == path  # tamaño grande: la original
    assert web.poster_size("/static/posters/no-existe.jpg", "w185") == "/static/posters/no-existe.jpg"
    assert web.poster_size("/static/img/sin-portada.svg", "w185") == "/static/img/sin-portada.svg"
    tmdb = "https://image.tmdb.org/t/p/w500/abc.jpg"
    assert web.poster_size(tmdb, "w185") == "https://image.tmdb.org/t/p/w185/abc.jpg"


def test_ensure_thumb_resizes_and_regenerates_when_original_changes(tmp_path, monkeypatch):
    _poster(tmp_path, monkeypatch)
    dest = thumbs.ensure_thumb(240, "12.jpg")
    with Image.open(dest) as im:
        assert im.size == (240, 360)
    assert os.path.getsize(dest) < os.path.getsize(tmp_path / "12.jpg")

    # Portada subida a mano: la miniatura se rehace.
    Image.new("RGB", (400, 400), (0, 0, 0)).save(tmp_path / "12.jpg", "JPEG")
    later = os.path.getmtime(dest) + 10
    os.utime(tmp_path / "12.jpg", (later, later))
    with Image.open(thumbs.ensure_thumb(240, "12.jpg")) as im:
        assert im.size == (240, 240)


def test_ensure_thumb_rejects_bad_requests(tmp_path, monkeypatch):
    _poster(tmp_path, monkeypatch)
    assert thumbs.ensure_thumb(999, "12.jpg") is None       # ancho no permitido
    assert thumbs.ensure_thumb(240, "../secreto.jpg") is None
    assert thumbs.ensure_thumb(240, "otra.jpg") is None      # no existe
