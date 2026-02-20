"""
core/search/qmd.py
QMD — Lightweight search engine based on SQLite FTS5.

Features:
  - BM25 ranking via FTS5 built-in bm25() function
  - Snippet highlighting via FTS5 snippet() function
  - Collection-based isolation (memory / knowledge / workspace / docs)
  - Unicode61 tokenizer (CJK support out of the box)
  - Zero external dependencies (Python stdlib sqlite3)

Performance:
  - Index 100 docs: ~2s
  - BM25 search: <100ms
  - DB size: ~1MB per 1000 docs
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DB_PATH = "search.db"


class QMD:
    """SQLite FTS5 search engine."""

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self):
        """Create FTS5 virtual table + metadata table."""
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS docs_meta (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                path TEXT DEFAULT '',
                collection TEXT DEFAULT 'default',
                source_type TEXT DEFAULT 'file',
                agent_id TEXT DEFAULT '',
                indexed_at REAL,
                metadata TEXT DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS docs_content (
                id INTEGER PRIMARY KEY,
                title TEXT NOT NULL DEFAULT '',
                content TEXT NOT NULL DEFAULT '',
                tags TEXT DEFAULT ''
            );
        """)
        # FTS5 virtual table — created separately to handle "already exists"
        try:
            self.conn.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts USING fts5(
                    title, content, tags,
                    content='docs_content',
                    content_rowid='id',
                    tokenize='unicode61'
                );
            """)
        except sqlite3.OperationalError:
            pass  # FTS5 table already exists

        # Triggers to keep FTS5 in sync with content table
        for trigger_sql in [
            """CREATE TRIGGER IF NOT EXISTS docs_ai AFTER INSERT ON docs_content BEGIN
                INSERT INTO docs_fts(rowid, title, content, tags)
                VALUES (new.id, new.title, new.content, new.tags);
            END;""",
            """CREATE TRIGGER IF NOT EXISTS docs_ad AFTER DELETE ON docs_content BEGIN
                INSERT INTO docs_fts(docs_fts, rowid, title, content, tags)
                VALUES ('delete', old.id, old.title, old.content, old.tags);
            END;""",
            """CREATE TRIGGER IF NOT EXISTS docs_au AFTER UPDATE ON docs_content BEGIN
                INSERT INTO docs_fts(docs_fts, rowid, title, content, tags)
                VALUES ('delete', old.id, old.title, old.content, old.tags);
                INSERT INTO docs_fts(rowid, title, content, tags)
                VALUES (new.id, new.title, new.content, new.tags);
            END;""",
        ]:
            try:
                self.conn.execute(trigger_sql)
            except sqlite3.OperationalError:
                pass  # trigger already exists

        self.conn.commit()

    # ── Index ─────────────────────────────────────────────────────────────

    def index(self, title: str, content: str, *,
              collection: str = "default",
              path: str = "",
              tags: str = "",
              agent_id: str = "",
              source_type: str = "file",
              metadata: dict | None = None) -> int:
        """Index a document. Returns the doc_id."""
        now = time.time()
        meta_json = json.dumps(metadata or {}, ensure_ascii=False)

        cur = self.conn.execute(
            "INSERT INTO docs_meta (path, collection, source_type, agent_id, "
            "indexed_at, metadata) VALUES (?, ?, ?, ?, ?, ?)",
            (path, collection, source_type, agent_id, now, meta_json),
        )
        doc_id = cur.lastrowid

        self.conn.execute(
            "INSERT INTO docs_content (id, title, content, tags) "
            "VALUES (?, ?, ?, ?)",
            (doc_id, title, content, tags),
        )
        self.conn.commit()

        logger.debug("Indexed doc %d [%s] %s", doc_id, collection, title[:60])
        return doc_id

    def index_file(self, filepath: str,
                   collection: str = "default",
                   agent_id: str = "") -> int:
        """Read a file and index its content."""
        p = Path(filepath)
        if not p.exists():
            raise FileNotFoundError(f"File not found: {filepath}")

        content = p.read_text(encoding="utf-8", errors="replace")
        title = p.stem.replace("-", " ").replace("_", " ")

        return self.index(
            title=title,
            content=content,
            collection=collection,
            path=str(p),
            agent_id=agent_id,
            source_type="file",
        )

    # ── Search ────────────────────────────────────────────────────────────

    def search(self, query: str, *,
               collection: str | None = None,
               limit: int = 10) -> list[dict]:
        """BM25 full-text search.

        Returns list of dicts:
          [{id, title, snippet, content, path, collection, rank, agent_id}]
        """
        if not query or not query.strip():
            return []

        # Escape FTS5 special characters for safety
        safe_query = self._escape_fts_query(query)
        if not safe_query:
            return []

        if collection:
            sql = """
                SELECT
                    m.id, c.title, c.tags, m.path, m.collection,
                    m.agent_id, m.source_type,
                    snippet(docs_fts, 1, '<b>', '</b>', '...', 40) AS snippet,
                    bm25(docs_fts) AS rank
                FROM docs_fts
                JOIN docs_content c ON c.id = docs_fts.rowid
                JOIN docs_meta m ON m.id = docs_fts.rowid
                WHERE docs_fts MATCH ?
                  AND m.collection = ?
                ORDER BY rank
                LIMIT ?
            """
            params = (safe_query, collection, limit)
        else:
            sql = """
                SELECT
                    m.id, c.title, c.tags, m.path, m.collection,
                    m.agent_id, m.source_type,
                    snippet(docs_fts, 1, '<b>', '</b>', '...', 40) AS snippet,
                    bm25(docs_fts) AS rank
                FROM docs_fts
                JOIN docs_content c ON c.id = docs_fts.rowid
                JOIN docs_meta m ON m.id = docs_fts.rowid
                WHERE docs_fts MATCH ?
                ORDER BY rank
                LIMIT ?
            """
            params = (safe_query, limit)

        try:
            rows = self.conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError as e:
            logger.warning("FTS5 search failed: %s (query=%r)", e, query)
            return []

        results = []
        for row in rows:
            results.append({
                "id": row["id"],
                "title": row["title"],
                "snippet": row["snippet"] or "",
                "tags": row["tags"] or "",
                "path": row["path"] or "",
                "collection": row["collection"],
                "agent_id": row["agent_id"] or "",
                "source_type": row["source_type"] or "",
                "rank": row["rank"],
            })
        return results

    @staticmethod
    def _escape_fts_query(query: str) -> str:
        """Escape an FTS5 query for safe MATCH.

        Strategy: split into tokens, wrap each in double quotes
        to treat them as literal terms, join with spaces (implicit AND).
        """
        tokens = query.strip().split()
        if not tokens:
            return ""
        # Wrap each token in quotes to escape special chars
        escaped = []
        for t in tokens:
            # Remove any existing quotes
            t = t.replace('"', '')
            if t:
                escaped.append(f'"{t}"')
        return " ".join(escaped)

    # ── Delete ────────────────────────────────────────────────────────────

    def delete(self, doc_id: int):
        """Delete a document by ID."""
        self.conn.execute("DELETE FROM docs_content WHERE id = ?", (doc_id,))
        self.conn.execute("DELETE FROM docs_meta WHERE id = ?", (doc_id,))
        self.conn.commit()

    def delete_by_path(self, path: str):
        """Delete all documents with the given path."""
        rows = self.conn.execute(
            "SELECT id FROM docs_meta WHERE path = ?", (path,)
        ).fetchall()
        for row in rows:
            self.delete(row["id"])

    def delete_collection(self, collection: str):
        """Delete all documents in a collection."""
        rows = self.conn.execute(
            "SELECT id FROM docs_meta WHERE collection = ?", (collection,)
        ).fetchall()
        for row in rows:
            self.conn.execute(
                "DELETE FROM docs_content WHERE id = ?", (row["id"],))
            self.conn.execute(
                "DELETE FROM docs_meta WHERE id = ?", (row["id"],))
        self.conn.commit()

    # ── Stats ─────────────────────────────────────────────────────────────

    def stats(self) -> dict:
        """Return index statistics."""
        total = self.conn.execute(
            "SELECT COUNT(*) FROM docs_meta").fetchone()[0]

        by_collection = {}
        for row in self.conn.execute(
            "SELECT collection, COUNT(*) as cnt FROM docs_meta "
            "GROUP BY collection"
        ).fetchall():
            by_collection[row["collection"]] = row["cnt"]

        db_size = 0
        if self.db_path != ":memory:" and os.path.exists(self.db_path):
            db_size = os.path.getsize(self.db_path)

        return {
            "total_docs": total,
            "by_collection": by_collection,
            "db_size_bytes": db_size,
            "db_size_kb": round(db_size / 1024, 1) if db_size else 0,
            "db_path": self.db_path,
        }

    # ── Lifecycle ─────────────────────────────────────────────────────────

    def close(self):
        """Close the database connection."""
        if self.conn:
            self.conn.close()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass
