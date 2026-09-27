"""
Integrated 3-Layer Knowledge Store:
- Layer 1: RawStore (Full source text archive)
- Layer 2: Markdown store + SQLite FTS5 index (Atomic concepts)
- Layer 3: VectorIndex (Semantic vectors + hybrid search)
"""
import re
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple
from .models import Concept, SearchResult, Workspace
from .db import Database
from .raw_store import RawStore
from .vector_index import VectorIndex
from ..config import CONCEPTS_DIR, RAW_DIR, get_active_workspace

class Store:
    def __init__(
        self,
        concepts_dir: Path = CONCEPTS_DIR,
        raw_dir: Path = RAW_DIR,
        db: Optional[Database] = None,
        workspace: Optional[str] = None
    ):
        self.concepts_dir = concepts_dir
        self.concepts_dir.mkdir(parents=True, exist_ok=True)
        self.db = db or Database()
        self.raw = RawStore(raw_dir=raw_dir, db=self.db)
        self.vectors = VectorIndex(db=self.db)
        self.workspace = workspace if workspace is not None else get_active_workspace()


    def _file_path(self, concept_id: str) -> Path:
        clean_id = concept_id.strip().lower().replace(" ", "_").replace("/", "_")
        return self.concepts_dir / f"{clean_id}.md"

    def save_concept(self, concept: Concept) -> Path:
        """
        Write concept to Layer 2 (markdown + SQLite FTS5)
        and Layer 3 (Vector semantic index).
        """
        if not concept.workspace:
            concept.workspace = self.workspace or "default"

        path = self._file_path(concept.id)
        md_content = concept.to_markdown()
        path.write_text(md_content, encoding="utf-8")

        # Sync to SQLite (FTS5 & Graph edges)
        self.db.upsert_concept(concept, md_content)

        # Sync to Layer 3 Vector Index
        try:
            self.vectors.index_concept(concept)
            self.vectors.sync_workspace_vector(concept.workspace)
        except Exception as e:
            # Vector indexing should not block concept save
            import sys
            print(f"Warning: Vector indexing failed for {concept.id}: {e}", file=sys.stderr)

        return path

    def save_concept_with_dedup(
        self,
        concept: Concept,
        merge_threshold: float = 0.90,
        link_threshold: float = 0.55,
        exclude_ids: Optional[Any] = None
    ) -> Tuple[Concept, str]:
        """
        Save concept with Vector-similarity based deduplication/merging and automatic linking:
        1. If concept exists by exact ID or very high similarity + compatible name:
           -> MERGE: Enrich existing concept with new sources, mechanisms, tags, and relations.
        2. If similarity is in [link_threshold, merge_threshold) or different named related concept:
           -> LINK: Save as new concept AND create 'related_to' edge to similar concepts.
        3. If similarity < link_threshold:
           -> NEW: Save as independent concept.
        Returns (final_concept, action_taken) where action_taken is 'merged', 'linked', or 'created'.
        """
        if not concept.workspace:
            concept.workspace = self.workspace or "default"

        # Exact ID match -> merge into existing
        existing = self.get_concept(concept.id)
        if existing:
            existing.sources = list(dict.fromkeys(existing.sources + concept.sources))
            existing.tags = list(dict.fromkeys(existing.tags + concept.tags))
            for m in concept.mechanisms:
                if m not in existing.mechanisms:
                    existing.mechanisms.append(m)
            if not existing.summary and concept.summary:
                existing.summary = concept.summary
            for r in concept.relations:
                if r.target != existing.id and not any(er.target == r.target and er.type == r.type for er in existing.relations):
                    existing.relations.append(r)
            if concept.workspace and existing.workspace == "default":
                existing.workspace = concept.workspace
            self.save_concept(existing)
            return existing, "merged"

        # Check semantic similarity against all registered concepts
        ex_set = set(exclude_ids or []) if isinstance(exclude_ids, (list, set, tuple)) else ({exclude_ids} if exclude_ids else set())
        ex_set.add(concept.id)

        components = [concept.name, " ".join(concept.tags), concept.summary, " ".join(concept.mechanisms)]
        text_repr = "\n".join(c for c in components if c)
        vec, _ = self.vectors.embed_text(text_repr)
        similars = self.vectors.find_most_similar_concept(vec, exclude_ids=ex_set, top_k=3)

        if similars:
            best_id, best_score = similars[0]
            target_concept = self.get_concept(best_id)

            # Name compatibility check: identical root or high lexical overlap
            name1 = re.sub(r"\W+", "", concept.name.lower())
            name2 = re.sub(r"\W+", "", target_concept.name.lower()) if target_concept else ""
            is_name_match = name1 and name2 and (name1 in name2 or name2 in name1)

            if target_concept and (best_score >= 0.95 or (best_score >= merge_threshold and is_name_match)):
                target_concept.sources = list(dict.fromkeys(target_concept.sources + concept.sources))
                target_concept.tags = list(dict.fromkeys(target_concept.tags + concept.tags))
                for m in concept.mechanisms:
                    if m not in target_concept.mechanisms:
                        target_concept.mechanisms.append(m)
                if not target_concept.summary and concept.summary:
                    target_concept.summary = concept.summary
                for r in concept.relations:
                    if r.target != target_concept.id and not any(er.target == r.target and er.type == r.type for er in target_concept.relations):
                        target_concept.relations.append(r)
                if concept.workspace and target_concept.workspace == "default":
                    target_concept.workspace = concept.workspace
                self.save_concept(target_concept)
                return target_concept, "merged"

            elif best_score >= link_threshold:
                self.save_concept(concept)
                for sim_id, score in similars:
                    if score >= link_threshold:
                        self.db.add_relation(
                            source_id=concept.id,
                            relation_type="related_to",
                            target_id=sim_id,
                            reason=f"벡터 유사도 {score:.2f} 기반 연관 지식"
                        )
                return concept, "linked"

        self.save_concept(concept)
        return concept, "created"

    def get_concept(self, concept_id: str) -> Optional[Concept]:
        """Load concept from Layer 2 markdown file."""
        path = self._file_path(concept_id)
        if not path.exists():
            return None
        content = path.read_text(encoding="utf-8")
        return Concept.from_markdown(content)

    def get_markdown(self, concept_id: str) -> Optional[str]:
        """Get raw markdown for a concept."""
        path = self._file_path(concept_id)
        if not path.exists():
            return None
        return path.read_text(encoding="utf-8")

    def exists(self, concept_id: str) -> bool:
        return self._file_path(concept_id).exists()

    def list_concepts(self, limit: int = 50, workspace: Optional[str] = None) -> List[Dict[str, Any]]:
        """List concepts with their metadata, optionally filtered by workspace."""
        target_ws = workspace if workspace is not None else self.workspace
        filter_ws = None if target_ws in ("all", "*") else target_ws
        ids = self.db.list_all_ids(workspace=filter_ws)[:limit]
        results = []
        for cid in ids:
            c = self.get_concept(cid)
            if c:
                results.append({
                    "id": c.id,
                    "name": c.name,
                    "type": c.type,
                    "tags": c.tags,
                    "summary": c.summary,
                    "workspace": c.workspace
                })
        return results

    def search(
        self,
        query: str,
        mode: str = "hybrid",
        limit: int = 5,
        workspace: Optional[str] = "auto"
    ) -> List[SearchResult]:
        """
        Search across knowledge warehouse:
        - mode='hybrid': Layer 3 Vector + SQLite FTS5 (RRF fusion)
        - mode='graph' / 'hipporag': HippoRAG Personalized PageRank spreading activation over hybrid seeds
        - mode='vector' / 'semantic': Layer 3 Vector semantic search
        - mode='keyword' / 'fts': SQLite FTS5 BM25 search

        Workspace options:
        - 'auto': Automatically route query to most relevant workspace via vector similarity
        - 'all' / None: Search across all workspaces without filtering
        - '<workspace_id>': Scope search to specific workspace
        """
        ws_filter: Optional[str] = None
        if workspace == "auto":
            routed_ws, route_score = self.vectors.route_query(query)
            if routed_ws:
                ws_filter = routed_ws
        elif workspace in ("all", "*", None):
            ws_filter = None
        else:
            ws_filter = workspace

        if mode in ("vector", "semantic"):
            return self.vectors.search_semantic(query, top_k=limit, workspace=ws_filter)
        elif mode in ("keyword", "fts"):
            return self.db.search(query, limit=limit, workspace=ws_filter)
        elif mode in ("graph", "hipporag", "network"):
            # HippoRAG: Multi-hop graph associative retrieval with workspace scoping
            from .graph import KnowledgeGraph
            initial_hits = self.vectors.hybrid_search(query, top_k=max(limit * 2, 8), workspace=ws_filter)
            if not initial_hits:
                return []

            kg = KnowledgeGraph(db=self.db)
            seed_weights = {h.concept_id: max(0.1, h.score) for h in initial_hits}
            ppr_scores = kg.personalized_pagerank(seed_weights, alpha=0.85)

            # Pool metadata for all known concepts
            meta_map = {h.concept_id: h for h in initial_hits}

            # Combine initial semantic relevance with graph topological relevance
            augmented = []
            max_ppr = max(ppr_scores.values(), default=1.0) or 1.0

            for cid, ppr in ppr_scores.items():
                if ppr <= 0:
                    continue
                norm_ppr = ppr / max_ppr
                seed_score = seed_weights.get(cid, 0.0)
                final_score = 0.6 * seed_score + 0.4 * (norm_ppr * 100.0)

                if cid in meta_map:
                    base = meta_map[cid]
                else:
                    c = self.get_concept(cid)
                    if not c:
                        continue
                    if ws_filter and c.workspace != ws_filter:
                        continue
                    base = SearchResult(
                        concept_id=c.id,
                        name=c.name,
                        type=c.type,
                        summary=c.summary,
                        tags=c.tags,
                        score=0.0,
                        matched_by="graph_ppr",
                        workspace=c.workspace
                    )

                augmented.append(SearchResult(
                    concept_id=cid,
                    name=base.name,
                    type=base.type,
                    summary=base.summary,
                    tags=base.tags,
                    score=round(final_score, 4),
                    matched_by="hipporag",
                    workspace=base.workspace
                ))

            augmented.sort(key=lambda x: x.score, reverse=True)
            return augmented[:limit]
        else:  # default hybrid
            return self.vectors.hybrid_search(query, top_k=limit, workspace=ws_filter)

    def create_workspace(self, workspace_id: str, name: str = "", description: str = "") -> Workspace:
        """Create a new knowledge workspace."""
        ws = self.db.create_workspace(workspace_id, name=name, description=description)
        try:
            self.vectors.sync_workspace_vector(ws.id)
        except Exception:
            pass
        return ws

    def get_workspace(self, workspace_id: str) -> Optional[Workspace]:
        """Get workspace by ID."""
        return self.db.get_workspace(workspace_id)

    def list_workspaces(self) -> List[Workspace]:
        """List all workspaces with metadata."""
        return self.db.list_workspaces()

    def delete_workspace(self, workspace_id: str) -> bool:
        """Delete a workspace."""
        return self.db.delete_workspace(workspace_id)

    def route_workspace(self, query: str) -> Tuple[Optional[str], float]:
        """Find the best-fitting workspace for a query using vector similarity."""
        return self.vectors.route_query(query)



    def get_raw_document(self, doc_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve Layer 1 raw document."""
        return self.raw.get(doc_id)

    def delete_raw_document(self, doc_id: str) -> bool:
        """
        Delete a Layer 1 raw document and clean up references in Layer 2 concepts.
        """
        success = self.raw.delete(doc_id)
        if not success:
            return False

        # Clean up references in concept files
        raw_tag = f"raw:{doc_id}"
        for md_file in self.concepts_dir.glob("*.md"):
            try:
                content = md_file.read_text(encoding="utf-8")
                if raw_tag in content:
                    concept = Concept.from_markdown(content)
                    if raw_tag in concept.sources:
                        concept.sources = [s for s in concept.sources if s != raw_tag]
                        self.save_concept(concept)
            except Exception:
                pass
        return True

    def delete_concept(self, concept_id: str) -> bool:
        """
        Delete a Layer 2 concept, its indexes, and incoming links stored in
        neighboring concept files so a later reindex cannot resurrect them.
        """
        concept = self.get_concept(concept_id)
        if not concept:
            return False

        path = self._file_path(concept_id)
        if path.exists():
            path.unlink()

        # Remove inbound relation declarations from neighboring markdown files.
        for md_file in self.concepts_dir.glob("*.md"):
            try:
                content = md_file.read_text(encoding="utf-8")
                other = Concept.from_markdown(content)
                filtered = [r for r in other.relations if r.target != concept_id]
                if len(filtered) != len(other.relations):
                    other.relations = filtered
                    new_content = other.to_markdown()
                    md_file.write_text(new_content, encoding="utf-8")
                    self.db.upsert_concept(other, new_content)
            except Exception:
                pass

        return self.db.delete_concept(concept_id)

    def reindex_all(self) -> int:
        """Re-index all markdown files into Layer 2 and Layer 3."""
        count = 0
        valid_ids = []
        for md_file in self.concepts_dir.glob("*.md"):
            try:
                content = md_file.read_text(encoding="utf-8")
                concept = Concept.from_markdown(content)
                valid_ids.append(concept.id)
                self.db.upsert_concept(concept, content)
                self.vectors.index_concept(concept)
                count += 1
            except Exception as e:
                print(f"Warning: Failed to index {md_file}: {e}")
        self.db.prune_concepts(valid_ids)
        try:
            self.vectors.sync_all_workspace_vectors()
        except Exception as e:
            print(f"Warning: Failed to sync workspace vectors: {e}")
        return count

