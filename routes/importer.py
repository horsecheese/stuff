"""routes/importer.py — Import API (nhentai, WeebCentral, CBZ, favorites)"""
import asyncio
import shutil
import tempfile
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, Request, UploadFile

from core import ComicDB, DownloadManager
from core.nhentai import NHentaiScraper
from core.weebcentral import WeebCentralScraper
from core.cbz import import_cbz

router = APIRouter(prefix="/api/import")

# Injected by main.py
_dl_manager: DownloadManager = None
_nh_scraper: NHentaiScraper = None
_wc_scraper: WeebCentralScraper = None
LIBRARY_DIR: Path = None


def set_deps(dl_mgr, nh, wc, lib_dir):
    global _dl_manager, _nh_scraper, _wc_scraper, LIBRARY_DIR
    _dl_manager = dl_mgr
    _nh_scraper = nh
    _wc_scraper = wc
    LIBRARY_DIR = lib_dir


# ── CBZ upload ────────────────────────────────────────────────────────────────

@router.post("/cbz")
async def import_cbz_upload(files: list[UploadFile] = File(...)):
    results, errors = [], []
    for f in files:
        try:
            suffix = Path(f.filename).suffix.lower()
            if suffix not in (".cbz", ".zip", ".cbr"):
                errors.append({"file": f.filename, "error": "Unsupported file type"})
                continue
            tmp = Path(tempfile.mkdtemp()) / f.filename
            tmp.write_bytes(await f.read())
            record = await asyncio.to_thread(import_cbz, tmp, LIBRARY_DIR)
            shutil.rmtree(tmp.parent, ignore_errors=True)
            results.append({
                "file": f.filename,
                "comic_id": record["comic_id"],
                "title": record.get("title", ""),
            })
        except Exception as e:
            errors.append({"file": f.filename, "error": str(e)})
    return {"imported": results, "errors": errors}


# ── nhentai gallery import ────────────────────────────────────────────────────

@router.post("/nhentai")
async def import_nhentai(request: Request):
    """Queue nhentai galleries by URL or ID.
    method='scrape' (default): scrapes page and downloads images individually.
    method='download': fetches pre-built CBZ via API (requires api_key).
    """
    data = await request.json()
    raw = data.get("urls", "")
    method = data.get("method", "scrape")
    api_key = data.get("api_key", "").strip()
    lines = [
        l.strip()
        for l in (raw if isinstance(raw, str) else "\n".join(raw)).splitlines()
        if l.strip()
    ]

    queued, errors = [], []
    db = ComicDB()
    for line in lines:
        try:
            gid = _nh_scraper.parse_id(line)
            if not gid:
                errors.append({"url": line, "error": "Cannot parse gallery ID"})
                continue
            if db.get_by_source_id("nhentai", gid):
                errors.append({"url": line, "error": f"Already in library (#{gid})"})
                continue
            cbz_url = f"https://nhentai.net/api/v2/galleries/{gid}/download?format=cbz"
            job_id = await _dl_manager.enqueue_nhentai(gid, cbz_url, method=method, api_key=api_key)
            queued.append({"id": gid, "job_id": job_id})
        except Exception as e:
            errors.append({"url": line, "error": str(e)})

    return {"queued": queued, "errors": errors}


# ── nhentai favorites — fetch via API key ────────────────────────────────────

@router.post("/nhentai/favorites")
async def import_nhentai_favorites(request: Request):
    """Fetch nhentai favorites using the user's access_token (Bearer auth)
    and queue them for download. No cookies or Cloudflare bypassing needed.
    """
    data     = await request.json()
    api_key  = data.get("api_key", "").strip()
    method   = data.get("method", "scrape")
    max_pages = int(data.get("max_pages", 50))

    if not api_key:
        raise HTTPException(400, "api_key is required")

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Authorization": f"Bearer {api_key}",
        "Referer": "https://nhentai.net/",
    }

    all_ids: list[str] = []

    try:
        import httpx
        async with httpx.AsyncClient(follow_redirects=True, timeout=30,
                                      headers=headers) as client:
            for page in range(1, max_pages + 1):
                r = await client.get(
                    f"https://nhentai.net/api/v2/users/me/favorites",
                    params={"page": page, "per_page": 25}
                )
                if r.status_code in (401, 403):
                    raise HTTPException(
                        401,
                        "Invalid or expired API key — copy a fresh access_token from your nhentai cookies."
                    )
                if not r.is_success:
                    break
                d = r.json()
                results = d.get("result") or d.get("galleries") or []
                if not results:
                    break
                for g in results:
                    gid = str(g.get("id") or g.get("gallery_id", ""))
                    if gid.isdigit():
                        all_ids.append(gid)
                num_pages = d.get("num_pages", 1)
                if page >= num_pages:
                    break
    except HTTPException:
        raise
    except ImportError:
        # Fall back to sync requests
        import requests as _req
        for page in range(1, max_pages + 1):
            r = _req.get(
                "https://nhentai.net/api/v2/users/me/favorites",
                params={"page": page, "per_page": 25},
                headers=headers, timeout=30
            )
            if r.status_code in (401, 403):
                raise HTTPException(401, "Invalid or expired API key.")
            if not r.ok:
                break
            d = r.json()
            results = d.get("result") or d.get("galleries") or []
            if not results:
                break
            for g in results:
                gid = str(g.get("id") or g.get("gallery_id", ""))
                if gid.isdigit():
                    all_ids.append(gid)
            if page >= d.get("num_pages", 1):
                break

    db = ComicDB()
    queued, skipped = [], []

    for gid in all_ids:
        if db.get_by_source_id("nhentai", gid):
            skipped.append(gid)
            continue
        cbz_url = f"https://nhentai.net/api/v2/galleries/{gid}/download?format=cbz"
        job_id  = await _dl_manager.enqueue_nhentai(gid, cbz_url, method=method,
                                                     api_key=api_key if method == "download" else "")
        queued.append({"id": gid, "job_id": job_id})

    return {
        "total_found": len(all_ids),
        "queued":      queued,
        "skipped":     skipped,
        "errors":      [],
    }


