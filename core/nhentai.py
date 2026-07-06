"""
scraper.py — nhentai scraper for ComicVault

nhentai now uses SvelteKit. All gallery data is embedded as a JSON blob in:
  <script type="application/json" data-sveltekit-fetched
          data-url="/api/v2/galleries/{id}?include=...">{"status":200,...,"body":"..."}</script>

The body is a JSON-encoded string containing the full gallery API response with:
  id, media_id, title, tags (typed), num_pages, pages (with path/width/height), cover, thumbnail

Falls back to HTML parsing if the JSON blob is absent.
"""

import re
import json
import html as html_mod
import time
import threading
from typing import Optional, List, Dict
from pathlib import Path

try:
    from bs4 import BeautifulSoup
    HAS_BS4 = True
except ImportError:
    HAS_BS4 = False


# ---------------------------------------------------------------------------
# Image CDN config
# ---------------------------------------------------------------------------
IMAGE_BASES = [
    "https://i.nhentai.net/galleries",
    "https://i2.nhentai.net/galleries",
    "https://i3.nhentai.net/galleries",
]
THUMB_BASE = "https://t.nhentai.net/galleries"
# SvelteKit pages use t1/t2/t3/t4 subdomains
SVELTEKIT_THUMB_BASES = [
    "https://t1.nhentai.net/galleries",
    "https://t2.nhentai.net/galleries",
    "https://t3.nhentai.net/galleries",
    "https://t4.nhentai.net/galleries",
]

EXT_MAP  = {"j": "jpg", "p": "png", "g": "gif", "w": "webp"}
LANG_MAP = {"english": "EN", "japanese": "JP", "chinese": "ZH"}


