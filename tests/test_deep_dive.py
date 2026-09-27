import unittest
import tempfile
import shutil
from pathlib import Path
from unittest.mock import patch, MagicMock
from llmwiki.storage.db import Database
from llmwiki.storage.models import Concept
from llmwiki.storage.store import Store
from llmwiki.parser.web_parser import WebParser
from llmwiki.synthesizer.deep_diver import DeepDiver


class TestDeepDiveAndGreedy(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = Path(tempfile.mkdtemp())
        self.concepts_dir = self.tmp_dir / "concepts"
        self.raw_dir = self.tmp_dir / "raw"
        self.db_path = self.tmp_dir / "test.db"

        self.db = Database(db_path=self.db_path)
        self.store = Store(concepts_dir=self.concepts_dir, raw_dir=self.raw_dir, db=self.db)

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_search_candidate_urls(self):
        urls = WebParser.search_candidate_urls("FlashAttention", limit=2)
        self.assertGreaterEqual(len(urls), 1)
        self.assertTrue(any("FlashAttention" in u or "flashattention" in u.lower() for u in urls))

    def test_greedy_crawl_parameters(self):
        # Verify greedy mode relaxes clamps and sets generous quotas
        with patch.object(WebParser, "parse") as mock_parse:
            mock_parse.return_value = {
                "title": "Root Test Page",
                "text": "This is root content mentioning FlashAttention and Transformers.",
                "source": "https://example.com/root",
                "source_type": "web",
                "links": [
                    {"url": "https://example.com/sub1", "text": "Subpage 1 FlashAttention"},
                    {"url": "https://example.com/sub2", "text": "Subpage 2 Transformers"},
                ]
            }
            # Greedy mode with max_pages=0
            pages = WebParser.crawl("https://example.com/root", depth=2, max_pages=0, greedy=True)
            self.assertGreaterEqual(len(pages), 1)
            self.assertEqual(pages[0]["source"], "https://example.com/root")

    def test_autonomous_deep_dive_and_graph_expansion(self):
        store = self.store
        diver = DeepDiver(store=store)

        # 1. Seed origin concept
        origin = Concept(
            id="transformer",
            name="Transformer Architecture",
            type="architecture",
            summary="Self-attention based neural network architecture for sequence transduction.",
            tags=["nlp", "attention", "deep_learning"]
        )
        store.save_concept(origin)

        # 2. Mock web search & parse responses for deterministic test
        mock_crawled_pages = [
            {
                "title": "FlashAttention: Fast and Memory-Efficient Exact Attention",
                "text": "# FlashAttention\n\nFlashAttention is an exact attention algorithm that uses tiling to reduce memory reads/writes between GPU HBM and SRAM.\n\n## Mechanisms\nTiling for SRAM blocks and forward pass recomputation.",
                "source": "https://example.com/flashattention",
                "source_type": "web",
                "crawl_depth": 1,
                "parent_url": "https://example.com/transformer",
                "anchor_text": "FlashAttention"
            }
        ]

        with patch.object(WebParser, "search_candidate_urls", return_value=["https://example.com/flashattention"]):
            with patch.object(WebParser, "crawl", return_value=mock_crawled_pages):
                result = diver.deep_dive("transformer", depth=1, max_pages=3)

        self.assertEqual(result["concept_id"], "transformer")
        self.assertEqual(result["crawled_pages"], 1)
        self.assertGreaterEqual(result["new_relations"], 1)

        # Verify origin concept now has the new source
        updated_origin = store.get_concept("transformer")
        self.assertIn("https://example.com/flashattention", updated_origin.sources)

        # Verify new concepts are saved and linked
        edges = store.db.get_full_graph_data()["edges"]
        has_expansion_edge = any(
            (e["source"] == "transformer" and e["relation"] == "expands") or
            (e["target"] == "transformer" and e["relation"] == "related_to")
            for e in edges
        )
        self.assertTrue(has_expansion_edge)


if __name__ == "__main__":
    unittest.main()
