"""SQLite storage: items, page texts, notes and annotations, collections, and
the FTS5 index used to preselect pages."""
import json
import shutil
import sqlite3
import logging
from pathlib import Path
from datetime import datetime, timezone

from zotegrep import config

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
# Version of the indexing format (items.indexed). Raising it makes the next
# sync re-index every PDF from disk, without downloading anything.
INDEX_VERSION = 1

FTS_ENABLED = False

METADATA_COLUMNS = (
    "title", "authors", "authors_full", "date", "abstract", "tags",
    "collections", "item_type", "publication", "doi", "url",
)


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(config.paths.db_file), check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    global FTS_ENABLED
    config.paths.data_dir.mkdir(parents=True, exist_ok=True)
    config.paths.pdf_dir.mkdir(parents=True, exist_ok=True)
    conn = get_db()
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS items (
                zotero_key    TEXT PRIMARY KEY,
                library_id    INTEGER NOT NULL,
                parent_key    TEXT,
                filename      TEXT,
                version       INTEGER,
                indexed       INTEGER DEFAULT 0,
                no_text       INTEGER DEFAULT 0,
                title         TEXT,
                authors       TEXT DEFAULT '',
                authors_full  TEXT DEFAULT '',
                date          TEXT DEFAULT '',
                abstract      TEXT DEFAULT '',
                tags          TEXT DEFAULT '[]',
                collections   TEXT DEFAULT '[]',
                item_type     TEXT DEFAULT '',
                publication   TEXT DEFAULT '',
                doi           TEXT DEFAULT '',
                url           TEXT DEFAULT ''
            );
            -- `folded`: copy without accents or typographic punctuation (see
            -- text.fold), same length as `text` so offsets match, NULL when it
            -- would be identical. It is what gets searched and indexed.
            CREATE TABLE IF NOT EXISTS pages (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                zotero_key  TEXT NOT NULL,
                page_num    INTEGER NOT NULL,
                text        TEXT,
                folded      TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_pages_key_page ON pages(zotero_key, page_num);
            CREATE TABLE IF NOT EXISTS collections (
                library_id      INTEGER NOT NULL,
                collection_key  TEXT NOT NULL,
                name            TEXT NOT NULL,
                parent_key      TEXT,
                PRIMARY KEY (library_id, collection_key)
            );
            -- Zotero notes and annotations, attached to the PDF they belong to.
            CREATE TABLE IF NOT EXISTS extras (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                library_id  INTEGER NOT NULL,
                zotero_key  TEXT NOT NULL,
                kind        TEXT NOT NULL,
                page_num    INTEGER NOT NULL DEFAULT 0,
                text        TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_extras_key ON extras(zotero_key);
            CREATE TABLE IF NOT EXISTS meta (
                key   TEXT PRIMARY KEY,
                value TEXT
            );
        """)
        conn.execute("INSERT OR IGNORE INTO meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
        conn.commit()
        FTS_ENABLED = _init_fts(conn)
    finally:
        conn.close()


def _init_fts(conn: sqlite3.Connection) -> bool:
    """Full-text index (trigrams, so substrings work) used to preselect the
    pages the patterns then run on."""
    try:
        exists = conn.execute("SELECT 1 FROM sqlite_master WHERE name='pages_fts'").fetchone()
        conn.executescript("""
            CREATE VIEW IF NOT EXISTS pages_body AS
                SELECT id, COALESCE(folded, text) AS body FROM pages;
            CREATE VIRTUAL TABLE IF NOT EXISTS pages_fts USING fts5(
                body, content='pages_body', content_rowid='id', tokenize='trigram'
            );
            CREATE TRIGGER IF NOT EXISTS pages_fts_ai AFTER INSERT ON pages BEGIN
                INSERT INTO pages_fts(rowid, body) VALUES (new.id, COALESCE(new.folded, new.text));
            END;
            CREATE TRIGGER IF NOT EXISTS pages_fts_ad AFTER DELETE ON pages BEGIN
                INSERT INTO pages_fts(pages_fts, rowid, body)
                VALUES ('delete', old.id, COALESCE(old.folded, old.text));
            END;
        """)
        if not exists:
            conn.execute("INSERT INTO pages_fts(pages_fts) VALUES ('rebuild')")
        conn.commit()
        return True
    except sqlite3.OperationalError as e:
        conn.rollback()
        logger.warning("FTS5 trigram unavailable, falling back to full scans: %s", e)
        return False


# --------------------------------------------------------------------------
# Files and metadata
# --------------------------------------------------------------------------

def library_dir(library_id: int) -> Path:
    return config.paths.pdf_dir / str(library_id)


def pdf_path(library_id: int, zotero_key: str) -> Path:
    return library_dir(library_id) / f"{zotero_key}.pdf"


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str):
    conn.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, value))
    conn.commit()


def stamp_sync():
    conn = get_db()
    try:
        set_meta(conn, "last_sync", datetime.now(timezone.utc).isoformat())
    finally:
        conn.close()


def delete_item(conn: sqlite3.Connection, library_id: int, zotero_key: str):
    pdf_path(library_id, zotero_key).unlink(missing_ok=True)
    conn.execute("DELETE FROM pages WHERE zotero_key=?", (zotero_key,))
    conn.execute("DELETE FROM extras WHERE zotero_key=?", (zotero_key,))
    conn.execute("DELETE FROM items WHERE zotero_key=?", (zotero_key,))


def delete_library_data(library_id: int):
    """Drop the index and the PDFs of a library removed from the configuration."""
    conn = get_db()
    try:
        for row in conn.execute("SELECT zotero_key FROM items WHERE library_id=?", (library_id,)).fetchall():
            delete_item(conn, library_id, row["zotero_key"])
        conn.execute("DELETE FROM collections WHERE library_id=?", (library_id,))
        conn.execute("DELETE FROM extras WHERE library_id=?", (library_id,))
        conn.execute("DELETE FROM meta WHERE key LIKE ? ESCAPE '\\'", (f"%\\_L{library_id}",))
        conn.commit()
    finally:
        conn.close()
    shutil.rmtree(library_dir(library_id), ignore_errors=True)


def get_stats() -> dict:
    conn = get_db()
    try:
        return {
            "pdf_count": conn.execute("SELECT COUNT(*) FROM items").fetchone()[0],
            "page_count": conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0],
            "no_text_count": conn.execute("SELECT COUNT(*) FROM items WHERE no_text=1").fetchone()[0],
            "last_sync": get_meta(conn, "last_sync"),
        }
    finally:
        conn.close()


def get_no_text_items() -> list[dict]:
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT zotero_key, title, authors, date, filename FROM items WHERE no_text=1 ORDER BY title"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_item(zotero_key: str) -> dict | None:
    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM items WHERE zotero_key=?", (zotero_key,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_pdf_path(zotero_key: str) -> Path | None:
    item = get_item(zotero_key)
    if not item:
        return None
    path = pdf_path(item["library_id"], zotero_key)
    return path if path.exists() else None


def get_facets() -> dict:
    """Values offered by the filters: collections (tree), tags, authors, years."""
    conn = get_db()
    try:
        items = conn.execute("SELECT library_id, date, tags, collections, authors_full FROM items").fetchall()
        collections = [dict(r) for r in conn.execute(
            "SELECT library_id, collection_key AS key, name, parent_key FROM collections"
        ).fetchall()]
        tag_colors: dict[str, str] = {}
        for lib_id in sorted({it["library_id"] for it in items}):
            for name, color in json.loads(get_meta(conn, f"tag_colors_L{lib_id}") or "{}").items():
                tag_colors.setdefault(name, color)
    finally:
        conn.close()

    tag_counts: dict[str, int] = {}
    author_counts: dict[str, int] = {}
    direct_counts: dict[tuple, int] = {}
    years = []
    for it in items:
        for tag in json.loads(it["tags"] or "[]"):
            tag_counts[tag] = tag_counts.get(tag, 0) + 1
        for name in (it["authors_full"] or "").split(";"):
            name = name.strip()
            if name:
                author_counts[name] = author_counts.get(name, 0) + 1
        for ck in json.loads(it["collections"] or "[]"):
            direct_counts[(it["library_id"], ck)] = direct_counts.get((it["library_id"], ck), 0) + 1
        if (it["date"] or "")[:4].isdigit():
            years.append(int(it["date"][:4]))

    for c in collections:
        c["count"] = direct_counts.get((c["library_id"], c["key"]), 0)

    def top(counts: dict[str, int], n: int) -> list[dict]:
        ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0].lower()))[:n]
        return [{"name": k, "count": v} for k, v in ranked]

    return {
        "collections": sorted(collections, key=lambda c: c["name"].lower()),
        "tags": [{**t, "color": tag_colors.get(t["name"])} for t in top(tag_counts, 500)],
        "authors": top(author_counts, 500),
        "year_min": min(years) if years else None,
        "year_max": max(years) if years else None,
    }
