"""
Layer 1: Raw Document Storage.
Safely archives full ground-truth source text (papers, web articles, files).
Allows agents to drill down for exact proofs, numbers, or code without bloating
the Layer 2 concept summary layer.
"""
from pathlib import Path
from typing import Optional, Dict, Any
import hashlib
import json
from datetime import datetime
from ..config import RAW_DIR
from .db import Database

class RawStore:
    def __init__(self, raw_dir: Path = RAW_DIR, db: Optional[Database] = None):
        self.raw_dir = raw_dir
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.db = db or Database()
        self._init_table()

    def _init_table(self):
        with self.db.get_connection() as conn:
            conn.execute("""
            CREATE TABLE IF NOT EXISTS raw_documents (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                source_uri TEXT NOT NULL,
                file_path TEXT NOT NULL,
                char_count INTEGER,
                created_at TEXT
            )
            """)
            conn.commit()

    def compute_doc_id(self, source_uri: str, content: str) -> str:
        """Create deterministic doc ID from URI or hash."""
        h = hashlib.sha256(f"{source_uri}:{content[:500]}".encode("utf-8")).hexdigest()[:16]
        # Clean title slug
        slug = Path(source_uri).stem.lower()
        slug = "".join(c if c.isalnum() else "_" for c in slug).strip("_")
        if slug and len(slug) < 30:
            return f"{slug}_{h[:8]}"
        return f"doc_{h}"

    def save(self, title: str, source_uri: str, content: str, doc_id: Optional[str] = None) -> str:
        """Store raw document to disk and record metadata in SQLite."""
        if not doc_id:
            doc_id = self.compute_doc_id(source_uri, content)

        file_path = self.raw_dir / f"{doc_id}.txt"
        file_path.write_text(content, encoding="utf-8")

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self.db.get_connection() as conn:
            conn.execute("""
            INSERT OR REPLACE INTO raw_documents (
                id, title, source_uri, file_path, char_count, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """, (
                doc_id,
                title,
                source_uri,
                str(file_path.resolve()),
                len(content),
                now
            ))
            conn.commit()

        return doc_id

    def get(self, doc_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve raw document content and metadata."""
        with self.db.get_connection() as conn:
            cursor = conn.execute("SELECT * FROM raw_documents WHERE id = ?", (doc_id,))
            row = cursor.fetchone()
            if not row:
                return None

            file_path = Path(row["file_path"])
            content = file_path.read_text(encoding="utf-8") if file_path.exists() else ""
            return {
                "id": row["id"],
                "title": row["title"],
                "source_uri": row["source_uri"],
                "content": content,
                "char_count": row["char_count"],
                "created_at": row["created_at"]
            }

    def list_all(self, limit: int = 50) -> list:
        """List all archived raw documents."""
        with self.db.get_connection() as conn:
            cursor = conn.execute("""
            SELECT id, title, source_uri, char_count, created_at
            FROM raw_documents ORDER BY created_at DESC LIMIT ?
            """, (limit,))
            return [dict(r) for r in cursor.fetchall()]

    def delete(self, doc_id: str) -> bool:
        """Delete raw document file from disk and database record."""
        with self.db.get_connection() as conn:
            cursor = conn.execute("SELECT file_path FROM raw_documents WHERE id = ?", (doc_id,))
            row = cursor.fetchone()
            if not row:
                return False

            file_path = Path(row["file_path"])
            if file_path.exists():
                try:
                    file_path.unlink()
                except Exception as e:
                    import sys
                    print(f"Warning: Failed to delete raw file {file_path}: {e}", file=sys.stderr)

            conn.execute("DELETE FROM raw_documents WHERE id = ?", (doc_id,))
            conn.execute("DELETE FROM sources WHERE source_uri = ?", (f"raw:{doc_id}",))
            conn.commit()
            return True
