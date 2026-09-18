from app.web import fmt_ago


def test_fmt_ago_parses_python_iso_format():
    from datetime import datetime, timedelta, timezone

    hace_5_min = (datetime.now(timezone.utc) - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert fmt_ago(hace_5_min) == "hace 5 min"


def test_fmt_ago_parses_sqlite_format():
    """Bug real (AGY, 2026-09-18): fmt_ago solo entendia el formato con T/Z que
    genera Python - los created_at de episode_comments vienen de datetime('now')
    de SQLite ('YYYY-MM-DD HH:MM:SS', sin T ni Z) y caian al fallback (fecha en
    crudo) en vez de dar "hace X min"."""
    from datetime import datetime, timedelta, timezone

    hace_5_min = (datetime.now(timezone.utc) - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
    assert fmt_ago(hace_5_min) == "hace 5 min"


def test_fmt_ago_falls_back_to_raw_string_on_garbage():
    assert fmt_ago("no es una fecha") == "no es una fecha"


def test_fmt_ago_empty_is_empty():
    assert fmt_ago("") == ""
    assert fmt_ago(None) == ""
