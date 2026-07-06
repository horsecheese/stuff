"""routes/library.py — Library CRUD API"""
import io
import shutil
import zipfile
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from core import ComicDB, get_pages_for_comic
from core.cbz import sorted_images

router = APIRouter(prefix="/api")
LIBRARY_DIR: Path = None  # injected by main.py


def set_library_dir(path: Path):
    global LIBRARY_DIR
    LIBRARY_DIR = path


# ── Library queries ───────────────────────────────────────────────────────────

@router.get("/library")
async def get_library(
    sort: str = "date_added", order: str = "desc",
    search: str = "", tag: str = "", language: str = "",
    source: str = "", favorites: bool = False, unread: bool = False,
    collection: str = "", group_series: bool = False,
    author: str = "", artist: str = "", character: str = "",
    parody: str = "", tags_any: str = "",
    limit: int = 0, offset: int = 0,
):
    db = ComicDB()
    comics = db.get_all(
        sort=sort, order=order, search=search, tag=tag,
        language=language, source=source, favorites=favorites,
        unread=unread, collection=collection,
        author=author, artist=artist, character=character,
        parody=parody, tags_any=tags_any,
    )
    if group_series:
        comics = _group_series(comics)
    total = len(comics)
    if limit > 0:
        comics = comics[offset:offset + limit]
    return {"comics": comics, "total": total, "offset": offset, "limit": limit}


def _group_series(comics: list) -> list:
    """Collapse multi-chapter series into a single representative entry.
    A comic is considered part of a series when it has a non-empty 'series'
    field OR its title matches '* — Chapter *' / '* Ch. *'.
    The representative is the entry with the lowest chapter number (or first
    alphabetically). Individual nhentai / local entries pass through unchanged.
    """
    from collections import OrderedDict
    import re as _re

    def _series_key(c: dict) -> str | None:
        if c.get("series"):
            return c["series"].strip()
        t = c.get("title", "")
        m = _re.match(r"^(.+?)\s+(?:—\s*Chapter|Ch\.)\s*[\d.]+", t, _re.IGNORECASE)
        if m:
            return m.group(1).strip()
        return None

    groups: OrderedDict = OrderedDict()
    singles: list = []

    for c in comics:
        key = _series_key(c)
        if key:
            if key not in groups:
                groups[key] = {"_chapters": [], "_key": key}
            groups[key]["_chapters"].append(c)
        else:
            singles.append(c)

    result = []
    for key, grp in groups.items():
        chapters = grp["_chapters"]
        # Pick representative: best cover, then lowest chapter number
        rep = sorted(chapters, key=lambda c: (
            0 if c.get("cover_path") else 1,
            _parse_ch(c.get("chapter", "") or c.get("title", "")),
        ))[0].copy()
        rep["_is_series"]    = True
        rep["_series_key"]   = key
        rep["_chapter_count"] = len(chapters)
        rep["_chapter_ids"]  = [c["comic_id"] for c in chapters]
        # Aggregate: total pages, best rating, any favorite
        rep["page_count"]    = sum(c.get("page_count", 0) for c in chapters)
        rep["rating"]        = max((c.get("rating", 0) or 0) for c in chapters)
        rep["favorite"]      = any(c.get("favorite") for c in chapters)
        # Aggregate tags from all chapters (deduplicated, capped at 20)
        seen_tags: set = set()
        agg_tags: list = []
        for _c in chapters:
            for _t in (_c.get("tags") or []):
                if _t not in seen_tags:
                    seen_tags.add(_t)
                    agg_tags.append(_t)
        rep["tags"] = agg_tags[:20]
        result.append(rep)

    result.extend(singles)
    return result


def _parse_ch(s: str) -> float:
    """Extract first valid number from a string for chapter sorting."""
    import re as _re
    # Match proper floats/ints only (not bare dots)
    m = _re.search(r'\b\d+(?:\.\d+)?', s or "")
    if m:
        try:
            return float(m.group())
        except ValueError:
            pass
    return 9999.0


@router.get("/library/series/{series_key}/chapters")
async def get_series_chapters(series_key: str):
    """Return all chapters belonging to a series key, sorted by chapter number."""
    import re as _re
    db = ComicDB()
    # Fetch all comics that might belong to this series
    all_comics = db.get_all(sort="title", order="asc")
    def _matches(c):
        if c.get("series") and c["series"].strip() == series_key:
            return True
        t = c.get("title", "")
        m = _re.match(r"^(.+?)\s+(?:—\s*Chapter|Ch\.)\s*[\d.]+", t, _re.IGNORECASE)
        if m and m.group(1).strip() == series_key:
            return True
        return False

    chapters = [c for c in all_comics if _matches(c)]
    chapters.sort(key=lambda c: _parse_ch(c.get("chapter", "") or c.get("title", "")))
    return {"series_key": series_key, "chapters": chapters}


