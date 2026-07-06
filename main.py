"""
ComicVault — main.py
Run: python main.py  →  http://localhost:7771
"""
import webbrowser
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader

from config import load_settings
from core import init_db, DownloadManager, NHentaiScraper, WeebCentralScraper

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
LIBRARY_DIR = BASE_DIR / "library"
LIBRARY_DIR.mkdir(exist_ok=True)

# ── Services ──────────────────────────────────────────────────────────────────
_cfg = load_settings()
dl_manager = DownloadManager(
    LIBRARY_DIR,
    max_concurrent=_cfg.get("max_concurrent", 2),
    max_concurrent_images=_cfg.get("max_concurrent_images", 6),
)
nh_scraper = NHentaiScraper()
wc_scraper = WeebCentralScraper()

# ── Jinja2 ────────────────────────────────────────────────────────────────────
_jinja = Environment(
    loader=FileSystemLoader(str(BASE_DIR / "templates")),
    auto_reload=True,
)

# ── Lifespan ──────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield
    await dl_manager.shutdown()

# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(title="ComicVault", lifespan=lifespan)
app.mount("/static",  StaticFiles(directory=BASE_DIR / "static"),  name="static")
app.mount("/library", StaticFiles(directory=LIBRARY_DIR),          name="library")

# ── Register routes ───────────────────────────────────────────────────────────
from routes import pages, library, collections, downloads, importer, settings as settings_route

# Inject dependencies into route modules
pages.set_jinja(_jinja)
library.set_library_dir(LIBRARY_DIR)
downloads.set_manager(dl_manager)
importer.set_deps(dl_manager, nh_scraper, wc_scraper, LIBRARY_DIR)
settings_route.set_manager(dl_manager)

app.include_router(pages.router)
app.include_router(library.router)
app.include_router(collections.router)
app.include_router(downloads.router)
app.include_router(downloads.ws_router)
app.include_router(importer.router)
app.include_router(settings_route.router)

# ── Entry ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    cfg = load_settings()
    print("╔═══════════════════════════════════════╗")
    print("║        ComicVault  v3.0.0             ║")
    print("║   http://localhost:7771               ║")
    print("╚═══════════════════════════════════════╝")
    if cfg.get("auto_open_browser", True):
        webbrowser.open("http://localhost:7771")
    uvicorn.run("main:app", host="0.0.0.0", port=7771, reload=False)
