"""
weebcentral.py — Weeb Central scraper adapted from Yui007/weebcentral_downloader
MIT License. Core scraping logic ported and integrated into ComicVault.

Key endpoints discovered from the working scraper:
  - Series page:      https://weebcentral.com/series/{id}
  - Full chapter list: https://weebcentral.com/series/{id}/full-chapter-list
  - Chapter images:   https://weebcentral.com/chapters/{id}/images?reading_style=long_strip
"""

import re
import time
import logging
import random
from threading import Lock
from typing import Optional, List
from urllib.parse import urljoin, urlparse

try:
    from bs4 import BeautifulSoup
    HAS_BS4 = True
except ImportError:
    HAS_BS4 = False

logger = logging.getLogger(__name__)

LANG_MAP = {
    "english": "EN", "japanese": "JP", "chinese": "ZH",
    "korean": "KO", "french": "FR", "spanish": "ES",
    "portuguese": "PT", "italian": "IT", "russian": "RU",
}


class WeebCentralScraper:
    BASE = "https://weebcentral.com"

    def __init__(self):
        self._lock = Lock()
        self._session = None
        self.base_delay = 1.0
        self.delay = 1.0
        self.rate_limit_hits = 0
        self.last_rate_limit_time = 0

    # ------------------------------------------------------------------
    # Session
    # ------------------------------------------------------------------

    def _get_session(self):
        if self._session:
            return self._session
        import requests
        s = requests.Session()
        s.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate",
            "Connection": "keep-alive",
            "Pragma": "no-cache",
            "Cache-Control": "no-cache",
        })
        self._session = s
        return s

    def _backoff(self, attempt: int, base: float = 2.0, max_d: float = 60.0) -> float:
        d = min(base * (2 ** attempt), max_d)
        jitter = d * 0.2 * (random.random() - 0.5) * 2
        return max(0.5, d + jitter)

    def _adjust_rate_limit(self):
        with self._lock:
            now = time.time()
            if now - self.last_rate_limit_time < 60:
                self.rate_limit_hits += 1
                self.delay = min(self.base_delay * (1.5 ** self.rate_limit_hits), 10.0)
            else:
                self.rate_limit_hits = 0
                self.delay = self.base_delay
            self.last_rate_limit_time = now

    def _fetch(self, url: str, retries: int = 5) -> "requests.Response":
        """
        Fetch URL with retry + rate-limit handling.
        Tries FlareSolverr as fallback on 403/503/Cloudflare.
        """
        s = self._get_session()
        last_exc = None

        for attempt in range(retries):
            try:
                with self._lock:
                    resp = s.get(url, timeout=20)

                if resp.status_code == 429:
                    self._adjust_rate_limit()
                    wait = self._backoff(attempt, base=1, max_d=30)
                    logger.warning(f"[WC] 429 rate limit — waiting {wait:.1f}s")
                    time.sleep(wait)
                    continue

                # Cloudflare challenge check
                is_cf = resp.status_code in (403, 503)
                if not is_cf and resp.text:
                    t = resp.text
                    if "Just a moment" in t or "Enable JavaScript" in t:
                        is_cf = True

                if is_cf:
                    logger.warning(f"[WC] Cloudflare on {url} — trying FlareSolverr")
                    fs_resp = self._try_flaresolverr(url)
                    if fs_resp:
                        return fs_resp
                    wait = self._backoff(attempt)
                    time.sleep(wait)
                    continue

                resp.raise_for_status()
                return resp

            except Exception as e:
                last_exc = e
                if attempt < retries - 1:
                    wait = self._backoff(attempt)
                    logger.warning(f"[WC] Request failed ({e}) — retry {attempt+1} in {wait:.1f}s")
                    time.sleep(wait)

        raise last_exc or RuntimeError(f"Failed to fetch {url}")

    def _try_flaresolverr(self, url: str):
        """Optional FlareSolverr fallback on localhost:8191."""
        try:
            import requests as _r
            payload = {"cmd": "request.get", "url": url, "maxTimeout": 60000}
            r = _r.post("http://localhost:8191/v1", json=payload, timeout=65)
            if r.status_code == 200:
                data = r.json()
                if data.get("status") == "ok":
                    # Return a fake response-like object
                    class _Resp:
                        status_code = 200
                        content = data["solution"]["response"].encode("utf-8")
                        text = data["solution"]["response"]
                    return _Resp()
        except Exception:
            pass
        return None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def parse_url(self, url: str) -> Optional[dict]:
        """Return {"type": "series"|"chapter", "id": str, "url": str} or None."""
        url = url.strip()
        for pat, typ in [
            (r"weebcentral\.com/series/([^/?#]+)", "series"),
            (r"weebcentral\.com/chapters/([^/?#/]+)", "chapter"),
        ]:
            m = re.search(pat, url)
            if m:
                return {"type": typ, "id": m.group(1), "url": url}
        return None

    def fetch_series(self, series_id_or_url: str) -> dict:
        """
        Fetch series metadata + full chapter list.
        Accepts either a series ID or a full URL.
        """
        if series_id_or_url.startswith("http"):
            url = series_id_or_url
            parsed = self.parse_url(url)
            series_id = parsed["id"] if parsed else url.rstrip("/").split("/")[-1]
        else:
            series_id = series_id_or_url
            url = f"{self.BASE}/series/{series_id}"

        # Fetch main series page for metadata + cover
        resp = self._fetch(url)
        soup = BeautifulSoup(resp.content, "html.parser") if HAS_BS4 else None

        meta = {
            "source":     "weebcentral",
            "source_id":  series_id,
            "source_url": url,
            "title": "", "title_alt": "", "description": "",
            "authors": [], "artists": [], "genres": [], "tags": [],
            "language": "EN", "cover_url": "",
            "chapters": [],
        }

        if soup:
            self._parse_series_page(soup, meta)

        # Fetch full chapter list from dedicated endpoint
        # Pattern from working scraper: /series/{id}/full-chapter-list
        chapter_list_url = self._build_chapter_list_url(url)
        chapters = self._fetch_chapter_list(chapter_list_url)
        meta["chapters"] = chapters

        return meta

    def fetch_chapter_images(self, chapter_url: str) -> List[str]:
        """
        Return list of image URLs for a chapter.
        Appends /images?reading_style=long_strip as discovered in working scraper.
        """
        images_url = chapter_url.rstrip("/") + "/images?reading_style=long_strip"
        logger.info(f"[WC] Fetching images: {images_url}")

        try:
            resp = self._fetch(images_url)
            if HAS_BS4:
                soup = BeautifulSoup(resp.content, "html.parser")
                images = []
                for img in soup.find_all("img"):
                    src = img.get("src")
                    if isinstance(src, list): src = src[0]
                    if src and "broken_image" not in src and src.startswith("http"):
                        images.append(src)
                logger.info(f"[WC] Found {len(images)} images")
                return images
            else:
                # Regex fallback
                import re as _re
                return _re.findall(r'<img[^>]+src=["\']([^"\']+)["\']', resp.text)
        except Exception as e:
            logger.error(f"[WC] Failed to fetch chapter images: {e}")
            return []

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _build_chapter_list_url(self, manga_url: str) -> str:
        """Build full-chapter-list URL from manga series URL."""
        parsed = urlparse(manga_url)
        parts  = parsed.path.split("/")
        # /series/{id}/... → /series/{id}/full-chapter-list
        base_path = "/".join(parts[:3])
        return f"{self.BASE}{base_path}/full-chapter-list"

    def _fetch_chapter_list(self, url: str) -> List[dict]:
        """Fetch and parse the full chapter list page."""
        logger.info(f"[WC] Fetching chapter list: {url}")
        try:
            resp = self._fetch(url)
        except Exception as e:
            logger.error(f"[WC] Chapter list fetch failed: {e}")
            return []

        if not HAS_BS4:
            # Regex fallback
            import re as _re
            chapters = []
            for m in _re.finditer(r'href=["\']([^"\']*weebcentral\.com/chapters/[^"\']+)["\']', resp.text):
                chapters.append({"url": m.group(1), "title": "", "number": ""})
            return list(reversed(chapters))

        soup = BeautifulSoup(resp.content, "html.parser")
        chapters = []

        # From working scraper: `div[x-data] > a`
        elements = soup.select("div[x-data] > a")
        if not elements:
            # Fallback selectors
            elements = soup.select("a[href*='/chapters/']")

        for el in reversed(elements):  # reversed = oldest first
            href = el.get("href", "")
            if not href:
                continue
            if not href.startswith("http"):
                href = urljoin(self.BASE, href)

            # Title from nested span (as in working scraper)
            name_el = el.select_one("span.flex > span, span")
            name = name_el.get_text(strip=True) if name_el else el.get_text(strip=True)

            # Extract chapter number
            num_m = re.search(r"(\d+(?:\.\d+)?)", name)
            num = num_m.group(1) if num_m else ""

            # Extract upload date from <time> element if present
            time_el = el.select_one("time[datetime], time")
            upload_date = ""
            if time_el:
                upload_date = time_el.get("datetime", "") or time_el.get_text(strip=True)

            chapters.append({"url": href, "title": name, "number": num, "upload_date": upload_date})

        logger.info(f"[WC] Found {len(chapters)} chapters")
        return chapters

    def _parse_series_page(self, soup, meta: dict):
        """Extract series metadata from the main series page."""
        # Title — from working scraper: section[x-data] > section:nth-of-type(2) h1
        for sel in [
            "section[x-data] > section:nth-of-type(2) h1",
            "h1.series-name", "h1",
        ]:
            el = soup.select_one(sel)
            if el:
                meta["title"] = el.get_text(strip=True)
                break

        # Cover image — `img[alt$='cover']` from working scraper
        cover = soup.select_one("img[alt$='cover'], img[alt*='cover'], .cover img")
        if cover:
            src = cover.get("src") or cover.get("data-src", "")
            if src and not src.startswith("http"):
                src = urljoin(self.BASE, src)
            meta["cover_url"] = src

        # Description
        for sel in [".description", ".synopsis", ".summary", "p.text-sm"]:
            el = soup.select_one(sel)
            if el:
                meta["description"] = el.get_text(strip=True)
                break

        # Metadata rows (author, artist, genre, language, etc.)
        # WeebCentral typically uses a dl/dt/dd or ul/li pattern
        for row in soup.select("li, tr, .meta-row, [class*='info'] > div"):
            text = row.get_text(separator=" ", strip=True)
            label_el = row.select_one("strong, b, .label, dt, th")
            if not label_el:
                continue
            label = label_el.get_text(strip=True).lower().rstrip(":")
            vals  = [a.get_text(strip=True) for a in row.select("a")]
            if not vals:
                raw = text[len(label_el.get_text()):].strip().lstrip(":").strip()
                vals = [v.strip() for v in raw.split(",") if v.strip()]

            vals = [v for v in vals if v]
            if not vals:
                continue

            if "author" in label or "writer" in label:
                meta["authors"] = vals
            elif "artist" in label or "illustrat" in label:
                meta["artists"] = vals
            elif "genre" in label:
                meta["genres"] = vals
            elif "tag" in label:
                meta["tags"] = vals
            elif "language" in label:
                for v in vals:
                    code = LANG_MAP.get(v.lower())
                    if code:
                        meta["language"] = code
                        break
