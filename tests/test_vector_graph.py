import unittest
import tempfile
import shutil
from pathlib import Path
import numpy as np

from llmwiki.storage.db import Database
from llmwiki.storage.vector_index import VectorIndex, _fnv1a_32
from llmwiki.storage.graph import KnowledgeGraph
from llmwiki.storage.models import Concept, Relation
from llmwiki.storage.store import Store


class TestVectorAndGraph(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="llmwiki_test_"))
        self.db_path = self.temp_dir / "test.db"
        self.db = Database(db_path=self.db_path)
        self.concepts_dir = self.temp_dir / "concepts"
        self.raw_dir = self.temp_dir / "raw"
        self.store = Store(concepts_dir=self.concepts_dir, raw_dir=self.raw_dir, db=self.db)
        self.graph = KnowledgeGraph(db=self.db)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_deterministic_fnv1a(self):
        """Test FNV-1a produces deterministic 32-bit hash regardless of environment."""
        h1 = _fnv1a_32("attention")
        h2 = _fnv1a_32("attention")
        self.assertEqual(h1, h2)
        self.assertIsInstance(h1, int)
        self.assertTrue(0 <= h1 <= 0xFFFFFFFF)

        # Difference for different tokens
        h3 = _fnv1a_32("transformer")
        self.assertNotEqual(h1, h3)

    def test_vector_fallback_embed(self):
        """Test fallback embedding normalization and semantic overlap."""
        vec1 = self.store.vectors._fallback_embed("FlashAttention GPU SRAM memory optimization")
        vec2 = self.store.vectors._fallback_embed("FlashAttention GPU memory IO")
        vec3 = self.store.vectors._fallback_embed("Cooking pasta with tomato sauce")

        norm1 = np.linalg.norm(vec1)
        self.assertAlmostEqual(norm1, 1.0, places=4)

        sim_related = float(np.dot(vec1, vec2))
        sim_unrelated = float(np.dot(vec1, vec3))

        self.assertGreater(sim_related, sim_unrelated)

    def test_rrf_hybrid_search(self):
        """Test hybrid search with RRF fusion."""
        c1 = Concept(
            id="flash_attention",
            name="FlashAttention",
            type="algorithm",
            summary="Fast and memory-efficient exact attention with IO-awareness.",
            tags=["transformer", "gpu", "sram"],
            mechanisms=["Tiling over SRAM", "Online softmax recomputation"]
        )
        c2 = Concept(
            id="paged_attention",
            name="PagedAttention",
            type="algorithm",
            summary="Virtual memory paging for LLM KV cache management.",
            tags=["vllm", "memory", "serving"],
            mechanisms=["Non-contiguous KV paging"]
        )
        self.store.save_concept(c1)
        self.store.save_concept(c2)

        results = self.store.search("attention memory", mode="hybrid", limit=5)
        self.assertTrue(len(results) >= 1)
        # First match should be relevant
        self.assertIn(results[0].concept_id, ["flash_attention", "paged_attention"])
        self.assertEqual(results[0].matched_by, "hybrid_rrf")

    def test_graphrag_and_hipporag(self):
        """Test GraphRAG community detection and HippoRAG personalized PageRank."""
        # Create interconnected concepts
        c_attn = Concept(
            id="attention",
            name="Attention Mechanism",
            type="architecture",
            summary="Key-Value-Query mapping mechanism."
        )
        c_flash = Concept(
            id="flash_attention",
            name="FlashAttention",
            type="algorithm",
            summary="Tiled SRAM IO-aware attention.",
            relations=[Relation(type="improves", target="attention", reason="Reduces HBM IO")]
        )
        c_vllm = Concept(
            id="vllm",
            name="vLLM",
            type="tool",
            summary="High-throughput serving engine with PagedAttention.",
            relations=[Relation(type="requires", target="paged_attention", reason="Core KV backend")]
        )
        c_paged = Concept(
            id="paged_attention",
            name="PagedAttention",
            type="algorithm",
            summary="Paged KV cache management.",
            relations=[Relation(type="variant_of", target="attention", reason="Attention variant")]
        )
        for c in [c_attn, c_flash, c_vllm, c_paged]:
            self.store.save_concept(c)

        self.graph.build_graph()

        # 1. PageRank hubs
        hubs = self.graph.get_central_concepts(top_k=2)
        self.assertTrue(len(hubs) > 0)
        # 'attention' should be highest degree hub
        self.assertEqual(hubs[0]["concept_id"], "attention")

        # 2. HippoRAG Personalized PageRank
        # When querying flash_attention, attention and related nodes should receive probability
        ppr = self.graph.personalized_pagerank({"flash_attention": 1.0})
        self.assertTrue(len(ppr) > 0)
        self.assertGreater(ppr.get("attention", 0.0), 0.0)

        # 3. Community detection (GraphRAG)
        comms = self.graph.detect_communities()
        self.assertTrue(len(comms) >= 1)

        # 4. Shortest reasoning path
        path = self.graph.find_path("flash_attention", "paged_attention")
        self.assertIsNotNone(path)
        self.assertTrue(len(path) >= 2)
        # Should route through 'attention'
        path_nodes = {s["from"] for s in path} | {s["to"] for s in path}
        self.assertIn("attention", path_nodes)

        # 5. Graph metrics
        metrics = self.graph.get_metrics()
        self.assertEqual(metrics["node_count"], 4)
        self.assertGreaterEqual(metrics["edge_count"], 3)

        # 6. HippoRAG search mode
        graph_results = self.store.search("FlashAttention", mode="graph", limit=3)
        self.assertTrue(len(graph_results) >= 1)
        self.assertEqual(graph_results[0].matched_by, "hipporag")


if __name__ == "__main__":
    unittest.main()
