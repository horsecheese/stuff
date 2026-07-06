"""
cbz_importer.py — Import CBZ/CBR/ZIP/folder comics into ComicVault

Handles:
  - .cbz (zip of images)
  - .cbr (rar — requires rarfile + unrar binary, gracefully skipped otherwise)
  - .zip  (same as cbz)
  - .pdf  (extracts pages via pymupdf if available)
  - Folder of images
  - ComicInfo.xml metadata (standard comic metadata spec)
"""

import io
import json
import os
import re
import shutil
import uuid
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Optional

from .database import ComicDB

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff", ".tif", ".avif"}


def natural_sort_key(s: str):
    return [int(c) if c.isdigit() else c.lower() for c in re.split(r"(\d+)", s)]


# ---------------------------------------------------------------------------
# ComicInfo.xml parser (standard metadata format)
# ---------------------------------------------------------------------------

def parse_comic_info(xml_text: str) -> dict:
    """Parse ComicInfo.xml into a metadata dict."""
    meta = {}
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return meta

    def g(tag, default=""):
        el = root.find(tag)
        return el.text.strip() if el is not None and el.text else default

    meta["title"]       = g("Title")
    meta["series"]      = g("Series")
    meta["volume"]      = g("Volume")
    meta["chapter"]     = g("Number")
    meta["year"]        = g("Year")
    meta["publisher"]   = g("Publisher")
    meta["language"]    = _normalize_lang(g("LanguageISO"))
    meta["description"] = g("Summary")
    meta["age_rating"]  = g("AgeRating")

    # People
    writers = [x.strip() for x in g("Writer").split(",") if x.strip()]
    pencilers = [x.strip() for x in g("Penciller").split(",") if x.strip()]
    meta["authors"]  = writers
    meta["artists"]  = pencilers or writers

    # Genres / tags
    meta["genres"] = [x.strip() for x in g("Genre").split(",") if x.strip()]
    meta["tags"]   = [x.strip() for x in g("Tags").split(",") if x.strip()]

    # Characters
    meta["characters"] = [x.strip() for x in g("Characters").split(",") if x.strip()]

    return meta


def _normalize_lang(lang: str) -> str:
    lang = lang.strip().lower()
    if lang in ("en", "english", "en-us", "en-gb"):
        return "EN"
    if lang in ("ja", "jp", "japanese"):
        return "JP"
    if lang in ("zh", "zh-cn", "zh-tw", "chinese"):
        return "ZH"
    if lang in ("ko", "korean"):
        return "KO"
    if lang in ("fr", "french"):
        return "FR"
    if lang in ("de", "german"):
        return "DE"
    if lang in ("es", "spanish"):
        return "ES"
    return lang.upper() if lang else ""


# ---------------------------------------------------------------------------
# Image helpers
# ---------------------------------------------------------------------------

def is_image(name: str) -> bool:
    return Path(name).suffix.lower() in IMAGE_EXTS


def sorted_images(names: list) -> list:
    return sorted([n for n in names if is_image(n)], key=natural_sort_key)


# ---------------------------------------------------------------------------
# Main import entry points
# ---------------------------------------------------------------------------

