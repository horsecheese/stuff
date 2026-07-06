"""
downloader.py — Async download manager for ComicVault
Handles nhentai CBZ downloads, WeebCentral chapter downloads, and generic URL queues.
"""

import asyncio
import json
import os
import shutil
import time
import zipfile
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional

from .database import ComicDB
from .cbz import register_cbz_download, import_cbz


class Status(str, Enum):
    QUEUED      = "queued"
    SCRAPING    = "scraping"
    DOWNLOADING = "downloading"
    PROCESSING  = "processing"
    COMPLETE    = "complete"
    ERROR       = "error"
    CANCELLED   = "cancelled"


@dataclass
class DownloadJob:
    job_id:        str
    url:           str
    source:        str = "nhentai"   # nhentai | weebcentral | direct_cbz
    status:        Status = Status.QUEUED
    title:         str = ""
    total_pages:   int = 0
    done_pages:    int = 0
    failed_pages:  List[int] = field(default_factory=list)
    speed_kbps:    float = 0.0
    eta_seconds:   int = 0
    error_msg:     str = ""
    started_at:    float = 0.0
    cover_url:     str = ""
    comic_id:      str = ""
    cancelled:     bool = False
    # nhentai specific
    method:        str = "scrape"    # scrape | download
    api_key:       str = ""
    # WeebCentral specific
    chapters:      List[dict] = field(default_factory=list)
    current_chapter: str = ""


