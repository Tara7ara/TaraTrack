"""Compila todas las plantillas Jinja2 en seco con el Environment real de la app,
para pillar errores de sintaxis antes de desplegar. Lo usa scripts/deploy.sh.

    python scripts/check_templates.py
"""
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
os.chdir(REPO_ROOT)  # Jinja2Templates(directory="app/templates") es relativo al cwd
sys.path.insert(0, str(REPO_ROOT))

from app.web import templates  # noqa: E402

TEMPLATES_DIR = REPO_ROOT / "app" / "templates"

names = sorted(str(p.relative_to(TEMPLATES_DIR)) for p in TEMPLATES_DIR.rglob("*.html"))

failures = []
for name in names:
    try:
        templates.env.get_template(name)
    except Exception as exc:
        failures.append((name, exc))

if failures:
    print(f"{len(failures)} plantilla(s) con error:", file=sys.stderr)
    for name, exc in failures:
        print(f"  {name}: {exc}", file=sys.stderr)
    sys.exit(1)

print(f"OK: {len(names)} plantillas compilan sin errores.")
