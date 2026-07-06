"""routes/pages.py — HTML page routes"""
from fastapi import APIRouter
from fastapi.responses import HTMLResponse
from jinja2 import Environment

router = APIRouter()
_jinja: Environment = None  # injected by main.py


def set_jinja(env: Environment):
    global _jinja
    _jinja = env


def render(name: str, **ctx) -> HTMLResponse:
    return HTMLResponse(_jinja.get_template(name).render(**ctx))


@router.get("/", response_class=HTMLResponse)
async def index():
    return render("index.html", active_page="library")

@router.get("/reader/{comic_id}", response_class=HTMLResponse)
async def reader(comic_id: str):
    return render("reader.html", comic_id=comic_id, active_page="library")

@router.get("/detail/{comic_id}", response_class=HTMLResponse)
async def detail(comic_id: str):
    return render("detail.html", comic_id=comic_id, active_page="library")

@router.get("/downloads", response_class=HTMLResponse)
async def downloads_page():
    return render("downloads.html", active_page="downloads")

@router.get("/stats", response_class=HTMLResponse)
async def stats_page():
    return render("stats.html", active_page="stats")

@router.get("/settings", response_class=HTMLResponse)
async def settings_page():
    return render("settings.html", active_page="settings")

@router.get("/history", response_class=HTMLResponse)
async def history_page():
    return render("history.html", active_page="history")

@router.get("/importer", response_class=HTMLResponse)
async def importer_page():
    return render("importer.html", active_page="importer")

@router.get("/collections", response_class=HTMLResponse)
async def collections_page():
    return render("collections.html", active_page="collections")
