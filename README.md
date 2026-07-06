# ComicVault v3.0.0

A local manga/comic archiver and reader. All data stays on your machine — no accounts, no tracking, no cloud.

## Quick Start

```bash
pip install -r requirements.txt
python main.py
# Opens at http://localhost:7771
```

## What's New in v3.0

### Bug Fixes
- **WeebCentral series**: Chapters from the same series now appear as a single card in the library. Click the card to open a chapter picker.
- **Collections**: Browse, Edit, Delete, and Add Manga buttons now work reliably via event delegation. No more needing to click away and back.
- **Color flash**: Accent color is now cached in `localStorage` and applied synchronously in `<head>` before the page renders — no more flash to default red.
- **Full color theming**: All pages (including All Comics / Library) now fully respect the accent color via CSS `color-mix()`. No more hardcoded red borders or highlights.
- **nhentai Favorites**: Simplified to scraper-only. Only your `sessionid` cookie is needed. Clear step-by-step guide included in the importer.
- **Styled dialogs**: All `confirm()` / `alert()` calls replaced with a styled in-page modal (`CV.confirm()`).
- **Settings Preview button**: Removed — color picks apply instantly as you select them.

### Code Structure
```
comicvault/
├── main.py              # App entry + route registration
├── config.py            # Settings management
├── requirements.txt
│
├── core/                # Backend modules
│   ├── database.py
│   ├── downloader.py
│   ├── nhentai.py       # nhentai scraper (was scraper.py)
│   ├── weebcentral.py
│   └── cbz.py           # CBZ importer (was cbz_importer.py)
│
├── routes/              # Modular FastAPI route handlers
│   ├── pages.py         # HTML page routes
│   ├── library.py       # Library CRUD API
│   ├── collections.py   # Collections API
│   ├── downloads.py     # Downloads API + WebSocket
│   ├── importer.py      # Import API
│   └── settings.py      # Settings API
│
├── static/
│   └── no-cover.svg
│
└── templates/
    ├── _nav.html          # Shared sidebar nav
    ├── _shared_css.html   # Shared base CSS (used via Jinja include)
    ├── _theme_init.html   # Inline script → prevents accent color flash
    ├── _confirm.html      # Styled confirm/alert modal component
    ├── index.html
    ├── importer.html
    ├── collections.html
    ├── settings.html
    └── ...
```

## Tech Stack
- **Backend**: FastAPI + SQLite  
- **Frontend**: Vanilla JS + Jinja2 templates  
- **Port**: 7771
