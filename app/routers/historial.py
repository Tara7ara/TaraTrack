"""app.routers.historial - historial de visionado."""
import re
from datetime import date, timedelta

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app import repo
from app.db import get_connection
from app.web import templates

router = APIRouter()


@router.get("/historial", response_class=HTMLResponse)
def historial(request: Request):
    with get_connection() as conn:
        items = repo.list_history(conn, request.state.user_id)
    days: dict[str, dict] = {}
    for it in items:
        day = (it["at"] or "")[:10]
        if it["season_number"] is not None:
            kind = "ep"
        else:
            kind = "title" if it["detail"] == "Vista entera" else "rewatch"
        d = days.setdefault(day, {"groups": [], "index": {}, "eps": 0, "minutes": 0, "titles": set()})
        key = (it["tmdb_id"], kind)
        g = d["index"].get(key)
        if g is None:
            g = {
                "title": it["title"], "tmdb_id": it["tmdb_id"], "type": it["type"], "kind": kind,
                "poster_path": it["poster_path"], "backdrop_path": it["backdrop_path"],
                "still_path": it["still_path"], "last": it["at"], "first": it["at"], "eps": [],
            }
            d["index"][key] = g
            d["groups"].append(g)
        g["first"] = it["at"]
        g["still_path"] = g["still_path"] or it["still_path"]
        d["titles"].add(it["tmdb_id"])
        if kind == "ep":
            g["eps"].append((it["season_number"], it["episode_number"], it["ep_name"]))
            d["eps"] += 1
            d["minutes"] += it["runtime_minutes"] or 0
        elif kind == "title" and it["type"] == "movie":
            d["minutes"] += it["runtime_minutes"] or 0
    for d in days.values():
        con_eps = {g["tmdb_id"] for g in d["groups"] if g["kind"] == "ep"}
        d["groups"] = [g for g in d["groups"]
                       if not (g["kind"] == "title" and g["type"] == "show" and g["tmdb_id"] in con_eps)]
        for g in d["groups"]:
            eps = sorted(set(g["eps"]))
            g["n_eps"] = len(eps)
            if len(eps) == 1:
                g["label"] = f"T{eps[0][0]}E{eps[0][1]}"
                name = eps[0][2] or ""
                g["ep_name"] = None if re.fullmatch(r"(Episodio|Episode) \d+", name) else name
            elif eps:
                (s1, e1, _), (s2, e2, _) = eps[0], eps[-1]
                g["label"] = f"T{s1}E{e1} – E{e2}" if s1 == s2 else f"T{s1}E{e1} – T{s2}E{e2}"
                g["ep_name"] = None
            else:
                if g["kind"] == "title":
                    g["label"] = "Película vista" if g["type"] == "movie" else "Marcada como vista"
                else:
                    g["label"] = "Volver a ver"
                g["ep_name"] = None
        d["n_titles"] = len(d.pop("titles"))
        d.pop("index")
    week_ago = (date.today() - timedelta(days=6)).isoformat()
    recent = [d for day, d in days.items() if day >= week_ago]
    semana = {
        "eps": sum(d["eps"] for d in recent),
        "titles": len({g["tmdb_id"] for d in recent for g in d["groups"]}),
        "minutes": sum(d["minutes"] for d in recent),
    }
    return templates.TemplateResponse(request, "history.html", {"days": days, "semana": semana})