def import_cbz(cbz_path: Path, library_dir: Path,
               progress_cb=None) -> dict:
    """
    Import a CBZ/ZIP file. Extracts pages + cover, parses ComicInfo.xml.
    Returns the comic dict as stored in DB.
    """
    comic_id   = str(uuid.uuid4())
    dest_dir   = library_dir / comic_id
    pages_dir  = dest_dir / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)

    meta = {
        "title":  cbz_path.stem,
        "source": "local",
        "authors": [], "artists": [], "genres": [], "tags": [],
        "characters": [], "parodies": [], "groups": [], "language": "",
        "series": "", "volume": "", "chapter": "", "year": "",
        "publisher": "", "description": "", "age_rating": "",
    }

    # Copy CBZ; later we decide whether to keep it based on auto_cbz setting
    dest_cbz = dest_dir / cbz_path.name
    shutil.copy2(cbz_path, dest_cbz)
    _keep_cbz = True  # updated below after extraction

    try:
        with zipfile.ZipFile(cbz_path, "r") as zf:
            names = zf.namelist()

            # Parse ComicInfo.xml if present (check root and subdirs)
            ci_found = False
            for ci_name in ("ComicInfo.xml", "comicinfo.xml", "COMICINFO.XML"):
                # Direct match
                if ci_name in names:
                    try:
                        xml_text = zf.read(ci_name).decode("utf-8", errors="replace")
                        meta.update({k: v for k, v in parse_comic_info(xml_text).items() if v})
                        ci_found = True
                    except Exception:
                        pass
                    break
            if not ci_found:
                # Search in subdirectories
                for n in names:
                    if n.lower().endswith("comicinfo.xml"):
                        try:
                            xml_text = zf.read(n).decode("utf-8", errors="replace")
                            meta.update({k: v for k, v in parse_comic_info(xml_text).items() if v})
                        except Exception:
                            pass
                        break

            # Extract images in sorted order
            img_names = sorted_images(names)
            total = len(img_names)
            if total == 0:
                raise ValueError("No images found in archive")

            for i, name in enumerate(img_names, start=1):
                ext  = Path(name).suffix.lower()
                dest = pages_dir / f"{i:04d}{ext}"
                data = zf.read(name)
                dest.write_bytes(data)
                if progress_cb:
                    progress_cb(i, total)

    except zipfile.BadZipFile:
        # Try CBR (rar)
        extracted = _try_rar(cbz_path, pages_dir, progress_cb)
        if not extracted:
            raise ValueError("File is not a valid CBZ/ZIP and rarfile is unavailable for CBR")

    # Cover = first page copy
    first_pages = sorted(pages_dir.iterdir(), key=lambda p: natural_sort_key(p.name))
    cover_path = ""
    if first_pages:
        cover_src = first_pages[0]
        cover_dst = dest_dir / f"cover{cover_src.suffix}"
        shutil.copy2(cover_src, cover_dst)
        cover_path = f"/library/{comic_id}/cover{cover_src.suffix}"

    page_count = len(list(pages_dir.iterdir()))
    # Check auto_cbz setting — if disabled, remove the CBZ copy after extraction
    try:
        from config import load_settings as _ls
        _keep_cbz = _ls().get("auto_cbz", False)
    except Exception:
        _keep_cbz = False
    if not _keep_cbz and dest_cbz.exists():
        try:
            dest_cbz.unlink()
        except Exception:
            pass

    file_size  = cbz_path.stat().st_size / (1024 * 1024)

    record = {
        "comic_id":    comic_id,
        "source":      "local",
        "cover_path":  cover_path,
        "cbz_path":    str(dest_cbz.relative_to(library_dir)) if dest_cbz.exists() else None,
        "pages_dir":   str(pages_dir.relative_to(library_dir)),
        "page_count":  page_count,
        "file_size_mb": round(file_size, 2),
        "storage_type": "pages",
        "status":      "complete",
        "upload_date": meta.pop("upload_date", ""),
        **meta,
    }

    db = ComicDB()
    db.upsert(record)
    return record


def import_folder(folder: Path, library_dir: Path,
                  progress_cb=None) -> dict:
    """Import a folder of images as a comic."""
    comic_id  = str(uuid.uuid4())
    dest_dir  = library_dir / comic_id
    pages_dir = dest_dir / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)

    img_files = sorted_images([f.name for f in folder.iterdir() if f.is_file()])
    if not img_files:
        raise ValueError("No images found in folder")

    total = len(img_files)
    for i, name in enumerate(img_files, start=1):
        src  = folder / name
        ext  = Path(name).suffix.lower()
        dest = pages_dir / f"{i:04d}{ext}"
        shutil.copy2(src, dest)
        if progress_cb:
            progress_cb(i, total)

    first_pages = sorted(pages_dir.iterdir(), key=lambda p: natural_sort_key(p.name))
    cover_path = ""
    if first_pages:
        cover_dst = dest_dir / f"cover{first_pages[0].suffix}"
        shutil.copy2(first_pages[0], cover_dst)
        cover_path = f"/library/{comic_id}/cover{first_pages[0].suffix}"

    page_count = len(img_files)
    size_mb    = sum((folder / f).stat().st_size for f in img_files) / (1024 * 1024)

    record = {
        "comic_id":    comic_id,
        "title":       folder.name,
        "source":      "local",
        "cover_path":  cover_path,
        "pages_dir":   str(pages_dir.relative_to(library_dir)),
        "page_count":  page_count,
        "file_size_mb": round(size_mb, 2),
        "storage_type": "pages",
        "status":      "complete",
        "authors": [], "artists": [], "genres": [], "tags": [],
        "characters": [], "parodies": [], "groups": [],
    }
    db = ComicDB()
    db.upsert(record)
    return record