class NHentaiScraper:

    def __init__(self):
        self._lock    = threading.Lock()
        self._session = None

    # ------------------------------------------------------------------
    # Session
    # ------------------------------------------------------------------

    def _get_session(self):
        if self._session is not None:
            return self._session
        try:
            import cloudscraper
            s = cloudscraper.create_scraper(
                browser={"browser": "chrome", "platform": "windows", "mobile": False}
            )
        except ImportError:
            import requests
            s = requests.Session()

        s.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://nhentai.net/",
        })
        self._session = s
        return s

    def _get_html(self, url: str, retries: int = 5) -> str:
        s = self._get_session()
        for attempt in range(retries):
            try:
                with self._lock:
                    resp = s.get(url, timeout=30)
                if resp.status_code == 429:
                    wait = 10 * (attempt + 1)
                    print(f"  [nhentai] Rate limited — waiting {wait}s…")
                    time.sleep(wait)
                    continue
                resp.raise_for_status()
                resp.encoding = "utf-8"
                return resp.text
            except Exception as exc:
                if attempt == retries - 1:
                    raise
                time.sleep(2 ** attempt)
        raise RuntimeError("Max retries exceeded")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def parse_id(self, url: str) -> Optional[str]:
        url = url.strip()
        if url.isdigit():
            return url
        for pattern in [
            r"nhentai\.net/g/(\d+)",
            r"nhentai\.net/api/(?:v2/)?galler(?:y|ies)/(\d+)",
            r"/(\d{3,7})/?$",
        ]:
            m = re.search(pattern, url)
            if m:
                return m.group(1)
        return None

    def fetch_metadata(self, gallery_id: str) -> dict:
        url       = f"https://nhentai.net/g/{gallery_id}/"
        page_html = self._get_html(url)
        return self._extract(page_html, gallery_id)

    # ------------------------------------------------------------------
    # Extraction — try SvelteKit JSON first, then HTML fallback
    # ------------------------------------------------------------------

    def _extract(self, page_html: str, gallery_id: str) -> dict:
        meta = self._empty(gallery_id)

        # Strategy 1: SvelteKit embedded JSON (current nhentai format)
        api_data = self._find_sveltekit_gallery_json(page_html, gallery_id)
        if api_data:
            self._from_api_json(api_data, meta)
            if meta["page_count"] > 0:
                return meta

        # Strategy 2: Legacy window._gallery JSON blob
        json_data = self._find_legacy_gallery_json(page_html)
        if json_data:
            self._from_legacy_json(json_data, meta)
            if meta["page_count"] > 0:
                return meta

        # Strategy 3: HTML parse (BS4 or regex)
        if HAS_BS4:
            soup = BeautifulSoup(page_html, "html.parser")
            self._from_bs4(soup, page_html, meta)
        else:
            self._from_regex(page_html, meta)

        return meta

    # ------------------------------------------------------------------
    # Strategy 1 — SvelteKit <script type="application/json"> blob
    # ------------------------------------------------------------------

    def _find_sveltekit_gallery_json(self, page_html: str, gallery_id: str) -> Optional[dict]:
        """
        nhentai SvelteKit pages embed API data as:
          <script type="application/json" data-sveltekit-fetched
                  data-url="/api/v2/galleries/{id}?include=...">
            {"status":200,"statusText":"OK","headers":{...},"body":"{...escaped JSON...}"}
          </script>

        The outer JSON "body" field is a JSON-encoded string of the actual gallery object.
        """
        # Find all sveltekit-fetched script tags
        pattern = re.compile(
            r'<script\s[^>]*data-sveltekit-fetched[^>]*>(.*?)</script>',
            re.DOTALL | re.IGNORECASE
        )
        for m in pattern.finditer(page_html):
            try:
                outer = json.loads(m.group(1))
                # Only interested in the gallery API call
                data_url = m.group(0)
                if f"/galleries/{gallery_id}" not in data_url and f"galleries/{gallery_id}" not in str(outer):
                    continue
                body_str = outer.get("body", "")
                if not body_str:
                    continue
                # body is a JSON-encoded string — parse it
                if isinstance(body_str, str):
                    gallery = json.loads(body_str)
                elif isinstance(body_str, dict):
                    gallery = body_str
                else:
                    continue
                # Validate it looks like a gallery object
                if "media_id" in gallery or "num_pages" in gallery or "pages" in gallery:
                    return gallery
            except Exception:
                continue
        return None

    def _from_api_json(self, data: dict, meta: dict):
        """Parse the /api/v2/galleries/{id} response format (new SvelteKit embed)."""
        meta["gallery_id"] = str(data.get("id", meta["gallery_id"]))
        meta["media_id"]   = str(data.get("media_id", ""))

        # Title
        titles = data.get("title", {})
        meta["title"]    = (titles.get("english") or titles.get("pretty") or
                            titles.get("japanese", ""))
        meta["title_jp"] = titles.get("japanese", "")

        meta["page_count"]  = data.get("num_pages", 0)
        meta["upload_date"] = str(data.get("upload_date", ""))
        meta["source_url"]  = f"https://nhentai.net/g/{meta['gallery_id']}/"

        # Tags — grouped by type
        by_type: Dict[str, List[str]] = {}
        for tag in data.get("tags", []):
            ttype = tag.get("type", "tag")
            name  = tag.get("name", "").strip()
            if name:
                by_type.setdefault(ttype, []).append(name)

        meta["tags"]       = by_type.get("tag", [])
        meta["artists"]    = by_type.get("artist", [])
        meta["characters"] = by_type.get("character", [])
        meta["parodies"]   = by_type.get("parody", [])
        meta["groups"]     = by_type.get("group", [])
        meta["categories"] = by_type.get("category", [])

        for lang_name in by_type.get("language", []):
            code = LANG_MAP.get(lang_name.lower())
            if code:
                meta["language"] = code
                break

        # Pages — new format uses {number, path, width, height, thumbnail}
        pages_raw = data.get("pages", [])
        if pages_raw and meta["media_id"]:
            for page in pages_raw:
                num  = page.get("number", 0)
                path = page.get("path", "")           # e.g. "galleries/1380631/1.jpg"
                ext  = path.rsplit(".", 1)[-1] if "." in path else "jpg"
                # Build full image URL from path
                img_url = f"https://i.nhentai.net/{path}" if path else \
                          f"{IMAGE_BASES[0]}/{meta['media_id']}/{num}.jpg"
                meta["pages"].append({
                    "index":    num,
                    "filename": f"{num:04d}.{ext}",
                    "url":      img_url,
                    "ext":      ext,
                    "media_id": meta["media_id"],
                })

        # Cover — new format has {path, width, height} under "cover" key
        cover = data.get("cover", {})
        cover_path = cover.get("path", "")
        if cover_path:
            meta["cover_url"] = f"https://t.nhentai.net/{cover_path}"
        elif meta["media_id"]:
            meta["cover_url"] = f"{THUMB_BASE}/{meta['media_id']}/cover.jpg"

        # Fallback page count from pages list
        if not meta["page_count"] and meta["pages"]:
            meta["page_count"] = len(meta["pages"])

    # ------------------------------------------------------------------
    # Strategy 2 — Legacy window._gallery JSON (old format)
    # ------------------------------------------------------------------

    def _find_legacy_gallery_json(self, page_html: str) -> Optional[dict]:
        # Format A: JSON.parse("...escaped...")
        m = re.search(
            r'window\._gallery\s*=\s*JSON\.parse\s*\(\s*["\'](.+?)["\'\s]*\)',
            page_html, re.DOTALL
        )
        if m:
            raw = m.group(1)
            try:
                return json.loads(json.loads(f'"{raw}"'))
            except Exception:
                try:
                    return json.loads(raw.replace('\\"', '"'))
                except Exception:
                    pass

        # Format B: bare inline object
        m = re.search(r'window\._gallery\s*=\s*(\{.+?\})\s*;', page_html, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(1))
            except Exception:
                pass

        return None

    def _from_legacy_json(self, data: dict, meta: dict):
        """Parse the old window._gallery format."""
        meta["gallery_id"] = str(data.get("id", meta["gallery_id"]))
        meta["media_id"]   = str(data.get("media_id", ""))

        titles = data.get("title", {})
        meta["title"]    = titles.get("english") or titles.get("pretty") or titles.get("japanese", "")
        meta["title_jp"] = titles.get("japanese", "")
        meta["page_count"]  = data.get("num_pages", 0)
        meta["upload_date"] = str(data.get("upload_date", ""))
        meta["source_url"]  = f"https://nhentai.net/g/{meta['gallery_id']}/"

        by_type: Dict[str, List[str]] = {}
        for tag in data.get("tags", []):
            ttype = tag.get("type", "tag")
            name  = tag.get("name", "").strip()
            if name:
                by_type.setdefault(ttype, []).append(name)

        meta["tags"]       = by_type.get("tag", [])
        meta["artists"]    = by_type.get("artist", [])
        meta["characters"] = by_type.get("character", [])
        meta["parodies"]   = by_type.get("parody", [])
        meta["groups"]     = by_type.get("group", [])
        meta["categories"] = by_type.get("category", [])

        for lang_name in by_type.get("language", []):
            code = LANG_MAP.get(lang_name.lower())
            if code:
                meta["language"] = code
                break

        # Pages from images.pages list (old format uses {t: "j"} type codes)
        if meta["media_id"]:
            pages_raw = data.get("images", {}).get("pages", [])
            for i, img in enumerate(pages_raw, start=1):
                ext = EXT_MAP.get(img.get("t", "j"), "jpg")
                meta["pages"].append({
                    "index":    i,
                    "filename": f"{i:04d}.{ext}",
                    "url":      f"{IMAGE_BASES[0]}/{meta['media_id']}/{i}.{ext}",
                    "ext":      ext,
                    "media_id": meta["media_id"],
                })
            cover_raw = data.get("images", {}).get("cover", {})
            cover_ext = EXT_MAP.get(cover_raw.get("t", "j"), "jpg")
            meta["cover_url"] = f"{THUMB_BASE}/{meta['media_id']}/cover.{cover_ext}"

    # ------------------------------------------------------------------
    # Strategy 3 — BeautifulSoup HTML parse
    # ------------------------------------------------------------------

    def _from_bs4(self, soup, page_html: str, meta: dict):
        # Title
        if not meta["title"]:
            for sel in ["span.pretty", "h1.title span", "h1.title", "h1"]:
                el = soup.select_one(sel)
                if el:
                    meta["title"] = el.get_text(strip=True)
                    break

        # media_id from thumbnail src patterns (t1.nhentai.net/galleries/{id}/...)
        if not meta["media_id"]:
            for img in soup.select("img[src*='/galleries/'], img[data-src*='/galleries/']"):
                src = img.get("data-src") or img.get("src", "")
                m = re.search(r"/galleries/(\d+)/", src)
                if m:
                    meta["media_id"] = m.group(1)
                    break

        # Page count from thumbnail grid
        if not meta["page_count"]:
            thumbs = soup.select(".thumb-container, .gallerythumb")
            meta["page_count"] = len(thumbs)

        # Tags — nhentai SvelteKit uses class="tagchip" with <span class="name">
        if not any([meta["tags"], meta["artists"], meta["characters"], meta["parodies"]]):
            self._parse_tags_bs4_sveltekit(soup, meta)

        # Build pages
        if not meta["pages"] and meta["media_id"] and meta["page_count"]:
            self._build_pages_from_thumbs(soup, meta)

        # Upload date
        if not meta["upload_date"]:
            t = soup.find("time")
            if t and t.get("datetime"):
                meta["upload_date"] = t["datetime"]

    def _parse_tags_bs4_sveltekit(self, soup, meta: dict):
        """
        Parse nhentai's SvelteKit tag layout:
          <div class="tag-container field-name svelte-iec8wt">
            Parodies:
            <span class="tags svelte-iec8wt">
              <a class="tagchip variant-pill ..."><span class="name svelte-mmywhv">original</span></a>
            </span>
          </div>
        """
        for container in soup.select("div.tag-container"):
            # Get the label text (text node before <span class="tags">)
            label_text = ""
            for child in container.children:
                if hasattr(child, 'name') and child.name:
                    break
                label_text += str(child)
            label = label_text.strip().lower().rstrip(":")

            # Also try explicit label elements
            if not label:
                label_el = container.find(["label", "strong", "b"])
                if label_el:
                    label = label_el.get_text(strip=True).lower().rstrip(":")

            # Collect names from tagchip spans
            names = []
            for chip in container.select("a.tagchip, a.tag"):
                name_span = chip.select_one("span.name")
                name = name_span.get_text(strip=True) if name_span else chip.get_text(strip=True)
                name = name.strip()
                if name and name not in ("+", "–", "-", ""):
                    names.append(name)

            if not names:
                continue

            if "parody" in label or "parodies" in label:
                meta["parodies"] = names
            elif "tag" in label:
                meta["tags"] = names
            elif "artist" in label:
                meta["artists"] = names
            elif "group" in label:
                meta["groups"] = names
            elif "character" in label:
                meta["characters"] = names
            elif "language" in label:
                for n in names:
                    code = LANG_MAP.get(n.lower())
                    if code:
                        meta["language"] = code
                        break
            elif "categor" in label:
                meta["categories"] = names

    def _build_pages_from_thumbs(self, soup, meta: dict):
        thumbs = soup.select(".thumb-container img, .gallerythumb img")
        for i, img in enumerate(thumbs, start=1):
            src = img.get("data-src") or img.get("src", "")
            m = re.search(r"\.(\w+)(?:\?|$)", src)
            ext = m.group(1) if m else "jpg"
            meta["pages"].append({
                "index":    i,
                "filename": f"{i:04d}.{ext}",
                "url":      f"{IMAGE_BASES[0]}/{meta['media_id']}/{i}.{ext}",
                "ext":      ext,
                "media_id": meta["media_id"],
            })
        if not meta["cover_url"] and meta["media_id"]:
            meta["cover_url"] = f"{THUMB_BASE}/{meta['media_id']}/cover.jpg"

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _from_regex(self, page_html: str, meta: dict):
        """Last-resort regex extraction when BS4 unavailable."""
        if not meta["media_id"]:
            m = re.search(r"/galleries/(\d+)/", page_html)
            if m:
                meta["media_id"] = m.group(1)

        if not meta["title"]:
            for pat in [
                r'<span[^>]*class="[^"]*pretty[^"]*"[^>]*>([^<]+)',
                r'<h1[^>]*>([^<]+)',
            ]:
                m = re.search(pat, page_html, re.IGNORECASE)
                if m:
                    meta["title"] = html_mod.unescape(m.group(1).strip())
                    break

        if not meta["page_count"]:
            meta["page_count"] = len(re.findall(r'class="[^"]*thumb-container[^"]*"', page_html))

        if not meta["pages"] and meta["media_id"] and meta["page_count"]:
            for i in range(1, meta["page_count"] + 1):
                meta["pages"].append({
                    "index": i, "filename": f"{i:04d}.jpg",
                    "url": f"{IMAGE_BASES[0]}/{meta['media_id']}/{i}.jpg",
                    "ext": "jpg", "media_id": meta["media_id"],
                })
            meta["cover_url"] = f"{THUMB_BASE}/{meta['media_id']}/cover.jpg"

    def _empty(self, gallery_id: str) -> dict:
        return {
            "gallery_id":  gallery_id,
            "media_id":    "",
            "title":       "",
            "title_jp":    "",
            "cover_url":   "",
            "page_count":  0,
            "language":    "unknown",
            "tags":        [],
            "artists":     [],
            "parodies":    [],
            "characters":  [],
            "groups":      [],
            "categories":  [],
            "upload_date": "",
            "source_url":  f"https://nhentai.net/g/{gallery_id}/",
            "pages":       [],
        }
