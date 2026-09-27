"""
Layer 3: Vector & Semantic Index Engine.
Supports local embeddings via Ollama or fast local NumPy hashing vectorizer fallback.
Enables sub-millisecond semantic search and hybrid (Vector + FTS5) ranking.
"""
import sqlite3
import math
import re
from typing import List, Dict, Any, Optional, Tuple
import sqlite3
import math
import re
from typing import List, Dict, Any, Optional, Tuple
import numpy as np
import httpx
from ..config import OLLAMA_HOST
from .db import Database
from .models import Concept, SearchResult


def _fnv1a_32(token: str) -> int:
    """
    32-bit FNV-1a deterministic hash.
    Zero external dependencies, 100% stable across processes, Python versions, and operating systems.
    """
    h = 2166136261
    for b in token.encode("utf-8"):
        h ^= b
        h = (h * 16777619) & 0xFFFFFFFF
    return h


class VectorIndex:
    def __init__(self, db: Optional[Database] = None, host: str = OLLAMA_HOST, embed_model: str = "nomic-embed-text"):
        self.db = db or Database()
        self.host = host.rstrip("/")
        self.embed_model = embed_model
        self.dim = 384  # Default dimension for fallback vectorizer
        self._init_table()

        # In-memory vector matrix cache for sub-millisecond retrieval
        self._cache_valid = False
        self._cached_ids: List[str] = []
        self._cached_meta: List[Dict[str, Any]] = []
        self._cached_matrix: Optional[np.ndarray] = None
        self._cached_dims: List[int] = []

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

    def invalidate_cache(self):
        """Invalidate the cached in-memory vector matrix."""
        self._cache_valid = False
        self._cached_matrix = None
        self._cached_ids.clear()
        self._cached_meta.clear()
        self._cached_dims.clear()

    def _fallback_embed(self, text: str) -> np.ndarray:
        """
        Ultra-fast, deterministic hashing vectorizer in pure NumPy.
        Uses FNV-1a hashing for cross-process determinism, with:
        - Word-level unigrams with position-decay weighting
        - Word-level bigrams for syntactic phrase capture
        - Character trigrams for morphological and subword/fuzzy matching
        Guarantees stable cosine similarity with 0 external dependencies.
        """
        words = re.findall(r"[\w가-힣]+", text.lower())
        vec = np.zeros(self.dim, dtype=np.float32)
        if not words:
            return vec

        total_words = len(words)
        for i, w in enumerate(words):
            # Positional weight: words earlier in text (e.g. title/summary) have higher importance
            pos_weight = 1.0 + max(0.0, (10 - i) / 10.0) * 0.5

            # 1. Word level unigram
            idx1 = _fnv1a_32(w) % self.dim
            vec[idx1] += 1.0 * pos_weight

            # 2. Word level bigram for phrase context
            if i < total_words - 1:
                bigram = f"{w}_{words[i+1]}"
                idx_bi = _fnv1a_32(bigram) % self.dim
                vec[idx_bi] += 1.2 * pos_weight

            # 3. Subword character trigrams for fuzzy and morphological overlap
            if len(w) >= 3:
                for j in range(len(w) - 2):
                    tri = w[j:j+3]
                    idx_tri = _fnv1a_32(tri) % self.dim
                    vec[idx_tri] += 0.4

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
        cleaned = text.strip()[:3000]
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

        # Fallback to deterministic local vectorizer
        return self._fallback_embed(cleaned), "local_hash_v2"

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

        self.invalidate_cache()

    def _ensure_cache(self):
        """Load and cache all concept vectors in memory as a contiguous NumPy matrix with workspace metadata."""
        if self._cache_valid and self._cached_matrix is not None:
            return

        import json
        with self.db.get_connection() as conn:
            cursor = conn.execute("""
            SELECT cv.concept_id, cv.vector, cv.dim, c.name, c.type, c.summary, c.tags_json, c.workspace_id
            FROM concept_vectors cv
            JOIN concepts c ON cv.concept_id = c.id
            """)
            rows = cursor.fetchall()

        if not rows:
            self._cached_ids = []
            self._cached_meta = []
            self._cached_matrix = np.empty((0, self.dim), dtype=np.float32)
            self._cached_dims = []
            self._cache_valid = True
            return

        ids = []
        meta = []
        vecs = []
        dims = []
        for r in rows:
            blob = r["vector"]
            dim = r["dim"]
            arr = np.frombuffer(blob, dtype=np.float32)
            ids.append(r["concept_id"])
            dims.append(dim)
            ws = r["workspace_id"] if "workspace_id" in r.keys() else "default"
            meta.append({
                "name": r["name"],
                "type": r["type"],
                "summary": r["summary"] or "",
                "tags": json.loads(r["tags_json"]) if r["tags_json"] else [],
                "workspace": ws or "default"
            })
            vecs.append(arr)

        self._cached_ids = ids
        self._cached_meta = meta
        self._cached_dims = dims
        self._cached_matrix = np.stack(vecs) if vecs else np.empty((0, self.dim), dtype=np.float32)
        self._cache_valid = True

    def find_most_similar_concept(
        self,
        target_vec: np.ndarray,
        exclude_ids: Optional[Any] = None,
        top_k: int = 5,
        workspace: str = "all"
    ) -> List[Tuple[str, float]]:
        """
        Find most similar existing concepts for a given vector.
        Returns list of (concept_id, cosine_similarity_score) sorted descending.
        """
        self._ensure_cache()
        if self._cached_matrix is None or len(self._cached_ids) == 0:
            return []

        ex_set = set(exclude_ids or []) if isinstance(exclude_ids, (list, set, tuple)) else ({exclude_ids} if exclude_ids else set())
        target_dim = len(target_vec)

        # Filter matching dimensions and workspace
        valid_indices = [
            i for i, (cid, dim, m) in enumerate(zip(self._cached_ids, self._cached_dims, self._cached_meta))
            if cid not in ex_set and dim == target_dim and (not workspace or workspace == "all" or m.get("workspace") == workspace)
        ]
        if not valid_indices:
            return []

        sub_matrix = self._cached_matrix[valid_indices]
        scores = np.dot(sub_matrix, target_vec)
        top_local_idx = np.argsort(scores)[::-1][:top_k]

        results = []
        for idx in top_local_idx:
            orig_i = valid_indices[idx]
            results.append((self._cached_ids[orig_i], float(scores[idx])))
        return results

    def search_semantic(self, query: str, top_k: int = 5, workspace: str = "all") -> List[SearchResult]:
        """Sub-millisecond cosine similarity search over concept vectors with workspace filtering."""
        if not query.strip():
            return []

        q_vec, _ = self.embed_text(query)
        self._ensure_cache()

        if self._cached_matrix is None or len(self._cached_ids) == 0:
            return []

        q_dim = len(q_vec)
        valid_indices = [
            i for i, (dim, m) in enumerate(zip(self._cached_dims, self._cached_meta))
            if dim == q_dim and (not workspace or workspace == "all" or m.get("workspace") == workspace)
        ]
        if not valid_indices:
            return []

        sub_matrix = self._cached_matrix[valid_indices]
        scores = np.dot(sub_matrix, q_vec)

        top_local_idx = np.argsort(scores)[::-1][:top_k]

        results = []
        for idx in top_local_idx:
            orig_i = valid_indices[idx]
            cid = self._cached_ids[orig_i]
            m = self._cached_meta[orig_i]
            results.append(SearchResult(
                concept_id=cid,
                name=m["name"],
                type=m["type"],
                summary=m["summary"],
                tags=m["tags"],
                score=round(float(scores[idx]), 4),
                matched_by="vector",
                workspace=m.get("workspace", "default")
            ))
        return results

    def hybrid_search(
        self,
        query: str,
        top_k: int = 5,
        alpha: float = 0.5,
        method: str = "rrf",
        rrf_k: int = 60,
        workspace: str = "all"
    ) -> List[SearchResult]:
        """
        High-performance Hybrid Search combining:
        - Layer 3 Vector Cosine Similarity (Semantic)
        - SQLite FTS5 BM25 (Exact keywords/aliases)
        Optionally filtered by workspace domain.
        """
        candidate_k = max(top_k * 3, 20)
        fts_hits = self.db.search(query, limit=candidate_k, workspace=workspace)
        vec_hits = self.search_semantic(query, top_k=candidate_k, workspace=workspace)

        if not fts_hits and not vec_hits:
            return []

        meta_pool: Dict[str, SearchResult] = {}
        for h in fts_hits:
            meta_pool[h.concept_id] = h
        for v in vec_hits:
            if v.concept_id not in meta_pool:
                meta_pool[v.concept_id] = v

        if method == "rrf":
            rrf_scores: Dict[str, float] = {}

            w_fts = (1.0 - alpha) * 2.0
            for rank, h in enumerate(fts_hits, 1):
                rrf_scores[h.concept_id] = rrf_scores.get(h.concept_id, 0.0) + (w_fts / (rrf_k + rank))

            w_vec = alpha * 2.0
            for rank, v in enumerate(vec_hits, 1):
                rrf_scores[v.concept_id] = rrf_scores.get(v.concept_id, 0.0) + (w_vec / (rrf_k + rank))

            sorted_ids = sorted(rrf_scores.keys(), key=lambda cid: rrf_scores[cid], reverse=True)
            results = []
            for cid in sorted_ids[:top_k]:
                base = meta_pool[cid]
                results.append(SearchResult(
                    concept_id=cid,
                    name=base.name,
                    type=base.type,
                    summary=base.summary,
                    tags=base.tags,
                    score=round(rrf_scores[cid] * 100.0, 4),
                    matched_by="hybrid_rrf",
                    workspace=base.workspace
                ))
            return results

        else:
            combined: Dict[str, Dict[str, Any]] = {}
            fts_scores = [h.score for h in fts_hits]
            max_fts = max(fts_scores, default=1.0)
            min_fts = min(fts_scores, default=0.0)
            fts_range = max_fts - min_fts if max_fts > min_fts else 1.0

            for h in fts_hits:
                norm_fts = (h.score - min_fts) / fts_range if fts_range > 0 else 1.0
                combined[h.concept_id] = {
                    "hit": h,
                    "fts_score": norm_fts,
                    "vec_score": 0.0
                }

            vec_scores = [v.score for v in vec_hits]
            max_vec = max(vec_scores, default=1.0)
            min_vec = min(vec_scores, default=0.0)
            vec_range = max_vec - min_vec if max_vec > min_vec else 1.0

            for v in vec_hits:
                norm_vec = (v.score - min_vec) / vec_range if vec_range > 0 else 1.0
                if v.concept_id in combined:
                    combined[v.concept_id]["vec_score"] = norm_vec
                else:
                    combined[v.concept_id] = {
                        "hit": v,
                        "fts_score": 0.0,
                        "vec_score": norm_vec
                    }

            final_list = []
            for cid, item in combined.items():
                h = item["hit"]
                score = (1.0 - alpha) * item["fts_score"] + alpha * item["vec_score"]
                final_list.append(SearchResult(
                    concept_id=cid,
                    name=h.name,
                    type=h.type,
                    summary=h.summary,
                    tags=h.tags,
                    score=round(score, 4),
                    matched_by="hybrid_linear",
                    workspace=h.workspace
                ))

            final_list.sort(key=lambda x: x.score, reverse=True)
            return final_list[:top_k]

    def sync_workspace_vector(self, workspace_id: str):
        """
        Compute and store the representative semantic centroid vector for a workspace.
        Combines the workspace name + description embedding with the centroid of its member concepts.
        """
        ws = self.db.get_workspace(workspace_id)
        if not ws:
            return

        desc_text = f"{ws.name}\n{ws.description}\n{' '.join(ws.tags)}"
        desc_vec, _ = self.embed_text(desc_text)

        with self.db.get_connection() as conn:
            rows = conn.execute("""
            SELECT cv.vector, cv.dim FROM concept_vectors cv
            JOIN concepts c ON cv.concept_id = c.id
            WHERE c.workspace_id = ?
            """, (workspace_id,)).fetchall()

        concept_vecs = []
        for r in rows:
            if r["dim"] == len(desc_vec):
                concept_vecs.append(np.frombuffer(r["vector"], dtype=np.float32))

        if concept_vecs:
            member_centroid = np.mean(concept_vecs, axis=0)
            member_norm = np.linalg.norm(member_centroid)
            if member_norm > 0:
                member_centroid /= member_norm
            final_vec = 0.5 * desc_vec + 0.5 * member_centroid
        else:
            final_vec = desc_vec

        norm = np.linalg.norm(final_vec)
        if norm > 0:
            final_vec /= norm

        self.db.update_workspace_vector(workspace_id, final_vec.astype(np.float32).tobytes(), len(final_vec))

    def sync_all_workspace_vectors(self):
        """Re-sync centroid vectors for all registered workspaces."""
        workspaces = self.db.list_workspaces()
        for w in workspaces:
            self.sync_workspace_vector(w.id)

    def route_workspaces(self, query: str, top_k: int = 3) -> List[Tuple[str, float]]:
        """
        Rank all workspaces by cosine similarity with the query.
        Returns sorted list of (workspace_id, similarity_score).
        """
        if not query.strip():
            return [("default", 1.0)]

        q_vec, _ = self.embed_text(query)
        ws_list = self.db.get_workspace_vectors()

        # If no workspace vectors yet, auto-sync
        if not ws_list:
            self.sync_all_workspace_vectors()
            ws_list = self.db.get_workspace_vectors()

        if not ws_list:
            return [("default", 1.0)]

        results = []
        for item in ws_list:
            ws_id = item["id"]
            dim = item["dim"]
            if dim != len(q_vec):
                continue
            arr = np.frombuffer(item["vector"], dtype=np.float32)
            sim = float(np.dot(arr, q_vec))
            results.append((ws_id, sim))

        if not results:
            return [("default", 1.0)]

        results.sort(key=lambda x: x[1], reverse=True)
        return results[:top_k]

    def route_query(self, query: str) -> Tuple[Optional[str], float]:
        """
        Semantic Workspace Routing:
        Finds the single best-fitting workspace for a query.
        Returns (best_workspace_id, score).
        If only one workspace exists (e.g. 'default'), returns (None, 1.0) so search remains global.
        """
        all_ws = self.db.list_workspaces()
        if len(all_ws) <= 1:
            return None, 1.0

        ranked = self.route_workspaces(query, top_k=1)
        if not ranked:
            return None, 0.0

        best_id, best_score = ranked[0]
        return best_id, best_score



