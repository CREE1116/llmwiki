"""
SQLite storage engine with FTS5 full-text indexing and relation graph edges.
Zero external database dependencies.
"""
import sqlite3
import json
import re
from pathlib import Path
from typing import List, Dict, Optional, Tuple, Any
from .models import Concept, Relation, SearchResult, Workspace
from ..config import DB_PATH

class Database:
    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        self._init_db()

    def get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA cache_size = -64000")
        conn.execute("PRAGMA temp_store = MEMORY")
        conn.execute("PRAGMA mmap_size = 268435456")
        return conn


    def _init_db(self):
        """Initialize relational schema, workspaces, and FTS5 full text search table."""
        with self.get_connection() as conn:
            # 0. Workspaces table
            conn.execute("""
            CREATE TABLE IF NOT EXISTS workspaces (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT,
                tags_json TEXT,
                vector BLOB,
                dim INTEGER,
                created_at TEXT,
                updated_at TEXT
            )
            """)

            # Ensure default workspace exists
            conn.execute("""
            INSERT OR IGNORE INTO workspaces (id, name, description, tags_json, created_at, updated_at)
            VALUES ('default', 'Default', 'Default general knowledge repository', '[]', datetime('now'), datetime('now'))
            """)

            # 1. Concepts table
            conn.execute("""
            CREATE TABLE IF NOT EXISTS concepts (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                type TEXT NOT NULL,
                summary TEXT,
                aliases_json TEXT,
                tags_json TEXT,
                workspace_id TEXT DEFAULT 'default',
                created_at TEXT,
                updated_at TEXT
            )
            """)

            # Migration: Add workspace_id to concepts if upgraded from older version
            cur = conn.execute("PRAGMA table_info(concepts)")
            cols = [r["name"] for r in cur.fetchall()]
            if "workspace_id" not in cols:
                conn.execute("ALTER TABLE concepts ADD COLUMN workspace_id TEXT DEFAULT 'default'")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_concepts_ws ON concepts(workspace_id)")

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
                workspace_id TEXT DEFAULT 'default',
                FOREIGN KEY (concept_id) REFERENCES concepts(id) ON DELETE CASCADE
            )
            """)

            # Migration: Add workspace_id to sources if upgraded
            cur_s = conn.execute("PRAGMA table_info(sources)")
            cols_s = [r["name"] for r in cur_s.fetchall()]
            if "workspace_id" not in cols_s:
                conn.execute("ALTER TABLE sources ADD COLUMN workspace_id TEXT DEFAULT 'default'")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_sources_ws ON sources(workspace_id)")

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
                result_count INTEGER DEFAULT 0,
                workspace_id TEXT DEFAULT 'default'
            )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_timestamp ON query_logs(timestamp DESC)")

            # Migration: Add workspace_id to query_logs if upgraded
            cur_q = conn.execute("PRAGMA table_info(query_logs)")
            cols_q = [r["name"] for r in cur_q.fetchall()]
            if "workspace_id" not in cols_q:
                conn.execute("ALTER TABLE query_logs ADD COLUMN workspace_id TEXT DEFAULT 'default'")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_ws ON query_logs(workspace_id)")
            conn.commit()


    def upsert_concept(self, concept: Concept, full_markdown: str):
        """Index or update a concept and its edges within its assigned workspace."""
        ws_id = getattr(concept, "workspace", None) or "default"
        with self.get_connection() as conn:
            aliases_str = " ".join(concept.aliases)
            tags_str = " ".join(concept.tags)

            # Insert/Replace in concepts table with workspace_id
            conn.execute("""
            INSERT OR REPLACE INTO concepts (
                id, name, type, summary, aliases_json, tags_json, workspace_id, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                concept.id,
                concept.name,
                concept.type,
                concept.summary,
                json.dumps(concept.aliases, ensure_ascii=False),
                json.dumps(concept.tags, ensure_ascii=False),
                ws_id,
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

    def create_workspace(self, ws_id: str, name: str, description: str = "", tags: Optional[List[str]] = None) -> Workspace:
        import time
        raw_id = (ws_id or name or "").strip().lower()
        clean_id = re.sub(r"[^\w-]", "_", raw_id).strip("_")
        if not clean_id:
            clean_id = f"ws_{int(time.time())}"
        display_name = (name or clean_id).strip()
        tags_json = json.dumps(tags or [], ensure_ascii=False)
        with self.get_connection() as conn:
            conn.execute("""
            INSERT OR REPLACE INTO workspaces (id, name, description, tags_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, datetime('now'), datetime('now'))
            """, (clean_id, display_name, description.strip(), tags_json))
            conn.commit()
        ws = self.get_workspace(clean_id)
        if not ws:
            raise RuntimeError(f"Failed to create workspace '{clean_id}'")
        return ws

    def get_workspace(self, ws_id: str) -> Optional[Workspace]:
        if not ws_id:
            return None
        clean = ws_id.strip()
        with self.get_connection() as conn:
            # 1. Exact ID match
            row = conn.execute("""
            SELECT w.*, 
                   (SELECT COUNT(*) FROM concepts WHERE workspace_id = w.id) as concept_count,
                   (SELECT COUNT(*) FROM sources WHERE workspace_id = w.id) as raw_count
            FROM workspaces w WHERE w.id = ?
            """, (clean,)).fetchone()

            # 2. Case-insensitive or Name match
            if not row:
                row = conn.execute("""
                SELECT w.*, 
                       (SELECT COUNT(*) FROM concepts WHERE workspace_id = w.id) as concept_count,
                       (SELECT COUNT(*) FROM sources WHERE workspace_id = w.id) as raw_count
                FROM workspaces w 
                WHERE LOWER(w.id) = LOWER(?) OR LOWER(w.name) = LOWER(?)
                LIMIT 1
                """, (clean, clean)).fetchone()

            # 3. Known common alias fallback
            if not row:
                alias_map = {"typemoon": "타입문", "타입문": "typemoon", "type_moon": "타입문"}
                if clean.lower() in alias_map:
                    target_alias = alias_map[clean.lower()]
                    row = conn.execute("""
                    SELECT w.*, 
                           (SELECT COUNT(*) FROM concepts WHERE workspace_id = w.id) as concept_count,
                           (SELECT COUNT(*) FROM sources WHERE workspace_id = w.id) as raw_count
                    FROM workspaces w 
                    WHERE w.id = ? OR w.name = ? OR LOWER(w.id) = LOWER(?) OR LOWER(w.name) = LOWER(?)
                    LIMIT 1
                    """, (target_alias, target_alias, target_alias, target_alias)).fetchone()

            if not row:
                return None
            return Workspace(
                id=row["id"],
                name=row["name"],
                description=row["description"] or "",
                tags=json.loads(row["tags_json"]) if row["tags_json"] else [],
                created_at=row["created_at"] or "",
                updated_at=row["updated_at"] or "",
                concept_count=row["concept_count"],
                raw_count=row["raw_count"]
            )

    def resolve_workspace_id(self, ws_id_or_name: Optional[str]) -> Optional[str]:
        """Resolve a workspace ID, name, or alias to canonical ID in this database."""
        if not ws_id_or_name or ws_id_or_name in ("all", "*"):
            return None
        ws = self.get_workspace(ws_id_or_name)
        if ws:
            return ws.id
        return ws_id_or_name

    def list_workspaces(self) -> List[Workspace]:
        with self.get_connection() as conn:
            rows = conn.execute("""
            SELECT w.*, 
                   (SELECT COUNT(*) FROM concepts WHERE workspace_id = w.id) as concept_count,
                   (SELECT COUNT(*) FROM sources WHERE workspace_id = w.id) as raw_count
            FROM workspaces w ORDER BY w.id ASC
            """).fetchall()
            return [
                Workspace(
                    id=r["id"],
                    name=r["name"],
                    description=r["description"] or "",
                    tags=json.loads(r["tags_json"]) if r["tags_json"] else [],
                    created_at=r["created_at"] or "",
                    updated_at=r["updated_at"] or "",
                    concept_count=r["concept_count"],
                    raw_count=r["raw_count"]
                )
                for r in rows
            ]

    def delete_workspace(self, ws_id: str) -> bool:
        if ws_id == "default":
            return False  # Cannot delete default workspace
        with self.get_connection() as conn:
            conn.execute("DELETE FROM workspaces WHERE id = ?", (ws_id,))
            conn.execute("UPDATE concepts SET workspace_id = 'default' WHERE workspace_id = ?", (ws_id,))
            conn.execute("UPDATE sources SET workspace_id = 'default' WHERE workspace_id = ?", (ws_id,))
            conn.commit()
            return True

    def update_workspace_vector(self, ws_id: str, vector: bytes, dim: int):
        with self.get_connection() as conn:
            conn.execute("""
            UPDATE workspaces SET vector = ?, dim = ?, updated_at = datetime('now')
            WHERE id = ?
            """, (vector, dim, ws_id))
            conn.commit()

    def get_workspace_vectors(self) -> List[Dict[str, Any]]:
        with self.get_connection() as conn:
            rows = conn.execute("""
            SELECT id, name, description, vector, dim FROM workspaces
            WHERE vector IS NOT NULL
            """).fetchall()
            return [
                {
                    "id": r["id"],
                    "name": r["name"],
                    "description": r["description"] or "",
                    "vector": r["vector"],
                    "dim": r["dim"]
                }
                for r in rows
            ]

    def search(self, query: str, limit: int = 5, workspace: str = "all") -> List[SearchResult]:
        """Perform high-speed FTS5 full-text search with BM25 rank, optionally filtered by workspace."""
        if not query.strip():
            return []

        # Sanitize query for FTS5: wrap words in quotes or escape special characters
        clean_words = [re.sub(r'[^\w가-힣]', '', w) for w in query.split()]
        clean_words = [w for w in clean_words if w]
        if not clean_words:
            return []

        # Build query for matching exact phrase, AND tokens, and OR prefix tokens
        if len(clean_words) > 1:
            full_phrase = " ".join(clean_words)
            and_part = " AND ".join([f'"{w}"*' for w in clean_words])
            or_part = " OR ".join([f'"{w}"*' for w in clean_words])
            fts_query = f'"{full_phrase}" OR ({and_part}) OR ({or_part})'
        else:
            fts_query = f'"{clean_words[0]}"*'

        resolved_ws = self.resolve_workspace_id(workspace)
        with self.get_connection() as conn:
            try:
                if resolved_ws:
                    sql = """
                    SELECT
                        c.id, c.name, c.type, c.summary, c.tags_json, c.workspace_id,
                        bm25(concepts_fts, 10.0, 5.0, 3.0, 2.0, 1.0) AS score
                    FROM concepts_fts
                    JOIN concepts c ON concepts_fts.id = c.id
                    WHERE concepts_fts MATCH ? AND c.workspace_id = ?
                    ORDER BY score ASC
                    LIMIT ?
                    """
                    cursor = conn.execute(sql, (fts_query, resolved_ws, limit))
                else:
                    sql = """
                    SELECT
                        c.id, c.name, c.type, c.summary, c.tags_json, c.workspace_id,
                        bm25(concepts_fts, 10.0, 5.0, 3.0, 2.0, 1.0) AS score
                    FROM concepts_fts
                    JOIN concepts c ON concepts_fts.id = c.id
                    WHERE concepts_fts MATCH ?
                    ORDER BY score ASC
                    LIMIT ?
                    """
                    cursor = conn.execute(sql, (fts_query, limit))

                rows = cursor.fetchall()
            except sqlite3.OperationalError:
                # Fallback to simple LIKE search if FTS query syntax error occurs
                if resolved_ws:
                    sql = """
                    SELECT id, name, type, summary, tags_json, workspace_id, -1.0 as score
                    FROM concepts
                    WHERE (name LIKE ? OR summary LIKE ? OR id LIKE ?) AND workspace_id = ?
                    LIMIT ?
                    """
                    cursor = conn.execute(sql, (f"%{query}%", f"%{query}%", f"%{query}%", resolved_ws, limit))
                else:
                    sql = """
                    SELECT id, name, type, summary, tags_json, workspace_id, -1.0 as score
                    FROM concepts
                    WHERE name LIKE ? OR summary LIKE ? OR id LIKE ?
                    LIMIT ?
                    """
                    cursor = conn.execute(sql, (f"%{query}%", f"%{query}%", f"%{query}%", limit))
                rows = cursor.fetchall()

            results = []
            for row in rows:
                tags = json.loads(row["tags_json"]) if row["tags_json"] else []
                raw_bm25 = float(row["score"])
                pos_score = max(0.01, -raw_bm25) if raw_bm25 < 0 else 0.5
                ws = row["workspace_id"] if "workspace_id" in row.keys() else "default"
                results.append(SearchResult(
                    concept_id=row["id"],
                    name=row["name"],
                    type=row["type"],
                    summary=row["summary"] or "",
                    tags=tags,
                    score=round(pos_score, 4),
                    matched_by="fts",
                    workspace=ws or "default"
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

    def list_all_ids(self, workspace: Optional[str] = None) -> List[str]:
        """List all concept IDs in the store, optionally filtered by workspace."""
        resolved_ws = self.resolve_workspace_id(workspace)
        with self.get_connection() as conn:
            if resolved_ws:
                cursor = conn.execute("SELECT id FROM concepts WHERE workspace_id = ? ORDER BY id ASC", (resolved_ws,))
            else:
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

    def log_query(self, caller: str, action: str, query: str, details: Any = None, result_count: int = 0, workspace: str = "default"):
        """Record an agent or user retrieval/action event."""
        import datetime
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        details_str = json.dumps(details, ensure_ascii=False) if details else ""
        ws_id = workspace or "default"
        with self.get_connection() as conn:
            conn.execute("""
            INSERT INTO query_logs (timestamp, caller, action, query, details_json, result_count, workspace_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (now, caller, action, query, details_str, result_count, ws_id))
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

    def get_full_graph_data(self, workspace: Optional[str] = None) -> Dict[str, Any]:
        """Return all nodes and edges for visualization in Electron/Web, optionally scoped to a workspace."""
        resolved_ws = self.resolve_workspace_id(workspace)
        with self.get_connection() as conn:
            if resolved_ws:
                c_cursor = conn.execute("""
                SELECT c.id, c.name, c.type, c.summary, c.tags_json, c.workspace_id, COUNT(s.id) as sources_count
                FROM concepts c
                LEFT JOIN sources s ON c.id = s.concept_id
                WHERE c.workspace_id = ?
                GROUP BY c.id
                """, (resolved_ws,))
            else:
                c_cursor = conn.execute("""
                SELECT c.id, c.name, c.type, c.summary, c.tags_json, c.workspace_id, COUNT(s.id) as sources_count
                FROM concepts c
                LEFT JOIN sources s ON c.id = s.concept_id
                GROUP BY c.id
                """)
            nodes = []
            node_ids = set()
            for r in c_cursor.fetchall():
                tags = json.loads(r["tags_json"]) if r["tags_json"] else []
                node_ids.add(r["id"])
                nodes.append({
                    "id": r["id"],
                    "name": r["name"],
                    "type": r["type"],
                    "summary": r["summary"] or "",
                    "tags": tags,
                    "workspace": r["workspace_id"] or "default",
                    "sources_count": r["sources_count"] or 1
                })

            e_cursor = conn.execute("SELECT source_id, relation_type, target_id, reason FROM relations")
            edges = []
            for r in e_cursor.fetchall():
                if resolved_ws:
                    if r["source_id"] not in node_ids or r["target_id"] not in node_ids:
                        continue
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

    def stats(self, workspace: Optional[str] = None) -> Dict[str, Any]:
        """Return counts of concepts, relations, sources, and query logs, optionally for a workspace."""
        resolved_ws = self.resolve_workspace_id(workspace)
        with self.get_connection() as conn:
            if resolved_ws:
                c_count = conn.execute("SELECT COUNT(*) FROM concepts WHERE workspace_id = ?", (resolved_ws,)).fetchone()[0]
                r_count = conn.execute("""
                SELECT COUNT(*) FROM relations r
                JOIN concepts c ON r.source_id = c.id
                WHERE c.workspace_id = ?
                """, (resolved_ws,)).fetchone()[0]
                s_count = conn.execute("SELECT COUNT(*) FROM sources WHERE workspace_id = ?", (resolved_ws,)).fetchone()[0]
                l_count = conn.execute("SELECT COUNT(*) FROM query_logs WHERE workspace_id = ?", (resolved_ws,)).fetchone()[0]
                ws = self.get_workspace(resolved_ws)
                ws_name = ws.name if ws else resolved_ws
            else:
                c_count = conn.execute("SELECT COUNT(*) FROM concepts").fetchone()[0]
                r_count = conn.execute("SELECT COUNT(*) FROM relations").fetchone()[0]
                s_count = conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
                l_count = conn.execute("SELECT COUNT(*) FROM query_logs").fetchone()[0]
                ws_name = "all"

            w_count = conn.execute("SELECT COUNT(*) FROM workspaces").fetchone()[0]
            return {
                "workspace": ws_name,
                "total_workspaces": w_count,
                "total_concepts": c_count,
                "total_relations": r_count,
                "total_sources": s_count,
                "total_queries": l_count
            }