def register_cbz_download(cbz_path: Path, library_dir: Path,
                           source: str = "nhentai", source_id: str = "",
                           source_url: str = "", meta_override: dict = None,
                           progress_cb=None) -> dict:
    """
    Register a downloaded CBZ (e.g. from nhentai / WeebCentral download).
    Same as import_cbz but sets source/source_id properly and supports
    a series_cover_url override (downloads it and saves as the cover_path).
    """
    record = import_cbz(cbz_path, library_dir, progress_cb=progress_cb)
    record["source"]     = source
    record["source_id"]  = source_id
    record["source_url"] = source_url

    if meta_override:
        # Pop the special series_cover_url key before updating record
        series_cover_url = meta_override.pop("series_cover_url", None)
        # Use `is not None` so we don't skip empty strings or valid empty lists
        record.update({k: v for k, v in meta_override.items() if v is not None})

        # If a series cover URL was provided, download it and use it as the cover
        if series_cover_url and series_cover_url.startswith("http"):
            try:
                comic_id = record.get("comic_id", "")
                if comic_id:
                    cover_dest_dir = library_dir / "covers"
                    cover_dest_dir.mkdir(exist_ok=True)
                    ext = series_cover_url.rsplit(".", 1)[-1].split("?")[0].lower() or "jpg"
                    if ext not in ("jpg", "jpeg", "png", "webp", "gif"):
                        ext = "jpg"
                    # Use source_id for a stable cover filename shared across all chapters
                    stable_id    = source_id or comic_id
                    cover_fname  = f"series_{stable_id}.{ext}"
                    cover_path   = cover_dest_dir / cover_fname

                    # Try curl_cffi first (bypasses CDN bot protection), fall back to requests
                    downloaded = False
                    hdrs = {
                        "User-Agent": (
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/124.0.0.0 Safari/537.36"
                        ),
                        "Referer": "https://weebcentral.com/",
                    }
                    try:
                        from curl_cffi.requests import get as curl_get
                        resp = curl_get(series_cover_url, headers=hdrs, timeout=20,
                                        impersonate="chrome")
                        if resp.ok and len(resp.content) > 500:
                            cover_path.write_bytes(resp.content)
                            downloaded = True
                    except Exception:
                        pass

                    if not downloaded:
                        import requests as _req
                        resp = _req.get(series_cover_url, timeout=15, headers=hdrs)
                        if resp.ok and len(resp.content) > 500:
                            cover_path.write_bytes(resp.content)
                            downloaded = True

                    if downloaded:
                        record["cover_path"] = f"covers/{cover_fname}"
            except Exception:
                pass  # Keep whatever cover import_cbz found

    db = ComicDB()
    db.upsert(record)
    return record


def _try_rar(rar_path: Path, pages_dir: Path, progress_cb=None) -> bool:
    """Attempt CBR extraction via rarfile library."""
    try:
        import rarfile
        with rarfile.RarFile(str(rar_path)) as rf:
            names = rf.namelist()
            img_names = sorted_images(names)
            if not img_names:
                return False
            total = len(img_names)
            for i, name in enumerate(img_names, start=1):
                ext  = Path(name).suffix.lower()
                dest = pages_dir / f"{i:04d}{ext}"
                dest.write_bytes(rf.read(name))
                if progress_cb:
                    progress_cb(i, total)
        return True
    except Exception:
        return False


def get_pages_for_comic(comic: dict, library_dir: Path) -> list:
    """
    Return sorted list of page file paths (as /library/... URLs) for a comic.
    Works whether stored as extracted pages or raw CBZ.
    """
    if comic.get("pages_dir"):
        pd = library_dir / comic["pages_dir"]
        if pd.exists():
            files = sorted(
                [f for f in pd.iterdir() if is_image(f.name)],
                key=lambda p: natural_sort_key(p.name)
            )
            return [f"/library/{comic['pages_dir']}/{f.name}" for f in files]

    # Fallback: serve from CBZ on the fly (pages extracted to temp by API)
    return []