async def get_recent():
    return {"comics": ComicDB().get_recently_added(limit=12)}


@router.get("/library/reading")
async def get_reading():
    return {"comics": ComicDB().get_in_progress(limit=8)}


@router.get("/comic/{comic_id}")
async def get_comic(comic_id: str):
    c = ComicDB().get(comic_id)
    if not c:
        raise HTTPException(404, "Not found")
    return c


@router.get("/comic/{comic_id}/pages")
async def get_pages(comic_id: str):
    db = ComicDB()
    c = db.get(comic_id)
    if not c:
        raise HTTPException(404, "Not found")
    pages = get_pages_for_comic(c, LIBRARY_DIR)
    return {"pages": pages, "count": len(pages)}


# ── Mutations ─────────────────────────────────────────────────────────────────

@router.post("/comic/{comic_id}/favorite")
async def toggle_favorite(comic_id: str):
    return {"favorite": ComicDB().toggle_favorite(comic_id)}


@router.post("/comic/{comic_id}/rating")
async def set_rating(comic_id: str, request: Request):
    data = await request.json()
    ComicDB().set_rating(comic_id, float(data.get("rating", 0)))
    return {"ok": True}


@router.post("/comic/{comic_id}/progress")
async def update_progress(comic_id: str, request: Request):
    data = await request.json()
    ComicDB().update_progress(comic_id, int(data.get("page", 0)))
    return {"ok": True}


@router.post("/comic/{comic_id}/notes")
async def update_notes(comic_id: str, request: Request):
    data = await request.json()
    ComicDB().update_notes(comic_id, data.get("notes", ""))
    return {"ok": True}


@router.post("/comic/{comic_id}/collection")
async def add_to_collection(comic_id: str, request: Request):
    data = await request.json()
    ComicDB().add_to_collection(comic_id, data["name"])
    return {"ok": True}


@router.delete("/comic/{comic_id}/collection/{name}")
async def remove_from_collection(comic_id: str, name: str):
    ComicDB().remove_from_collection(comic_id, name)
    return {"ok": True}


@router.post("/comic/{comic_id}/edit")
async def edit_comic(comic_id: str, request: Request):
    data = await request.json()
    data["comic_id"] = comic_id
    ComicDB().upsert(data)
    return {"ok": True}


@router.delete("/comic/{comic_id}")
async def delete_comic(comic_id: str):
    db = ComicDB()
    c = db.get(comic_id)
    if not c:
        raise HTTPException(404, "Not found")
    comic_dir = LIBRARY_DIR / comic_id
    if comic_dir.exists():
        shutil.rmtree(comic_dir)
    db.delete(comic_id)
    return {"ok": True}


# ── Tags / Stats ──────────────────────────────────────────────────────────────

@router.get("/tags")
async def get_tags(field: str = "tags"):
    return {"tags": ComicDB().get_tag_counts(field)}


@router.get("/stats")
async def get_stats():
    return ComicDB().get_stats()


@router.get("/history")
async def get_history(limit: int = 100):
    return {"history": ComicDB().get_reading_history(limit=limit)}


# ── Series grouping (WeebCentral chapters → grouped view) ─────────────────────

@router.get("/series")
async def get_series():
    """Return WeebCentral comics grouped by series name."""
    db = ComicDB()
    comics = db.get_all(source="weebcentral", sort="title", order="asc")
    groups: dict = {}
    for c in comics:
        key = (
            c.get("series")
            or c.get("title", "").rsplit(" — Chapter", 1)[0].strip()
            or c["title"]
        )
        if key not in groups:
            groups[key] = {
                "series": key,
                "comics": [],
                "cover_path": c.get("cover_path", ""),
                "authors": c.get("authors", []),
                "tags": c.get("tags", []),
            }
        groups[key]["comics"].append(c)
    return {"series": list(groups.values())}


# ── CBZ page streaming ────────────────────────────────────────────────────────

@router.get("/comic/{comic_id}/page/{page_num}")
async def stream_page(comic_id: str, page_num: int):
    """Stream a single page image directly from CBZ."""
    db = ComicDB()
    c = db.get(comic_id)
    if not c:
        raise HTTPException(404)
    if c.get("cbz_path"):
        cbz_full = LIBRARY_DIR / c["cbz_path"]
        if cbz_full.exists():
            with zipfile.ZipFile(cbz_full) as zf:
                imgs = sorted_images(zf.namelist())
                if 0 < page_num <= len(imgs):
                    data = zf.read(imgs[page_num - 1])
                    ext = Path(imgs[page_num - 1]).suffix.lstrip(".")
                    mime = {
                        "jpg": "image/jpeg", "jpeg": "image/jpeg",
                        "png": "image/png", "gif": "image/gif",
                        "webp": "image/webp",
                    }.get(ext, "image/jpeg")
                    return StreamingResponse(io.BytesIO(data), media_type=mime)
    raise HTTPException(404, "Page not found")