# ── nhentai favorites (legacy endpoint — kept for external callers) ────────────
@router.post("/nhentai/favorites/ids")
async def import_nhentai_favorites_ids(request: Request):
    """Accept a raw list of pre-collected nhentai gallery IDs."""
    data    = await request.json()
    raw_ids = data.get("ids", [])
    method  = data.get("method", "scrape")

    if not raw_ids:
        raise HTTPException(400, "No IDs provided")

    if isinstance(raw_ids, str):
        import json as _json
        try:
            raw_ids = _json.loads(raw_ids)
        except Exception:
            raw_ids = [x.strip() for x in raw_ids.replace(",", "\n").splitlines() if x.strip()]

    db = ComicDB()
    queued, skipped, errors = [], [], []

    for gid in raw_ids:
        gid = str(gid).strip()
        if not gid.isdigit():
            errors.append({"id": gid, "error": "Not a valid gallery ID"})
            continue
        if db.get_by_source_id("nhentai", gid):
            skipped.append(gid)
            continue
        cbz_url = f"https://nhentai.net/api/v2/galleries/{gid}/download?format=cbz"
        job_id  = await _dl_manager.enqueue_nhentai(gid, cbz_url, method=method)
        queued.append({"id": gid, "job_id": job_id})

    return {"total_found": len(raw_ids), "queued": queued, "skipped": skipped, "errors": errors}


# ── nhentai favorites (curl_cffi with browser TLS impersonation) ──────────────

