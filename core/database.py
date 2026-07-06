"""
database.py — SQLite + SQLAlchemy models for ComicVault
"""

import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from sqlalchemy import (
    Boolean, Column, Integer, String, Text, DateTime, Float,
    create_engine, func, desc, asc, text
)
from sqlalchemy.orm import declarative_base, sessionmaker

BASE_DIR = Path(__file__).parent
DB_PATH  = BASE_DIR / "comicvault.db"
engine   = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


class Comic(Base):
    __tablename__ = "comics"

    # identity
    comic_id     = Column(String, primary_key=True)   # uuid or nhentai id
    source       = Column(String, default="local")    # local | nhentai | weebcentral
    source_id    = Column(String, default="")         # original site ID
    source_url   = Column(String, default="")

    # metadata
    title        = Column(String, nullable=False, default="")
    title_alt    = Column(String, default="")         # JP title etc.
    series       = Column(String, default="")
    volume       = Column(String, default="")
    chapter      = Column(String, default="")
    authors      = Column(Text, default="[]")         # JSON list
    artists      = Column(Text, default="[]")
    genres       = Column(Text, default="[]")
    tags         = Column(Text, default="[]")
    characters   = Column(Text, default="[]")
    parodies     = Column(Text, default="[]")
    groups       = Column(Text, default="[]")
    publisher    = Column(String, default="")
    language     = Column(String, default="")
    year         = Column(String, default="")
    description  = Column(Text, default="")
    rating       = Column(Float, default=0.0)         # user rating 0-5
    age_rating   = Column(String, default="")         # All Ages / Teen / Mature / Adults Only

    # file info
    cover_path   = Column(String, default="")        # relative path served via /library/
    cbz_path     = Column(String, default="")        # path to .cbz if stored as CBZ
    pages_dir    = Column(String, default="")        # path to extracted pages dir
    page_count   = Column(Integer, default=0)
    file_size_mb = Column(Float, default=0.0)
    storage_type = Column(String, default="pages")   # pages | cbz

    # upload / publish date (scraped from source)
    upload_date  = Column(String, default="")         # unix timestamp string or ISO date

    # state
    status       = Column(String, default="complete") # complete | downloading | error
    date_added   = Column(DateTime, default=datetime.utcnow)
    last_read    = Column(DateTime, nullable=True)
    read_page    = Column(Integer, default=0)
    favorite     = Column(Boolean, default=False)
    notes        = Column(Text, default="")
    collections  = Column(Text, default="[]")        # JSON list of collection names


class Collection(Base):
    __tablename__ = "collections"
    name        = Column(String, primary_key=True)
    description = Column(Text, default="")
    created_at  = Column(DateTime, default=datetime.utcnow)
    cover_id    = Column(String, default="")   # comic_id to use as cover


class ReadingHistory(Base):
    __tablename__ = "reading_history"
    id         = Column(Integer, primary_key=True, autoincrement=True)
    comic_id   = Column(String, nullable=False)
    page       = Column(Integer, default=0)
    timestamp  = Column(DateTime, default=datetime.utcnow)


def init_db():
    Base.metadata.create_all(engine)
    # ── Safe migrations: add columns that may not exist in older DBs ──────────
    _safe_migrations = [
        "ALTER TABLE comics ADD COLUMN series TEXT DEFAULT ''",
        "ALTER TABLE comics ADD COLUMN upload_date TEXT DEFAULT ''",
    ]
    with engine.connect() as conn:
        for stmt in _safe_migrations:
            try:
                conn.execute(text(stmt))
                conn.commit()
            except Exception:
                pass  # column already exists


# ---------------------------------------------------------------------------