class DownloadManager:
    MAX_CONCURRENT = 2

    def __init__(self, library_dir: Path, max_concurrent: int = 2,
                 max_concurrent_images: int = 6):
        self.library_dir = library_dir
        self.MAX_CONCURRENT = max_concurrent
        self.max_concurrent_images = max_concurrent_images
        self.jobs: Dict[str, DownloadJob] = {}
        self.queue: asyncio.Queue = asyncio.Queue()
        self._workers: List[asyncio.Task] = []
        self._started = False

    def _ensure_workers(self):
        if not self._started:
            self._started = True
            loop = asyncio.get_event_loop()
            for _ in range(self.MAX_CONCURRENT):
                self._workers.append(loop.create_task(self._worker()))

    async def enqueue_nhentai(self, nhentai_id: str, cbz_url: str,
                               method: str = "scrape", api_key: str = "") -> str:
        """Queue an nhentai import. method='scrape' (default) or 'download'."""
        self._ensure_workers()
        job = DownloadJob(job_id=nhentai_id, url=cbz_url, source="nhentai",
                          method=method, api_key=api_key)
        self.jobs[nhentai_id] = job
        await self.queue.put(nhentai_id)
        return nhentai_id

    async def enqueue_weebcentral(self, series_url: str, chapter_urls: List[str],
                                   chapter_objects: List[dict] = None) -> str:
        self._ensure_workers()
        import hashlib
        job_id = hashlib.md5(series_url.encode()).hexdigest()[:12]
        if chapter_objects:
            chapters = chapter_objects
        else:
            chapters = [{"url": u} for u in chapter_urls]
        job = DownloadJob(job_id=job_id, url=series_url, source="weebcentral",
                          chapters=chapters)
        self.jobs[job_id] = job
        await self.queue.put(job_id)
        return job_id

    async def enqueue_cbz_file(self, file_path: str) -> str:
        self._ensure_workers()
        import hashlib
        job_id = hashlib.md5(file_path.encode()).hexdigest()[:12]
        job = DownloadJob(job_id=job_id, url=file_path, source="local_cbz")
        self.jobs[job_id] = job
        await self.queue.put(job_id)
        return job_id

    def cancel(self, job_id: str):
        if job_id in self.jobs:
            self.jobs[job_id].cancelled = True
            self.jobs[job_id].status = Status.CANCELLED

    async def retry(self, job_id: str):
        if job_id in self.jobs:
            j = self.jobs[job_id]
            j.status = Status.QUEUED
            j.cancelled = False
            j.error_msg = ""
            j.done_pages = 0
            j.failed_pages = []
            await self.queue.put(job_id)

    def get_status(self) -> dict:
        return {"jobs": [self._job_dict(j) for j in self.jobs.values()]}

    def _job_dict(self, j: DownloadJob) -> dict:
        pct = round(j.done_pages / j.total_pages * 100) if j.total_pages else 0
        is_wc = j.source == "weebcentral"
        return {
            "job_id":          j.job_id,
            "url":             j.url,
            "source":          j.source,
            "title":           j.title,
            "status":          j.status,
            "total_pages":     j.total_pages,
            "done_pages":      j.done_pages,
            "failed_pages":    j.failed_pages,
            "percent":         pct,
            "speed_kbps":      round(j.speed_kbps, 1),
            "eta_seconds":     j.eta_seconds,
            "error_msg":       j.error_msg,
            "cover_url":       j.cover_url,
            "comic_id":        j.comic_id,
            "current_chapter": j.current_chapter,
            "is_chapters":     is_wc,
        }

    async def _worker(self):
        while True:
            job_id = await self.queue.get()
            job = self.jobs.get(job_id)
            if not job or job.cancelled:
                self.queue.task_done()
                continue
            try:
                if job.source == "nhentai":
                    if job.method == "download":
                        await self._process_nhentai_download(job)
                    else:
                        await self._process_nhentai_scrape(job)
                elif job.source == "weebcentral":
                    await self._process_weebcentral(job)
                elif job.source == "local_cbz":
                    await self._process_local_cbz(job)
            except Exception as e:
                job.status = Status.ERROR
                job.error_msg = str(e)
            finally:
                self.queue.task_done()

    # ------------------------------------------------------------------
    # nhentai: parallel image download + CBZ (default, no auth needed)
    # ------------------------------------------------------------------

    async def _process_nhentai_scrape(self, job: DownloadJob):
        from .nhentai import NHentaiScraper, IMAGE_BASES
        scraper = NHentaiScraper()
        nhentai_id = job.job_id

        job.status = Status.SCRAPING
        try:
            meta = await asyncio.to_thread(scraper.fetch_metadata, nhentai_id)
        except Exception as e:
            raise RuntimeError(f"Metadata scrape failed: {e}")

        job.title       = meta.get("title", f"nhentai #{nhentai_id}")
        job.cover_url   = meta.get("cover_url", "")
        pages           = meta.get("pages", [])
        job.total_pages = len(pages) or meta.get("page_count", 0)

        if not pages:
            raise RuntimeError("No pages found — gallery may be unavailable or region-blocked")

        job.status = Status.DOWNLOADING

        tmp_dir = self.library_dir / f"_nh_{nhentai_id}"
        tmp_dir.mkdir(exist_ok=True)

        # Parallel image download with semaphore
        sem = asyncio.Semaphore(self.max_concurrent_images)
        t0  = time.time()
        downloaded_bytes = 0
        lock = asyncio.Lock()

        async def download_page(i: int, page: dict):
            nonlocal downloaded_bytes
            async with sem:
                if job.cancelled:
                    return
                img_url = page.get("url", "")
                ext     = page.get("ext", "jpg")
                dest    = tmp_dir / f"{i:04d}.{ext}"
                candidate_urls = [img_url] + [
                    f"{base}/{meta['media_id']}/{i}.{ext}"
                    for base in IMAGE_BASES[1:]
                    if meta.get("media_id")
                ]
                success = False
                for url in candidate_urls:
                    try:
                        nbytes = await self._download_binary_counted(
                            url, dest,
                            referer=f"https://nhentai.net/g/{nhentai_id}/"
                        )
                        if dest.exists() and dest.stat().st_size > 100:
                            success = True
                            async with lock:
                                downloaded_bytes += nbytes
                                elapsed = time.time() - t0
                                if elapsed > 0:
                                    job.speed_kbps = (downloaded_bytes / 1024) / elapsed
                            break
                    except Exception:
                        if dest.exists():
                            dest.unlink(missing_ok=True)

                async with lock:
                    if not success:
                        job.failed_pages.append(i)
                    job.done_pages += 1

        await asyncio.gather(*[download_page(i, p) for i, p in enumerate(pages, 1)])

        if job.cancelled:
            job.status = Status.CANCELLED
            shutil.rmtree(tmp_dir, ignore_errors=True)
            return

        job.status = Status.PROCESSING

        cbz_path = self.library_dir / f"_nh_{nhentai_id}.cbz"
        with zipfile.ZipFile(cbz_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for img_file in sorted(tmp_dir.iterdir(), key=lambda p: natural_sort_key(p.name)):
                zf.write(img_file, img_file.name)

        shutil.rmtree(tmp_dir, ignore_errors=True)

        def _import():
            return register_cbz_download(
                cbz_path, self.library_dir,
                source="nhentai",
                source_id=nhentai_id,
                source_url=f"https://nhentai.net/g/{nhentai_id}/",
                meta_override={
                    "title":       meta.get("title", ""),
                    "title_alt":   meta.get("title_jp", ""),
                    "tags":        meta.get("tags", []),
                    "artists":     meta.get("artists", []),
                    "parodies":    meta.get("parodies", []),
                    "characters":  meta.get("characters", []),
                    "groups":      meta.get("groups", []),
                    "categories":  meta.get("categories", []),
                    "language":    meta.get("language", ""),
                    "source_url":  f"https://nhentai.net/g/{nhentai_id}/",
                    "upload_date": meta.get("upload_date", ""),
                }
            )

        record = await asyncio.to_thread(_import)
        job.comic_id    = record["comic_id"]
        job.cover_url   = record.get("cover_path", "")
        job.total_pages = record.get("page_count", job.total_pages)
        job.done_pages  = job.total_pages

        if cbz_path.exists():
            cbz_path.unlink()

        job.status = Status.COMPLETE

    # ------------------------------------------------------------------
    # nhentai: direct CBZ download (requires API key / logged-in session)
    # ------------------------------------------------------------------

    async def _process_nhentai_download(self, job: DownloadJob):
        from .nhentai import NHentaiScraper
        scraper = NHentaiScraper()
        nhentai_id = job.job_id

        job.status = Status.SCRAPING
        try:
            meta = await asyncio.to_thread(scraper.fetch_metadata, nhentai_id)
            job.title       = meta.get("title", f"Gallery {nhentai_id}")
            job.cover_url   = meta.get("cover_url", "")
            job.total_pages = meta.get("page_count", 0)
        except Exception:
            job.title = f"nhentai #{nhentai_id}"
            meta = {}

        job.status = Status.DOWNLOADING

        api_key = job.api_key.strip()
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Referer": f"https://nhentai.net/g/{nhentai_id}/",
        }
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        cbz_url  = f"https://nhentai.net/api/v2/galleries/{nhentai_id}/download?format=cbz"
        tmp_path = self.library_dir / f"_dl_{nhentai_id}.cbz"

        try:
            import httpx
            async with httpx.AsyncClient(follow_redirects=True, timeout=180,
                                          headers=headers) as client:
                async with client.stream("GET", cbz_url) as r:
                    if r.status_code in (401, 403):
                        raise RuntimeError(
                            "Download blocked — nhentai requires authentication. "
                            "Provide your API key or switch to the Scrape method."
                        )
                    r.raise_for_status()
                    total = int(r.headers.get("content-length", 0))
                    downloaded = 0
                    t0 = time.time()
                    with open(tmp_path, "wb") as f:
                        async for chunk in r.aiter_bytes(65536):
                            f.write(chunk)
                            downloaded += len(chunk)
                            if total and job:
                                job.done_pages = int(downloaded / total * job.total_pages) if job.total_pages else 0
                            elapsed = time.time() - t0
                            if elapsed > 0:
                                job.speed_kbps = (downloaded / 1024) / elapsed
        except ImportError:
            import requests
            r = requests.get(cbz_url, headers=headers, timeout=180, stream=True)
            if r.status_code in (401, 403):
                raise RuntimeError(
                    "Download blocked — nhentai requires authentication. "
                    "Provide your API key or switch to the Scrape method."
                )
            r.raise_for_status()
            with open(tmp_path, "wb") as f:
                for chunk in r.iter_content(65536):
                    f.write(chunk)

        if not tmp_path.exists() or tmp_path.stat().st_size < 1000:
            if tmp_path.exists():
                tmp_path.unlink()
            raise RuntimeError("CBZ download failed or file too small — try the Scrape method instead")

        job.status = Status.PROCESSING

        def _import():
            return register_cbz_download(
                tmp_path, self.library_dir,
                source="nhentai",
                source_id=nhentai_id,
                source_url=f"https://nhentai.net/g/{nhentai_id}/",
                meta_override={
                    "title":       meta.get("title", ""),
                    "title_alt":   meta.get("title_jp", ""),
                    "tags":        meta.get("tags", []),
                    "artists":     meta.get("artists", []),
                    "parodies":    meta.get("parodies", []),
                    "characters":  meta.get("characters", []),
                    "groups":      meta.get("groups", []),
                    "categories":  meta.get("categories", []),
                    "language":    meta.get("language", ""),
                    "source_url":  f"https://nhentai.net/g/{nhentai_id}/",
                    "upload_date": meta.get("upload_date", ""),
                }
            )

        record = await asyncio.to_thread(_import)
        job.comic_id    = record["comic_id"]
        job.cover_url   = record.get("cover_path", "")
        job.total_pages = record.get("page_count", job.total_pages)
        job.done_pages  = job.total_pages

        if tmp_path.exists():
            tmp_path.unlink()

        job.status = Status.COMPLETE

    # ------------------------------------------------------------------
    # WeebCentral: download chapter images → build CBZ
    # ------------------------------------------------------------------

    async def _process_weebcentral(self, job: DownloadJob):
        from .weebcentral import WeebCentralScraper
        from config import load_settings
        wc  = WeebCentralScraper()
        cfg = load_settings()
        delay_s = cfg.get("request_delay_ms", 500) / 1000.0

        job.status = Status.SCRAPING

        series_meta: dict = {}
        parsed = wc.parse_url(job.url)
        if parsed and parsed["type"] == "series":
            series_meta = await asyncio.to_thread(wc.fetch_series, parsed["id"])
            job.title     = series_meta.get("title", "Unknown")
            job.cover_url = series_meta.get("cover_url", "")
            if not job.chapters:
                job.chapters = series_meta.get("chapters", [])

        if not job.chapters:
            raise RuntimeError("No chapters found to download")

        total_chapters = len(job.chapters)
        job.total_pages   = total_chapters
        job.done_pages    = 0
        job.current_chapter = f"0/{total_chapters} chapters"
        job.status = Status.DOWNLOADING

        tmp_dir = self.library_dir / f"_wc_{job.job_id}"
        tmp_dir.mkdir(exist_ok=True)

        ch_meta_map: dict[str, dict] = {}

        for i, ch in enumerate(job.chapters, 1):
            if job.cancelled:
                job.status = Status.CANCELLED
                shutil.rmtree(tmp_dir, ignore_errors=True)
                return

            ch_num  = str(ch.get("number", i)).strip() or str(i)
            ch_name = ch.get("title", f"Chapter {ch_num}").strip() or f"Chapter {ch_num}"
            job.current_chapter = f"Chapter {ch_num} ({i}/{total_chapters})"

            ch_dir = tmp_dir / f"ch_{ch_num.zfill(6)}"
            ch_dir.mkdir(exist_ok=True)
            ch_meta_map[ch_dir.name] = {"number": ch_num, "title": ch_name, "upload_date": ch.get("upload_date", "")}

            try:
                img_urls = []
                for attempt in range(3):
                    img_urls = await asyncio.to_thread(wc.fetch_chapter_images, ch["url"])
                    if img_urls:
                        break
                    await asyncio.sleep(delay_s * 2)

                # Parallel image download for each chapter
                ch_sem = asyncio.Semaphore(min(self.max_concurrent_images, 4))

                async def dl_img(j_idx: int, img_url: str):
                    async with ch_sem:
                        ext = img_url.split("?")[0].rsplit(".", 1)[-1].lower() or "jpg"
                        if ext not in ("jpg", "jpeg", "png", "webp", "gif"):
                            ext = "jpg"
                        dest = ch_dir / f"{j_idx:04d}.{ext}"
                        for attempt in range(4):
                            try:
                                await self._download_binary_simple(
                                    img_url, dest,
                                    referer="https://weebcentral.com/"
                                )
                                if dest.exists() and dest.stat().st_size > 500:
                                    return
                                if dest.exists():
                                    dest.unlink()
                                await asyncio.sleep(delay_s * (attempt + 1))
                            except Exception:
                                await asyncio.sleep(delay_s * (attempt + 1))

                await asyncio.gather(*[dl_img(j, u) for j, u in enumerate(img_urls, 1)])

            except Exception:
                job.failed_pages.append(i)

            await asyncio.sleep(delay_s)
            job.done_pages = i

        job.status = Status.PROCESSING

        series_cover_url = job.cover_url
        first_comic_id   = ""

        for ch_dir in sorted(tmp_dir.iterdir()):
            if not ch_dir.is_dir():
                continue
            img_files = [f for f in ch_dir.iterdir() if f.is_file()]
            if not img_files:
                continue

            ch_info   = ch_meta_map.get(ch_dir.name, {})
            ch_num    = ch_info.get("number", ch_dir.name)
            cbz_path  = tmp_dir / f"{ch_dir.name}.cbz"

            with zipfile.ZipFile(cbz_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for img in sorted(img_files, key=lambda p: natural_sort_key(p.name)):
                    zf.write(img, img.name)

            chapter_title = f"{job.title} — Chapter {ch_num}"
            ch_upload_date = ch_info.get("upload_date", "")
            meta_override = {
                "title":       chapter_title,
                "series":      job.title,
                "chapter":     ch_num,
                "title_alt":   series_meta.get("title_alt", ""),
                "authors":     series_meta.get("authors", []),
                "artists":     series_meta.get("artists", []),
                "tags":        series_meta.get("tags", []) + series_meta.get("genres", []),
                "language":    series_meta.get("language", "EN"),
                "source_url":  job.url,
                "upload_date": ch_upload_date,
                "series_cover_url": series_cover_url,
            }
            try:
                record = await asyncio.to_thread(
                    register_cbz_download, cbz_path, self.library_dir,
                    "weebcentral", job.job_id, job.url, meta_override
                )
                if not first_comic_id:
                    first_comic_id = record["comic_id"]
            except Exception:
                pass

        shutil.rmtree(tmp_dir, ignore_errors=True)
        job.comic_id = first_comic_id
        job.status   = Status.COMPLETE

    # ------------------------------------------------------------------
    # Local CBZ file import
    # ------------------------------------------------------------------

    async def _process_local_cbz(self, job: DownloadJob):
        job.status = Status.PROCESSING
        path = Path(job.url)
        if not path.exists():
            raise FileNotFoundError(f"File not found: {path}")

        def _do_import():
            return import_cbz(path, self.library_dir)

        record = await asyncio.to_thread(_do_import)
        job.title       = record.get("title", path.stem)
        job.comic_id    = record["comic_id"]
        job.total_pages = record.get("page_count", 0)
        job.done_pages  = job.total_pages
        job.status = Status.COMPLETE

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    async def _download_binary_counted(self, url: str, dest: Path,
                                        referer: str = "") -> int:
        """Download and return bytes downloaded."""
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": referer or url,
        }
        try:
            import httpx
            async with httpx.AsyncClient(follow_redirects=True, timeout=60) as client:
                r = await client.get(url, headers=headers)
                r.raise_for_status()
                dest.write_bytes(r.content)
                return len(r.content)
        except ImportError:
            import requests
            r = requests.get(url, headers=headers, timeout=60)
            r.raise_for_status()
            dest.write_bytes(r.content)
            return len(r.content)

    async def _download_binary(self, url: str, dest: Path,
                                job: DownloadJob, referer: str = "") -> bool:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": referer or url,
        }
        try:
            import httpx
            async with httpx.AsyncClient(follow_redirects=True, timeout=120) as client:
                async with client.stream("GET", url, headers=headers) as r:
                    r.raise_for_status()
                    total = int(r.headers.get("content-length", 0))
                    downloaded = 0
                    t0 = time.time()
                    with open(dest, "wb") as f:
                        async for chunk in r.aiter_bytes(65536):
                            f.write(chunk)
                            downloaded += len(chunk)
                            if total and job:
                                job.done_pages = int(downloaded / total * job.total_pages) if job.total_pages else 0
                            elapsed = time.time() - t0
                            if elapsed > 0 and job:
                                job.speed_kbps = (downloaded / 1024) / elapsed
            return True
        except ImportError:
            import requests
            r = requests.get(url, headers=headers, timeout=120, stream=True)
            r.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in r.iter_content(65536):
                    f.write(chunk)
            return True

    async def _download_binary_simple(self, url: str, dest: Path, referer: str = ""):
        headers = {"User-Agent": "Mozilla/5.0", "Referer": referer}
        try:
            import httpx
            async with httpx.AsyncClient(follow_redirects=True, timeout=30) as c:
                r = await c.get(url, headers=headers)
                r.raise_for_status()
                dest.write_bytes(r.content)
        except ImportError:
            import requests
            r = requests.get(url, headers=headers, timeout=30)
            r.raise_for_status()
            dest.write_bytes(r.content)

    async def shutdown(self):
        for t in self._workers:
            t.cancel()


def natural_sort_key(s):
    import re
    return [int(c) if c.isdigit() else c.lower() for c in re.split(r"(\d+)", str(s))]