@router.post("/nhentai/favorites")
async def import_nhentai_favorites(request: Request):
    """Import nhentai favorites using the nhentai API v2 with curl_cffi.

    curl_cffi impersonates a real Chrome TLS fingerprint, which is required to
    pass Cloudflare's bot detection. The user must supply their browser cookies
    (cf_clearance + access_token + refresh_token) copied from DevTools.
    """
    import re as _re
    import json as _json

    data = await request.json()
    cookie_str = data.get("cookies", "").strip()
    method     = data.get("method", "scrape")
    max_pages  = min(int(data.get("max_pages", 50)), 200)

    if not cookie_str:
        raise HTTPException(400, "No session cookie provided")

    # Parse semicolon-separated cookie string → dict
    cookies: dict[str, str] = {}
    for part in cookie_str.split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            cookies[k.strip()] = v.strip()

    if "cf_clearance" not in cookies:
        raise HTTPException(
            400,
            "Cookie must include 'cf_clearance'. "
            "Copy the full Cookie header from a logged-in nhentai.net request in DevTools."
        )

    # Build a single cookie header string for curl_cffi
    cookie_header = "; ".join(f"{k}={v}" for k, v in cookies.items())

    # These headers closely match what Chrome sends to nhentai
    base_headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept":          "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Referer":         "https://nhentai.net/",
        "Origin":          "https://nhentai.net",
        "Cookie":          cookie_header,
        "Sec-Fetch-Dest":  "empty",
        "Sec-Fetch-Mode":  "cors",
        "Sec-Fetch-Site":  "same-origin",
    }

    # Add Authorization bearer if access_token is present
    if "access_token" in cookies:
        base_headers["Authorization"] = f"Bearer {cookies['access_token']}"

    ids: list[str] = []
    errors: list   = []
    strategy_used  = "none"

    def _fetch_favorites_sync() -> tuple[list[str], str]:
        """Runs in a thread — uses curl_cffi Session with Chrome impersonation."""
        try:
            from curl_cffi.requests import Session as CurlSession
        except ImportError:
            raise RuntimeError(
                "curl_cffi is not installed. Run: pip install curl_cffi"
            )

        collected: list[str] = []
        used_strategy = "none"

        with CurlSession(impersonate="chrome") as s:
            # ── Strategy 1: nhentai API v2 /users/me/favorites ──────────────
            # This is the cleanest approach — returns JSON with gallery IDs
            try:
                page = 1
                while page <= max_pages:
                    api_url = f"https://nhentai.net/api/v2/users/me/favorites?page={page}&per_page=25"
                    resp = s.get(api_url, headers=base_headers, timeout=30)

                    if resp.status_code in (401, 403):
                        break  # auth failed, fall through to HTML scrape
                    if resp.status_code == 404:
                        break  # endpoint not available
                    if not resp.ok:
                        break

                    try:
                        payload = resp.json()
                    except Exception:
                        break

                    # API v2 returns {"result": [...], "num_pages": N, ...}
                    result = payload.get("result") or payload.get("galleries") or []
                    if not result:
                        break

                    for item in result:
                        gid = item.get("id") or item.get("gallery_id")
                        if gid:
                            collected.append(str(gid))

                    num_pages = payload.get("num_pages", 1)
                    if page >= num_pages or len(result) == 0:
                        break
                    page += 1

                if collected:
                    used_strategy = "api_v2"
                    return collected, used_strategy
            except Exception:
                pass  # fall through

            # ── Strategy 2: Scrape /user/favorites/ HTML pages ──────────────
            # Uses the SvelteKit embedded JSON blobs OR href patterns.
            collected = []
            page = 1
            while page <= max_pages:
                html_url = f"https://nhentai.net/user/favorites/?page={page}"
                resp = s.get(html_url, headers={
                    **base_headers,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                }, timeout=30)

                if resp.status_code in (401, 403):
                    raise RuntimeError(
                        "Authentication failed — your cf_clearance cookie may have expired. "
                        "Please copy a fresh Cookie header from DevTools after reloading nhentai.net."
                    )
                if not resp.ok:
                    break

                html = resp.text
                found_this_page: list[str] = []

                # Sub-strategy A: SvelteKit embedded JSON blob
                for m in _re.finditer(
                    r'<script[^>]+data-sveltekit-fetched[^>]*>(.*?)</script>',
                    html, _re.DOTALL | _re.IGNORECASE
                ):
                    try:
                        outer    = _json.loads(m.group(1))
                        body_raw = outer.get("body", "")
                        body_obj = _json.loads(body_raw) if isinstance(body_raw, str) else body_raw
                        for entry in (body_obj.get("result") or body_obj.get("galleries") or []):
                            gid = entry.get("id") or entry.get("gallery_id")
                            if gid:
                                found_this_page.append(str(gid))
                    except Exception:
                        pass

                # Sub-strategy B: plain href scan
                if not found_this_page:
                    found_this_page = list(dict.fromkeys(
                        _re.findall(r'href=["\'](?:https://nhentai\.net)?/g/(\d+)/?["\']', html)
                    ))

                # Sub-strategy C: JSON data embedded in __NEXT_DATA__ / window.__data
                if not found_this_page:
                    for pattern in (
                        r'"id"\s*:\s*(\d{5,6})',
                        r'\"gallery_id\"\s*:\s*(\d{5,6})',
                    ):
                        found_this_page = list(dict.fromkeys(_re.findall(pattern, html)))
                        if found_this_page:
                            break

                if not found_this_page:
                    break  # last page reached

                collected.extend(found_this_page)
                page += 1

            used_strategy = "html_scrape" if collected else "none"
            return collected, used_strategy

    try:
        raw_ids, strategy_used = await asyncio.to_thread(_fetch_favorites_sync)
        ids = raw_ids
    except RuntimeError as e:
        raise HTTPException(401 if "Authentication" in str(e) else 502, str(e))
    except Exception as e:
        raise HTTPException(502, f"Failed to fetch favorites: {e}")

    # Deduplicate while preserving order
    seen: set[str] = set()
    unique_ids = [x for x in ids if not (x in seen or seen.add(x))]  # type: ignore[func-returns-value]

    db = ComicDB()
    queued, skipped = [], []
    for gid in unique_ids:
        if db.get_by_source_id("nhentai", gid):
            skipped.append(gid)
            continue
        cbz_url = f"https://nhentai.net/api/v2/galleries/{gid}/download?format=cbz"
        job_id  = await _dl_manager.enqueue_nhentai(gid, cbz_url, method=method)
        queued.append({"id": gid, "job_id": job_id})

    return {
        "total_found":    len(unique_ids),
        "queued":         queued,
        "skipped":        skipped,
        "errors":         errors,
        "strategy_used":  strategy_used,
    }


# ── WeebCentral ───────────────────────────────────────────────────────────────

@router.post("/weebcentral/preview")
async def weebcentral_preview(request: Request):
    data = await request.json()
    url = data.get("url", "").strip()
    parsed = _wc_scraper.parse_url(url)
    if not parsed:
        raise HTTPException(400, "Not a valid Weeb Central URL")
    try:
        series = await asyncio.to_thread(_wc_scraper.fetch_series, parsed["id"])
        return series
    except Exception as e:
        raise HTTPException(502, f"Scrape failed: {e}")


@router.post("/weebcentral")
async def import_weebcentral(request: Request):
    data = await request.json()
    series_url = data.get("series_url", "")
    chapter_urls = data.get("chapter_urls", [])
    chapter_objects = data.get("chapter_objects", [])
    if not chapter_urls and not chapter_objects:
        raise HTTPException(400, "No chapters selected")
    job_id = await _dl_manager.enqueue_weebcentral(
        series_url, chapter_urls, chapter_objects or None
    )
    return {"job_id": job_id}
