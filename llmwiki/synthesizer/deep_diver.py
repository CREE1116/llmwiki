"""
Autonomous Concept Deep-Diver for LLMWiki.
Performs on-demand web crawling and knowledge expansion for a target concept:
1. Derives high-precision search keywords from concept metadata.
2. Resolves and crawls encyclopedic/technical web pages (Wikipedia, Namuwiki, Docs).
3. Distills new atomic subconcepts, mechanisms, and trade-offs.
4. Automatically weaves two-way graph relations linking back to the origin concept.
"""
from typing import Dict, Any, List, Optional
import urllib.parse
from ..storage.store import Store
from ..storage.models import Concept, Relation
from ..parser.web_parser import WebParser
from .distiller import Distiller


class DeepDiver:
    def __init__(self, store: Optional[Store] = None):
        self.store = store or Store()
        self.distiller = Distiller(store=self.store)

    def deep_dive(
        self,
        concept_id: str,
        depth: int = 1,
        max_pages: int = 5,
        greedy: bool = False,
        extra_query: Optional[str] = None,
        workspace: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Deep-dive into a concept by crawling relevant web knowledge and synthesizing
        new atomic concepts and relations.
        """
        origin = self.store.get_concept(concept_id)
        if not origin:
            return {"error": f"Concept '{concept_id}' not found."}

        target_ws = workspace or origin.workspace or "default"
        # Determine search keywords
        query_parts = [origin.name]
        if extra_query:
            query_parts.append(extra_query)
        elif origin.tags:
            # Filter non-generic tags
            meaningful_tags = [t for t in origin.tags if t not in {"atomic_extracted", "concept", "architecture"}]
            if meaningful_tags:
                query_parts.append(meaningful_tags[0])

        search_query = " ".join(query_parts).strip()

        # 1. Discover seed URLs
        candidate_urls = WebParser.search_candidate_urls(search_query, limit=3)
        if not candidate_urls:
            candidate_urls = [f"https://namu.wiki/w/{urllib.parse.quote(origin.name)}"]

        crawled_docs = []
        visited_urls = set()

        # 2. Crawl candidate sites
        for seed_url in candidate_urls:
            if len(crawled_docs) >= (10000 if greedy else max(1, max_pages)):
                break
            if seed_url in visited_urls:
                continue

            try:
                pages = WebParser.crawl(
                    seed_url,
                    depth=depth,
                    max_pages=max_pages if not greedy else 0,
                    greedy=greedy,
                    same_site=True,
                    timeout=15.0
                )
                for p in pages:
                    if p["source"] not in visited_urls:
                        visited_urls.add(p["source"])
                        crawled_docs.append(p)
                        if not greedy and len(crawled_docs) >= max_pages:
                            break
            except Exception:
                continue

        if not crawled_docs:
            return {
                "concept_id": origin.id,
                "name": origin.name,
                "crawled_pages": 0,
                "new_concepts": [],
                "updated_relations": 0,
                "message": f"No crawlable web documents could be retrieved for '{origin.name}'."
            }

        new_concepts: List[Concept] = []
        new_relation_count = 0

        # 3. Distill and link concepts
        for doc in crawled_docs:
            try:
                distilled = self.distiller.distill(
                    doc_title=doc["title"],
                    text=doc["text"],
                    source=doc["source"]
                )
                for c in distilled:
                    c.workspace = target_ws
                    # Automatically link each distilled concept to origin concept if not origin itself
                    if c.id != origin.id:
                        # Add directional link to origin
                        c.relations.append(Relation(
                            target=origin.id,
                            type="related_to",
                            reason=f"Deep-dive web extraction from '{doc['title'][:40]}'"
                        ))
                        saved_c, action = self.store.save_concept_with_dedup(c)
                        new_concepts.append(saved_c)

                        # Also add reverse edge in graph
                        self.store.db.add_relation(
                            source_id=origin.id,
                            relation_type="expands",
                            target_id=saved_c.id,
                            reason="Autonomous deep dive expansion"
                        )
                        new_relation_count += 1

                # Add source URI to origin concept sources
                if doc["source"] not in origin.sources:
                    origin.sources.append(doc["source"])

            except Exception:
                continue

        # Re-save updated origin concept
        self.store.save_concept(origin)

        # Sync workspace representative vector
        try:
            self.store.vectors.sync_workspace_vector(target_ws)
        except Exception:
            pass

        return {
            "concept_id": origin.id,
            "name": origin.name,
            "workspace": target_ws,
            "crawled_pages": len(crawled_docs),
            "sources": [d["source"] for d in crawled_docs],
            "new_concepts": [
                {
                    "id": c.id,
                    "name": c.name,
                    "type": c.type,
                    "summary": c.summary
                }
                for c in new_concepts
            ],
            "new_relations": new_relation_count,
            "message": f"Deep dive complete: Crawled {len(crawled_docs)} pages, synthesized {len(new_concepts)} concept(s) and {new_relation_count} graph relations."
        }
