"""
Database — SQLite storage for parsed journal articles.
"""

import json
import sqlite3
from pathlib import Path
from typing import Optional

DB_PATH = Path(__file__).parent / "articles.db"


# ── Schema ────────────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS articles (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    title            TEXT NOT NULL,
    publication_date TEXT,
    journal_name     TEXT,
    volume           TEXT,
    issue            TEXT,
    type             TEXT,
    tags             TEXT,   -- JSON array
    summary          TEXT,
    body             TEXT,
    "references"       TEXT,   -- JSON array
    source_file      TEXT,
    paywalled        INTEGER DEFAULT 0,
    article_url      TEXT,
    created_at       TEXT DEFAULT (datetime('now'))
);

CREATE VIRTUAL TABLE IF NOT EXISTS articles_fts USING fts5(
    title,
    summary,
    body,
    content='articles',
    content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS articles_ai AFTER INSERT ON articles BEGIN
    INSERT INTO articles_fts(rowid, title, summary, body)
    VALUES (new.id, new.title, new.summary, new.body);
END;

CREATE TRIGGER IF NOT EXISTS articles_ad AFTER DELETE ON articles BEGIN
    INSERT INTO articles_fts(articles_fts, rowid, title, summary, body)
    VALUES ('delete', old.id, old.title, old.summary, old.body);
END;

CREATE TRIGGER IF NOT EXISTS articles_au AFTER UPDATE ON articles BEGIN
    INSERT INTO articles_fts(articles_fts, rowid, title, summary, body)
    VALUES ('delete', old.id, old.title, old.summary, old.body);
    INSERT INTO articles_fts(rowid, title, summary, body)
    VALUES (new.id, new.title, new.summary, new.body);
END;
"""


# ── Connection ────────────────────────────────────────────────────────────────

def get_conn(db_path: Path = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db(db_path: Path = DB_PATH):
    """Create tables if they don't exist."""
    with get_conn(db_path) as conn:
        conn.executescript(SCHEMA)
        # Migrate existing DBs that predate the paywalled/article_url columns
        cols = {r[1] for r in conn.execute("PRAGMA table_info(articles)").fetchall()}
        if "paywalled" not in cols:
            conn.execute("ALTER TABLE articles ADD COLUMN paywalled INTEGER DEFAULT 0")
        if "article_url" not in cols:
            conn.execute("ALTER TABLE articles ADD COLUMN article_url TEXT")
    print(f"[db] Initialised: {db_path}")


# ── Write ─────────────────────────────────────────────────────────────────────

def article_url_exists(article_url: str, db_path: Path = DB_PATH) -> bool:
    """Return True if an article with this URL is already in the database."""
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT 1 FROM articles WHERE article_url = ?", (article_url,)
        ).fetchone()
        return row is not None