class ComicDB:
    def __init__(self):
        self.session = SessionLocal()

    def _j(self, v):
        return json.dumps(v, ensure_ascii=False) if isinstance(v, list) else v

    def _to_dict(self, c: Comic) -> dict:
        return {
            "comic_id":    c.comic_id,
            "source":      c.source,
            "source_id":   c.source_id,
            "source_url":  c.source_url,
            "title":       c.title,
            "title_alt":   c.title_alt,
            "series":      c.series,
            "volume":      c.volume,
            "chapter":     c.chapter,
            "authors":     json.loads(c.authors  or "[]"),
            "artists":     json.loads(c.artists  or "[]"),
            "genres":      json.loads(c.genres   or "[]"),
            "tags":        json.loads(c.tags     or "[]"),
            "characters":  json.loads(c.characters or "[]"),
            "parodies":    json.loads(c.parodies or "[]"),
            "groups":      json.loads(c.groups   or "[]"),
            "publisher":   c.publisher,
            "language":    c.language,
            "year":        c.year,
            "description": c.description,
            "rating":      c.rating,
            "age_rating":  c.age_rating,
            "cover_path":  c.cover_path,
            "cbz_path":    c.cbz_path,
            "pages_dir":   c.pages_dir,
            "page_count":  c.page_count,
            "file_size_mb":c.file_size_mb,
            "storage_type":c.storage_type,
            "upload_date": getattr(c, 'upload_date', '') or "",
            "status":      c.status,
            "date_added":  c.date_added.isoformat() if c.date_added else "",
            "last_read":   c.last_read.isoformat() if c.last_read else None,
            "read_page":   c.read_page,
            "favorite":    c.favorite,
            "notes":       c.notes,
            "collections": json.loads(c.collections or "[]"),
        }

    def get(self, comic_id: str) -> Optional[dict]:
        c = self.session.query(Comic).filter_by(comic_id=comic_id).first()
        return self._to_dict(c) if c else None

    def get_by_source_id(self, source: str, source_id: str) -> Optional[dict]:
        c = self.session.query(Comic).filter_by(source=source, source_id=source_id).first()
        return self._to_dict(c) if c else None

    def get_all(self, sort="date_added", order="desc", search="",
                tag="", language="", source="", favorites=False,
                unread=False, collection="",
                author="", artist="", character="", parody="",
                tags_any="") -> list:
        q = self.session.query(Comic).filter(Comic.status != "error")
        if search:
            q = q.filter(
                Comic.title.ilike(f"%{search}%") |
                Comic.title_alt.ilike(f"%{search}%") |
                Comic.series.ilike(f"%{search}%") |
                Comic.tags.ilike(f"%{search}%") |
                Comic.artists.ilike(f"%{search}%") |
                Comic.authors.ilike(f"%{search}%") |
                Comic.genres.ilike(f"%{search}%")
            )
        if tag:
            q = q.filter(Comic.tags.ilike(f"%{tag}%") | Comic.genres.ilike(f"%{tag}%"))
        if author:
            q = q.filter(Comic.authors.ilike(f"%{author}%"))
        if artist:
            q = q.filter(Comic.artists.ilike(f"%{artist}%"))
        if character:
            q = q.filter(Comic.characters.ilike(f"%{character}%"))
        if parody:
            q = q.filter(Comic.parodies.ilike(f"%{parody}%"))
        if tags_any:
            # Comma-separated list — match ANY of the tags
            terms = [t.strip() for t in tags_any.split(",") if t.strip()]
            if terms:
                from sqlalchemy import or_
                conds = [Comic.tags.ilike(f"%{t}%") | Comic.genres.ilike(f"%{t}%") for t in terms]
                q = q.filter(or_(*conds))
        if language:
            q = q.filter(Comic.language == language)
        if source:
            q = q.filter(Comic.source == source)
        if favorites:
            q = q.filter(Comic.favorite == True)
        if unread:
            q = q.filter(Comic.read_page == 0)
        if collection:
            q = q.filter(Comic.collections.ilike(f"%{collection}%"))

        col_map = {
            "date_added": Comic.date_added,
            "title":      Comic.title,
            "page_count": Comic.page_count,
            "last_read":  Comic.last_read,
            "rating":     Comic.rating,
            "year":       Comic.year,
        }
        col = col_map.get(sort, Comic.date_added)
        q = q.order_by(desc(col) if order == "desc" else asc(col))
        return [self._to_dict(c) for c in q.all()]

    def upsert(self, data: dict) -> str:
        if "comic_id" not in data or not data["comic_id"]:
            data["comic_id"] = str(uuid.uuid4())
        existing = self.session.query(Comic).filter_by(comic_id=data["comic_id"]).first()
        list_fields = {"authors","artists","genres","tags","characters","parodies","groups","collections"}
        if existing:
            for k, v in data.items():
                if hasattr(existing, k):
                    setattr(existing, k, json.dumps(v, ensure_ascii=False) if k in list_fields else v)
        else:
            obj = {k: (json.dumps(v, ensure_ascii=False) if k in list_fields else v)
                   for k, v in data.items() if hasattr(Comic, k)}
            self.session.add(Comic(**obj))
        self.session.commit()
        return data["comic_id"]

    def toggle_favorite(self, comic_id: str) -> bool:
        c = self.session.query(Comic).filter_by(comic_id=comic_id).first()
        if c:
            c.favorite = not c.favorite
            self.session.commit()
            return c.favorite
        return False

    def set_rating(self, comic_id: str, rating: float):
        c = self.session.query(Comic).filter_by(comic_id=comic_id).first()
        if c:
            c.rating = max(0.0, min(5.0, rating))
            self.session.commit()

    def update_progress(self, comic_id: str, page: int):
        c = self.session.query(Comic).filter_by(comic_id=comic_id).first()
        if c:
            c.read_page = page
            c.last_read = datetime.utcnow()
            # Log to history only when comic exists
            self.session.add(ReadingHistory(comic_id=comic_id, page=page))
            self.session.commit()

    def update_notes(self, comic_id: str, notes: str):
        c = self.session.query(Comic).filter_by(comic_id=comic_id).first()
        if c:
            c.notes = notes
            self.session.commit()

    def add_to_collection(self, comic_id: str, collection_name: str):
        c = self.session.query(Comic).filter_by(comic_id=comic_id).first()
        if c:
            cols = json.loads(c.collections or "[]")
            if collection_name not in cols:
                cols.append(collection_name)
                c.collections = json.dumps(cols)
                self.session.commit()

    def remove_from_collection(self, comic_id: str, collection_name: str):
        c = self.session.query(Comic).filter_by(comic_id=comic_id).first()
        if c:
            cols = [x for x in json.loads(c.collections or "[]") if x != collection_name]
            c.collections = json.dumps(cols)
            self.session.commit()

    def delete(self, comic_id: str):
        self.session.query(Comic).filter_by(comic_id=comic_id).delete()
        self.session.query(ReadingHistory).filter_by(comic_id=comic_id).delete()
        self.session.commit()

    # Collections
    def get_collections(self) -> list:
        rows = self.session.query(Collection).order_by(Collection.name).all()
        return [{"name": r.name, "description": r.description,
                 "created_at": r.created_at.isoformat(), "cover_id": r.cover_id} for r in rows]

    def upsert_collection(self, name: str, description: str = "", cover_id: str = ""):
        existing = self.session.query(Collection).filter_by(name=name).first()
        if existing:
            existing.description = description
            if cover_id: existing.cover_id = cover_id
        else:
            self.session.add(Collection(name=name, description=description, cover_id=cover_id))
        self.session.commit()

    def delete_collection(self, name: str):
        self.session.query(Collection).filter_by(name=name).delete()
        # Remove from all comics
        for c in self.session.query(Comic).filter(Comic.collections.ilike(f"%{name}%")).all():
            cols = [x for x in json.loads(c.collections or "[]") if x != name]
            c.collections = json.dumps(cols)
        self.session.commit()

    # Stats
    def get_tag_counts(self, field="tags") -> list:
        col_map = {"tags": Comic.tags, "genres": Comic.genres,
                   "authors": Comic.authors, "artists": Comic.artists}
        col = col_map.get(field, Comic.tags)
        counts = {}
        for (val,) in self.session.query(col).all():
            for item in json.loads(val or "[]"):
                counts[item] = counts.get(item, 0) + 1
        return sorted([{"tag": k, "count": v} for k, v in counts.items()],
                      key=lambda x: x["count"], reverse=True)

    def get_stats(self) -> dict:
        total      = self.session.query(func.count(Comic.comic_id)).scalar() or 0
        total_p    = self.session.query(func.sum(Comic.page_count)).scalar() or 0
        total_mb   = self.session.query(func.sum(Comic.file_size_mb)).scalar() or 0
        favorites  = self.session.query(func.count(Comic.comic_id)).filter(Comic.favorite == True).scalar() or 0
        unread     = self.session.query(func.count(Comic.comic_id)).filter(Comic.read_page == 0).scalar() or 0
        sources    = {}
        for (s,) in self.session.query(Comic.source).all():
            sources[s] = sources.get(s, 0) + 1
        langs = {}
        for (l,) in self.session.query(Comic.language).all():
            if l: langs[l] = langs.get(l, 0) + 1
        return {
            "total_comics": total, "total_pages": total_p,
            "total_size_mb": round(total_mb, 1),
            "favorites": favorites, "unread": unread,
            "sources": sources, "languages": langs,
        }

    def get_in_progress(self, limit=8) -> list:
        q = (self.session.query(Comic)
             .filter(Comic.read_page > 0)
             .filter(Comic.read_page < Comic.page_count - 1)
             .order_by(desc(Comic.last_read))
             .limit(limit))
        return [self._to_dict(c) for c in q.all()]

    def get_recently_added(self, limit=12) -> list:
        q = self.session.query(Comic).order_by(desc(Comic.date_added)).limit(limit)
        return [self._to_dict(c) for c in q.all()]

    def get_reading_history(self, limit: int = 100) -> list:
        """Return recent reading history entries with comic details."""
        from sqlalchemy import desc as _desc
        rows = (self.session.query(ReadingHistory)
                .order_by(_desc(ReadingHistory.timestamp))
                .limit(limit)
                .all())
        result = []
        for row in rows:
            comic = self.get(row.comic_id)
            if not comic:
                continue
            result.append({
                "comic_id":  row.comic_id,
                "page":      row.page,
                "timestamp": row.timestamp.isoformat() if row.timestamp else "",
                "title":     comic.get("title", ""),
                "cover_path": comic.get("cover_path", ""),
                "page_count": comic.get("page_count", 0),
                "source":    comic.get("source", ""),
            })
        return result
