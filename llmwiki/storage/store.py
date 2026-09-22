"""
Integrated 3-Layer Knowledge Store:
- Layer 1: RawStore (Full source text archive)
- Layer 2: Markdown store + SQLite FTS5 index (Atomic concepts)
- Layer 3: VectorIndex (Semantic vectors + hybrid search)
"""
import re
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple
from .models import Concept, SearchResult
from .db import Database
from .raw_store import RawStore
from .vector_index import VectorIndex
from ..config import CONCEPTS_DIR, RAW_DIR

class Store:
    def __init__(
        self,
        concepts_dir: Path = CONCEPTS_DIR,
        raw_dir: Path = RAW_DIR,
        db: Optional[Database] = None
    ):
        self.concepts_dir = concepts_dir
        self.concepts_dir.mkdir(parents=True, exist_ok=True)
        self.db = db or Database()
        self.raw = RawStore(raw_dir=raw_dir, db=self.db)
        self.vectors = VectorIndex(db=self.db)

    def _file_path(self, concept_id: str) -> Path:
        clean_id = concept_id.strip().lower().replace(" ", "_").replace("/", "_")
        return self.concepts_dir / f"{clean_id}.md"

    def save_concept(self, concept: Concept) -> Path:
        """
        Write concept to Layer 2 (markdown + SQLite FTS5)
        and Layer 3 (Vector semantic index).
        """
        path = self._file_path(concept.id)
        md_content = concept.to_markdown()
        path.write_text(md_content, encoding="utf-8")

        # Sync to SQLite (FTS5 & Graph edges)
        self.db.upsert_concept(concept, md_content)

        # Sync to Layer 3 Vector Index
        try:
            self.vectors.index_concept(concept)
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

    def list_concepts(self, limit: int = 50) -> List[Dict[str, Any]]:
        """List concepts with their metadata."""
        ids = self.db.list_all_ids()[:limit]
        results = []
        for cid in ids:
            c = self.get_concept(cid)
            if c:
                results.append({
                    "id": c.id,
                    "name": c.name,
                    "type": c.type,
                    "tags": c.tags,
                    "summary": c.summary
                })
        return results

    def search(self, query: str, mode: str = "hybrid", limit: int = 5) -> List[SearchResult]:
        """
        Search across knowledge warehouse:
        - mode='hybrid': Layer 3 Vector + SQLite FTS5
        - mode='vector': Layer 3 Vector semantic search
        - mode='keyword': SQLite FTS5 BM25 search
        """
        if mode in ("vector", "semantic"):
            return self.vectors.search_semantic(query, top_k=limit)
        elif mode in ("keyword", "fts"):
            return self.db.search(query, limit=limit)
        else: # default hybrid
            return self.vectors.hybrid_search(query, top_k=limit)

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
        return count