def insert_article(article: dict, source_file: str = None, db_path: Path = DB_PATH) -> int:
    """
    Insert a parsed article dict. Returns the new row id.
    Skips insert if an article with the same title and journal already exists.
    """
    with get_conn(db_path) as conn:
        existing = conn.execute(
            "SELECT id FROM articles WHERE title = ? AND journal_name = ?",
            (article.get("title"), article.get("journal_name")),
        ).fetchone()

        if existing:
            print(f"[db] Skipping duplicate: {article.get('title', '')[:60]}")
            return existing["id"]

        cursor = conn.execute(
            """
            INSERT INTO articles
                (title, publication_date, journal_name, volume, issue, type,
                 tags, summary, body, "references", source_file, paywalled, article_url)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                article.get("title"),
                article.get("publication_date"),
                article.get("journal_name"),
                article.get("volume"),
                article.get("issue"),
                article.get("type"),
                json.dumps(article.get("tags") or []),
                article.get("summary"),
                article.get("body"),
                json.dumps(article.get("references") or []),
                source_file,
                1 if article.get("paywalled") else 0,
                article.get("article_url"),
            ),
        )
        return cursor.lastrowid


def insert_articles(articles: list[dict], source_file: str = None, db_path: Path = DB_PATH) -> list[int]:
    """Insert a list of articles. Returns list of row ids."""
    return [insert_article(a, source_file=source_file, db_path=db_path) for a in articles]


def get_paywalled_articles(db_path: Path = DB_PATH) -> list[dict]:
    """Return all articles that were recorded as paywalled and have a URL to retry."""
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM articles WHERE paywalled = 1 AND article_url IS NOT NULL ORDER BY created_at DESC"
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def update_article_content(article_id: int, data: dict, db_path: Path = DB_PATH):
    """Overwrite content fields on an existing article and clear the paywalled flag."""
    with get_conn(db_path) as conn:
        conn.execute(
            """
            UPDATE articles SET
                publication_date = ?, journal_name = ?, volume = ?, issue = ?, type = ?,
                tags = ?, summary = ?, body = ?, "references" = ?, paywalled = 0
            WHERE id = ?
            """,
            (
                data.get("publication_date"),
                data.get("journal_name"),
                data.get("volume"),
                data.get("issue"),
                data.get("type"),
                json.dumps(data.get("tags") or []),
                data.get("summary"),
                data.get("body"),
                json.dumps(data.get("references") or []),
                article_id,
            ),
        )


# ── Read ──────────────────────────────────────────────────────────────────────

def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["tags"] = json.loads(d["tags"] or "[]")
    d["references"] = json.loads(d["references"] or "[]")
    return d


def get_article(article_id: int, db_path: Path = DB_PATH) -> Optional[dict]:
    with get_conn(db_path) as conn:
        row = conn.execute("SELECT * FROM articles WHERE id = ?", (article_id,)).fetchone()
    return _row_to_dict(row) if row else None


def list_articles(
    journal: str = None,
    volume: str = None,
    issue: str = None,
    article_type: str = None,
    db_path: Path = DB_PATH,
) -> list[dict]:
    """List articles with optional filters."""
    query = "SELECT * FROM articles WHERE 1=1"
    params = []
    if journal:
        query += " AND journal_name LIKE ?"
        params.append(f"%{journal}%")
    if volume:
        query += " AND volume = ?"
        params.append(volume)
    if issue:
        query += " AND issue = ?"
        params.append(issue)
    if article_type:
        query += " AND type LIKE ?"
        params.append(f"%{article_type}%")
    query += " ORDER BY created_at DESC"

    with get_conn(db_path) as conn:
        rows = conn.execute(query, params).fetchall()
    return [_row_to_dict(r) for r in rows]


def search(query: str, db_path: Path = DB_PATH) -> list[dict]:
    """Full-text search across title, summary, and body."""
    with get_conn(db_path) as conn:
        rows = conn.execute(
            """
            SELECT a.* FROM articles a
            JOIN articles_fts fts ON a.id = fts.rowid
            WHERE articles_fts MATCH ?
            ORDER BY rank
            """,
            (query,),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Article database utility")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("init", help="Initialise the database")

    ip = sub.add_parser("import", help="Import a JSON file of articles")
    ip.add_argument("path", help="Path to .json file (single article or array)")

    lp = sub.add_parser("list", help="List articles")
    lp.add_argument("--journal", default=None)
    lp.add_argument("--volume", default=None)
    lp.add_argument("--issue", default=None)
    lp.add_argument("--type", default=None, dest="article_type")

    sp = sub.add_parser("search", help="Full-text search")
    sp.add_argument("query")

    args = parser.parse_args()

    if args.cmd == "init":
        init_db()

    elif args.cmd == "import":
        init_db()
        data = json.loads(Path(args.path).read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data = [data]
        ids = insert_articles(data, source_file=args.path)
        print(f"[db] Imported {len(ids)} article(s)")

    elif args.cmd == "list":
        init_db()
        articles = list_articles(
            journal=args.journal, volume=args.volume,
            issue=args.issue, article_type=args.article_type,
        )
        for a in articles:
            print(f"[{a['id']}] {a['title'][:70]}  |  {a['journal_name']}  v{a['volume']} i{a['issue']}")

    elif args.cmd == "search":
        init_db()
        results = search(args.query)
        for a in results:
            print(f"[{a['id']}] {a['title'][:70]}")
            print(f"       {a['summary'][:120]}\n")

    else:
        parser.print_help()
