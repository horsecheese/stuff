"""
config.py — Settings management for ComicVault
"""

import json
from pathlib import Path

CONFIG_PATH = Path(__file__).parent / "config.json"

DEFAULTS: dict = {
    # Storage
    "library_dir":         "library",
    "auto_open_browser":   True,
    # Downloads
    "max_concurrent":        2,
    "max_concurrent_images": 6,   # parallel image downloads per job
    "auto_cbz":              False,
    # Image processing
    "image_format":        "original",   # original | jpg | png | webp
    "jpg_quality":         90,
    "max_width":           0,            # 0 = no resize
    # Reader
    "reader_mode":         "single",     # single | double | scroll
    "reading_direction":   "ltr",        # ltr | rtl
    "preload_pages":       3,
    "remember_position":   True,
    "keyboard_nav":        True,
    "fullscreen_on_open":  False,
    "show_page_numbers":   True,
    # Network
    "proxy":               "",
    "user_agent":          "",
    "request_timeout":     30,
    "request_delay_ms":    500,
    "max_retries":         5,
    "use_flaresolverr":    False,
    # Library
    "default_sort":        "date_added",
    "default_view":        "grid",
    "grid_columns":        0,
    "auto_metadata":       True,
    "extract_cover":       True,
    "show_explicit_tags":  True,
    # Display
    "accent_color":        "#e63946",
}


def load_settings() -> dict:
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r") as f:
                stored = json.load(f)
            return {**DEFAULTS, **stored}
        except Exception:
            pass
    return dict(DEFAULTS)


def save_settings(data: dict):
    current = load_settings()
    current.update(data)
    with open(CONFIG_PATH, "w") as f:
        json.dump(current, f, indent=2)
