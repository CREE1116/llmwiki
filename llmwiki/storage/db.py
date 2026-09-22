"""
SQLite storage engine with FTS5 full-text indexing and relation graph edges.
Zero external database dependencies.
"""
import sqlite3
import json
import re
from pathlib import Path
from typing import List, Dict, Optional, Tuple, Any
from .models import Concept, Relation, SearchResult
from ..config import DB_PATH

class Database:
    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        self._init_db()

    def get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _init_db(self):
        """Initialize relational schema and FTS5 full text search table."""
        with self.get_connection() as conn:
            # 1. Concepts table
            conn.execute("""
            CREATE TABLE IF NOT EXISTS concepts (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                type TEXT NOT NULL,
                summary TEXT,
                aliases_json TEXT,
                tags_json TEXT,
                created_at TEXT,
                updated_at TEXT
            )
            """)

            # 2. Relations table (Knowledge Graph Edges)
            conn.execute("""
            CREATE TABLE IF NOT EXISTS relations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id TEXT NOT NULL,
                relation_type TEXT NOT NULL,
                target_id TEXT NOT NULL,
                reason TEXT,
                FOREIGN KEY (source_id) REFERENCES concepts(id) ON DELETE CASCADE
            )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_rel_source ON relations(source_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_rel_target ON relations(target_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_rel_type ON relations(relation_type)")

            # 3. Sources table
            conn.execute("""
            CREATE TABLE IF NOT EXISTS sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                concept_id TEXT NOT NULL,
                source_uri TEXT NOT NULL,
                FOREIGN KEY (concept_id) REFERENCES concepts(id) ON DELETE CASCADE
            )
            """)

            # 4. FTS5 Virtual Table for Sub-millisecond Full-Text Search
            conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS concepts_fts USING fts5(
                id UNINDEXED,
                name,
                aliases,
                tags,
                summary,
                body,
                tokenize = 'porter unicode61'
            )
            """)

            # 5. Query Logs Table (Agent Access Tracking)
            conn.execute("""
            CREATE TABLE IF NOT EXISTS query_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                caller TEXT NOT NULL,
                action TEXT NOT NULL,
                query TEXT,
                details_json TEXT,
                result_count INTEGER DEFAULT 0
            )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_timestamp ON query_logs(timestamp DESC)")
            conn.commit()

    def upsert_concept(self, concept: Concept, full_markdown: str):
        """Index or update a concept and its edges."""
        with self.get_connection() as conn:
            aliases_str = " ".join(concept.aliases)
            tags_str = " ".join(concept.tags)

            # Insert/Replace in concepts table
            conn.execute("""
            INSERT OR REPLACE INTO concepts (
                id, name, type, summary, aliases_json, tags_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                concept.id,
                concept.name,
                concept.type,
                concept.summary,
                json.dumps(concept.aliases, ensure_ascii=False),
                json.dumps(concept.tags, ensure_ascii=False),
                concept.created_at,
                concept.updated_at
            ))

            # Delete old relations and re-insert
            conn.execute("DELETE FROM relations WHERE source_id = ?", (concept.id,))
            for rel in concept.relations:
                if rel.target and rel.target != concept.id:
                    conn.execute("""
                    INSERT INTO relations (source_id, relation_type, target_id, reason)
                    VALUES (?, ?, ?, ?)
                    """, (concept.id, rel.type, rel.target, rel.reason))

            # Delete old sources and re-insert
            conn.execute("DELETE FROM sources WHERE concept_id = ?", (concept.id,))
            for src in concept.sources:
                conn.execute("INSERT INTO sources (concept_id, source_uri) VALUES (?, ?)", (concept.id, src))

            # Update FTS5 index
            conn.execute("DELETE FROM concepts_fts WHERE id = ?", (concept.id,))
            conn.execute("""
            INSERT INTO concepts_fts (id, name, aliases, tags, summary, body)
            VALUES (?, ?, ?, ?, ?, ?)
            """, (
                concept.id,
                concept.name,
                aliases_str,
                tags_str,
                concept.summary,
                full_markdown
            ))
            conn.commit()

    def search(self, query: str, limit: int = 5) -> List[SearchResult]:
        """Perform high-speed FTS5 full-text search with BM25 rank."""
        if not query.strip():
            return []

        # Sanitize query for FTS5: wrap words in quotes or escape special characters
        clean_words = [w.replace('"', '""') for w in query.split() if w.strip()]
        if not clean_words:
            return []

        # Build query for matching either prefix or exact tokens
        fts_query = " OR ".join([f'"{w}"*' for w in clean_words])

        with self.get_connection() as conn:
            try:
                cursor = conn.execute("""
                SELECT
                    c.id, c.name, c.type, c.summary, c.tags_json,
                    bm25(concepts_fts, 10.0, 5.0, 3.0, 2.0, 1.0) AS score
                FROM concepts_fts
                JOIN concepts c ON concepts_fts.id = c.id
                WHERE concepts_fts MATCH ?
                ORDER BY score
                LIMIT ?
                """, (fts_query, limit))

                rows = cursor.fetchall()
            except sqlite3.OperationalError:
                # Fallback to simple LIKE search if FTS query syntax error occurs
                cursor = conn.execute("""
                SELECT id, name, type, summary, tags_json, 0.0 as score
                FROM concepts
                WHERE name LIKE ? OR summary LIKE ? OR id LIKE ?
                LIMIT ?
                """, (f"%{query}%", f"%{query}%", f"%{query}%", limit))
                rows = cursor.fetchall()

            results = []
            for row in rows:
                tags = json.loads(row["tags_json"]) if row["tags_json"] else []
                results.append(SearchResult(
                    concept_id=row["id"],
                    name=row["name"],
                    type=row["type"],
                    summary=row["summary"] or "",
                    tags=tags,
                    score=abs(float(row["score"])),
                    matched_by="fts"
                ))
            return results

    def get_concept(self, concept_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve concept row by ID."""
        with self.get_connection() as conn:
            cursor = conn.execute("SELECT * FROM concepts WHERE id = ?", (concept_id,))
            row = cursor.fetchone()
            if not row:
                return None
            return dict(row)

    def get_relations_for(self, concept_id: str) -> List[Dict[str, str]]:
        """Retrieve all outgoing and incoming relations for a concept."""
        with self.get_connection() as conn:
            cursor = conn.execute("""
            SELECT 'outgoing' AS direction, relation_type, target_id AS related_id, reason
            FROM relations WHERE source_id = ?
            UNION ALL
            SELECT 'incoming' AS direction, relation_type, source_id AS related_id, reason
            FROM relations WHERE target_id = ?
            """, (concept_id, concept_id))
            return [dict(r) for r in cursor.fetchall()]

    def list_all_ids(self) -> List[str]:
        """List all concept IDs in the store."""
        with self.get_connection() as conn:
            cursor = conn.execute("SELECT id FROM concepts ORDER BY id ASC")
            return [r["id"] for r in cursor.fetchall()]

    def prune_concepts(self, valid_ids: List[str]) -> None:
        """Remove index rows whose source concept file no longer exists."""
        with self.get_connection() as conn:
            if valid_ids:
                placeholders = ",".join("?" for _ in valid_ids)
                stale = conn.execute(
                    f"SELECT id FROM concepts WHERE id NOT IN ({placeholders})", valid_ids
                ).fetchall()
            else:
                stale = conn.execute("SELECT id FROM concepts").fetchall()
            for row in stale:
                concept_id = row["id"]
                conn.execute("DELETE FROM concepts_fts WHERE id = ?", (concept_id,))
                conn.execute("DELETE FROM relations WHERE target_id = ?", (concept_id,))
                conn.execute("DELETE FROM concepts WHERE id = ?", (concept_id,))
            conn.commit()

    def delete_concept(self, concept_id: str) -> bool:
        """Delete one concept and every database-side reference to it."""
        with self.get_connection() as conn:
            exists = conn.execute(
                "SELECT 1 FROM concepts WHERE id = ?", (concept_id,)
            ).fetchone()
            if not exists:
                return False
            conn.execute("DELETE FROM concepts_fts WHERE id = ?", (concept_id,))
            conn.execute("DELETE FROM relations WHERE target_id = ?", (concept_id,))
            # Outgoing relations, sources and vectors are removed by FK cascades.
            conn.execute("DELETE FROM concepts WHERE id = ?", (concept_id,))
            conn.commit()
            return True

    def log_query(self, caller: str, action: str, query: str, details: Any = None, result_count: int = 0):
        """Record an agent or user retrieval/action event."""
        import datetime
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        details_str = json.dumps(details, ensure_ascii=False) if details else ""
        with self.get_connection() as conn:
            conn.execute("""
            INSERT INTO query_logs (timestamp, caller, action, query, details_json, result_count)
            VALUES (?, ?, ?, ?, ?, ?)
            """, (now, caller, action, query, details_str, result_count))
            conn.commit()

    def get_query_logs(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Retrieve recent query audit logs."""
        with self.get_connection() as conn:
            cursor = conn.execute("""
            SELECT id, timestamp, caller, action, query, details_json, result_count
            FROM query_logs ORDER BY id DESC LIMIT ?
            """, (limit,))
            results = []
            for r in cursor.fetchall():
                d = dict(r)
                if d.get("details_json"):
                    try:
                        d["details"] = json.loads(d["details_json"])
                    except Exception:
                        d["details"] = d["details_json"]
                else:
                    d["details"] = None
                results.append(d)
            return results

    def get_sources_for(self, concept_id: str) -> List[Dict[str, Any]]:
        """Retrieve all sources linked to a concept with document details if available."""
        with self.get_connection() as conn:
            cursor = conn.execute("""
            SELECT s.source_uri, d.id as doc_id, d.title, d.char_count
            FROM sources s
            LEFT JOIN raw_documents d ON s.source_uri = ('raw:' || d.id)
            WHERE s.concept_id = ?
            """, (concept_id,))
            return [dict(r) for r in cursor.fetchall()]

    def add_relation(self, source_id: str, relation_type: str, target_id: str, reason: str = "") -> bool:
        """Insert a single relation edge if not already existing."""
        if source_id == target_id:
            return False
        with self.get_connection() as conn:
            exists = conn.execute("""
            SELECT 1 FROM relations
            WHERE (source_id = ? AND target_id = ? AND relation_type = ?)
               OR (source_id = ? AND target_id = ? AND relation_type = ?)
            """, (source_id, target_id, relation_type, target_id, source_id, relation_type)).fetchone()
            if exists:
                return False
            conn.execute("""
            INSERT INTO relations (source_id, relation_type, target_id, reason)
            VALUES (?, ?, ?, ?)
            """, (source_id, relation_type, target_id, reason))
            conn.commit()
            return True

    def get_full_graph_data(self) -> Dict[str, Any]:
        """Return all nodes and edges for visualization in Electron/Web."""
        with self.get_connection() as conn:
            c_cursor = conn.execute("""
            SELECT c.id, c.name, c.type, c.summary, c.tags_json, COUNT(s.id) as sources_count
            FROM concepts c
            LEFT JOIN sources s ON c.id = s.concept_id
            GROUP BY c.id
            """)
            nodes = []
            for r in c_cursor.fetchall():
                tags = json.loads(r["tags_json"]) if r["tags_json"] else []
                nodes.append({
                    "id": r["id"],
                    "name": r["name"],
                    "type": r["type"],
                    "summary": r["summary"] or "",
                    "tags": tags,
                    "sources_count": r["sources_count"] or 1
                })

            e_cursor = conn.execute("SELECT source_id, relation_type, target_id, reason FROM relations")
            edges = []
            for r in e_cursor.fetchall():
                edges.append({
                    "source": r["source_id"],
                    "target": r["target_id"],
                    "relation": r["relation_type"],
                    "reason": r["reason"] or "",
                    "inferred": False,
                })

            # Keep sparse or fallback-extracted libraries navigable. These links are
            # display-only similarities and never masquerade as authored relations.
            connected = {e["source"] for e in edges} | {e["target"] for e in edges}
            stopwords = {
                "the", "and", "for", "with", "from", "that", "this", "into", "via",
                "concept", "using", "used", "based", "their", "which", "are", "all",
            }

            def tokens(node):
                text = " ".join([node["name"], node["summary"], " ".join(node["tags"])])
                return {
                    token for token in re.findall(r"[a-zA-Z0-9가-힣]{3,}", text.lower())
                    if token not in stopwords and token != "auto_extracted"
                }

            token_map = {node["id"]: tokens(node) for node in nodes}
            existing_pairs = {frozenset((e["source"], e["target"])) for e in edges}
            for node in nodes:
                if node["id"] in connected or len(nodes) < 2:
                    continue
                best_id, best_score = None, 0.0
                own = token_map[node["id"]]
                for other in nodes:
                    if other["id"] == node["id"]:
                        continue
                    pair = frozenset((node["id"], other["id"]))
                    if pair in existing_pairs:
                        continue
                    theirs = token_map[other["id"]]
                    union = own | theirs
                    score = len(own & theirs) / len(union) if union else 0.0
                    if score > best_score:
                        best_id, best_score = other["id"], score
                if best_id and best_score >= 0.04:
                    edges.append({
                        "source": node["id"], "target": best_id,
                        "relation": "similar", "reason": "내용 유사도에 따른 탐색 연결",
                        "inferred": True,
                    })
                    connected.update((node["id"], best_id))
                    existing_pairs.add(frozenset((node["id"], best_id)))

            return {"nodes": nodes, "edges": edges}

    def stats(self) -> Dict[str, Any]:
        """Return counts of concepts, relations, sources, and query logs."""
        with self.get_connection() as conn:
            c_count = conn.execute("SELECT COUNT(*) FROM concepts").fetchone()[0]
            r_count = conn.execute("SELECT COUNT(*) FROM relations").fetchone()[0]
            s_count = conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
            l_count = conn.execute("SELECT COUNT(*) FROM query_logs").fetchone()[0]
            return {
                "total_concepts": c_count,
                "total_relations": r_count,
                "total_sources": s_count,
                "total_queries": l_count
            }
