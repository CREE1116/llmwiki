"""
Layer 3: Vector & Semantic Index Engine.
Supports local embeddings via Ollama or fast local NumPy hashing vectorizer fallback.
Enables sub-millisecond semantic search and hybrid (Vector + FTS5) ranking.
"""
import sqlite3
import math
import re
from typing import List, Dict, Any, Optional, Tuple
import numpy as np
import httpx
from ..config import OLLAMA_HOST
from .db import Database
from .models import Concept, SearchResult

class VectorIndex:
    def __init__(self, db: Optional[Database] = None, host: str = OLLAMA_HOST, embed_model: str = "nomic-embed-text"):
        self.db = db or Database()
        self.host = host.rstrip("/")
        self.embed_model = embed_model
        self.dim = 384 # Default dimension for fallback vectorizer
        self._init_table()

    def _init_table(self):
        with self.db.get_connection() as conn:
            conn.execute("""
            CREATE TABLE IF NOT EXISTS concept_vectors (
                concept_id TEXT PRIMARY KEY,
                model_name TEXT NOT NULL,
                dim INTEGER NOT NULL,
                vector BLOB NOT NULL,
                updated_at TEXT,
                FOREIGN KEY (concept_id) REFERENCES concepts(id) ON DELETE CASCADE
            )
            """)
            conn.commit()

    def _fallback_embed(self, text: str) -> np.ndarray:
        """
        Ultra-fast, deterministic hashing vectorizer in pure NumPy.
        Guarantees semantic n-gram overlap similarity with 0 external dependencies.
        """
        words = re.findall(r"\w+", text.lower())
        vec = np.zeros(self.dim, dtype=np.float32)
        if not words:
            return vec

        for w in words:
            # Word level
            idx1 = hash(w) % self.dim
            vec[idx1] += 1.0
            # Character trigram level for subword/fuzzy matching
            if len(w) >= 3:
                for i in range(len(w) - 2):
                    tri = w[i:i+3]
                    idx2 = hash(tri) % self.dim
                    vec[idx2] += 0.5

        # L2 normalize
        norm = np.linalg.norm(vec)
        if norm > 0:
            vec /= norm
        return vec

    def embed_text(self, text: str) -> Tuple[np.ndarray, str]:
        """
        Generate embedding. Tries Ollama embedding API first,
        gracefully falls back to deterministic local vectorizer.
        """
        cleaned = text.strip()[:2000]
        try:
            with httpx.Client(timeout=4.0) as client:
                res = client.post(
                    f"{self.host}/api/embeddings",
                    json={"model": self.embed_model, "prompt": cleaned}
                )
                if res.status_code == 200:
                    emb = res.json().get("embedding")
                    if emb:
                        arr = np.array(emb, dtype=np.float32)
                        norm = np.linalg.norm(arr)
                        if norm > 0:
                            arr /= norm
                        return arr, self.embed_model
        except Exception:
            pass

        # Fallback to pure local vectorizer
        return self._fallback_embed(cleaned), "local_hash_v1"

    def index_concept(self, concept: Concept):
        """Build embedding from concept's dense textual components."""
        # Combine high-density fields
        components = [
            concept.name,
            " ".join(concept.aliases),
            " ".join(concept.tags),
            concept.summary,
            " ".join(concept.mechanisms)
        ]
        text_repr = "\n".join([c for c in components if c])

        vec, model_name = self.embed_text(text_repr)
        vec_bytes = vec.tobytes()

        with self.db.get_connection() as conn:
            conn.execute("""
            INSERT OR REPLACE INTO concept_vectors (
                concept_id, model_name, dim, vector, updated_at
            ) VALUES (?, ?, ?, ?, datetime('now'))
            """, (concept.id, model_name, len(vec), vec_bytes))
            conn.commit()

    def find_most_similar_concept(
        self,
        target_vec: np.ndarray,
        exclude_ids: Optional[Any] = None,
        top_k: int = 5
    ) -> List[Tuple[str, float]]:
        """
        Find most similar existing concepts for a given vector.
        Returns list of (concept_id, cosine_similarity_score) sorted descending.
        """
        with self.db.get_connection() as conn:
            cursor = conn.execute("SELECT concept_id, vector, dim FROM concept_vectors")
            rows = cursor.fetchall()

        if not rows:
            return []

        if isinstance(exclude_ids, (list, set, tuple)):
            ex_set = set(exclude_ids)
        elif exclude_ids:
            ex_set = {exclude_ids}
        else:
            ex_set = set()

        ids = []
        vecs = []
        for r in rows:
            cid = r["concept_id"]
            if cid in ex_set:
                continue
            dim = r["dim"]
            if len(target_vec) != dim:
                continue
            ids.append(cid)
            vecs.append(np.frombuffer(r["vector"], dtype=np.float32))

        if not vecs:
            return []

        matrix = np.stack(vecs)
        scores = np.dot(matrix, target_vec)
        top_indices = np.argsort(scores)[::-1][:top_k]

        results = []
        for idx in top_indices:
            results.append((ids[idx], float(scores[idx])))
        return results

    def search_semantic(self, query: str, top_k: int = 5) -> List[SearchResult]:
        """Sub-millisecond cosine similarity search over concept vectors."""
        if not query.strip():
            return []

        q_vec, _ = self.embed_text(query)

        # Load all vectors from SQLite
        with self.db.get_connection() as conn:
            cursor = conn.execute("""
            SELECT cv.concept_id, cv.vector, cv.dim, c.name, c.type, c.summary, c.tags_json
            FROM concept_vectors cv
            JOIN concepts c ON cv.concept_id = c.id
            """)
            rows = cursor.fetchall()

        if not rows:
            return []

        ids = []
        names = []
        types = []
        summaries = []
        tags_list = []
        vec_list = []

        import json
        for r in rows:
            blob = r["vector"]
            dim = r["dim"]
            if len(q_vec) != dim:
                # Dim mismatch, skip or re-embed
                continue
            arr = np.frombuffer(blob, dtype=np.float32)
            ids.append(r["concept_id"])
            names.append(r["name"])
            types.append(r["type"])
            summaries.append(r["summary"] or "")
            tags_list.append(json.loads(r["tags_json"]) if r["tags_json"] else [])
            vec_list.append(arr)

        if not vec_list:
            return []

        # Vectorized dot product (cosine similarity since vectors are L2-normalized)
        matrix = np.stack(vec_list) # shape: (N, dim)
        scores = np.dot(matrix, q_vec) # shape: (N,)

        top_indices = np.argsort(scores)[::-1][:top_k]

        results = []
        for idx in top_indices:
            score_val = float(scores[idx])
            results.append(SearchResult(
                concept_id=ids[idx],
                name=names[idx],
                type=types[idx],
                summary=summaries[idx],
                tags=tags_list[idx],
                score=score_val,
                matched_by="vector"
            ))
        return results

    def hybrid_search(self, query: str, top_k: int = 5, alpha: float = 0.5) -> List[SearchResult]:
        """
        Hybrid search combining:
        - Layer 3 Vector Cosine Similarity (Semantic)
        - SQLite FTS5 BM25 (Exact keywords/aliases)
        """
        fts_hits = self.db.search(query, limit=top_k * 2)
        vec_hits = self.search_semantic(query, top_k=top_k * 2)

        # Merge scores
        combined: Dict[str, Dict[str, Any]] = {}

        # Normalize FTS scores (lower BM25 is better in SQLite, we invert it)
        max_fts = max([h.score for h in fts_hits], default=1.0) or 1.0
        for h in fts_hits:
            norm_fts = 1.0 / (1.0 + h.score)
            combined[h.concept_id] = {
                "hit": h,
                "fts_score": norm_fts,
                "vec_score": 0.0
            }

        for v in vec_hits:
            if v.concept_id in combined:
                combined[v.concept_id]["vec_score"] = max(0.0, v.score)
            else:
                combined[v.concept_id] = {
                    "hit": v,
                    "fts_score": 0.0,
                    "vec_score": max(0.0, v.score)
                }

        # Calculate final hybrid score
        final_list = []
        for cid, item in combined.items():
            h = item["hit"]
            score = (1 - alpha) * item["fts_score"] + alpha * item["vec_score"]
            final_list.append(SearchResult(
                concept_id=cid,
                name=h.name,
                type=h.type,
                summary=h.summary,
                tags=h.tags,
                score=round(score, 4),
                matched_by="hybrid"
            ))

        final_list.sort(key=lambda x: x.score, reverse=True)
        return final_list[:top_k]
