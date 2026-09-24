import pytest

from app import config


def test_validate_passes_when_everything_present():
    config.validate(env={"TMDB_API_KEY": "x", "TARATRACK_SECRET_KEY": "y", "TARATRACK_PASSWORD": "z"})


def test_validate_fails_listing_every_missing_var():
    """El arranque falla si falta cualquiera de las 3 variables obligatorias."""
    with pytest.raises(RuntimeError) as exc_info:
        config.validate(env={"TARATRACK_SECRET_KEY": "y"})
    assert "TMDB_API_KEY" in str(exc_info.value)
    assert "TARATRACK_PASSWORD" in str(exc_info.value)
    assert "TARATRACK_SECRET_KEY" not in str(exc_info.value)


def test_validate_reads_live_environ_by_default(monkeypatch):
    """Sin pasar env= explicito, validate() debe leer os.environ en vivo - no una
    constante congelada al importar config.py - para que un despliegue real sin la
    variable falle aqui, no en la primera peticion."""
    monkeypatch.delenv("TARATRACK_SECRET_KEY", raising=False)
    monkeypatch.setenv("TARATRACK_PASSWORD", "z")
    monkeypatch.setenv("TMDB_API_KEY", "x")
    with pytest.raises(RuntimeError):
        config.validate()
