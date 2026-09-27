import unittest
import tempfile
import shutil
from pathlib import Path
from llmwiki.storage.db import Database
from llmwiki.storage.models import Concept, Workspace
from llmwiki.storage.store import Store

class TestWorkspace(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = Path(tempfile.mkdtemp())
        self.concepts_dir = self.tmp_dir / "concepts"
        self.raw_dir = self.tmp_dir / "raw"
        self.db_path = self.tmp_dir / "test.db"

        self.db = Database(db_path=self.db_path)
        self.store = Store(concepts_dir=self.concepts_dir, raw_dir=self.raw_dir, db=self.db)

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_workspace_crud(self):
        store = self.store
        # Initial default workspace exists
        workspaces = store.list_workspaces()
        self.assertGreaterEqual(len(workspaces), 1)
        self.assertTrue(any(w.id == "default" for w in workspaces))

        # Create new workspace
        ws_bio = store.create_workspace("bio_med", name="Biomedical Science", description="Genomics, protein structures, and molecular biology")
        self.assertEqual(ws_bio.id, "bio_med")
        self.assertEqual(ws_bio.name, "Biomedical Science")

        # Fetch
        fetched = store.get_workspace("bio_med")
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched.description, "Genomics, protein structures, and molecular biology")

        # List
        all_ws = store.list_workspaces()
        self.assertEqual(len(all_ws), 2)

        # Delete
        deleted = store.delete_workspace("bio_med")
        self.assertTrue(deleted)
        self.assertIsNone(store.get_workspace("bio_med"))

    def test_workspace_scoped_storage_and_search(self):
        store = self.store

        # Create two isolated workspaces
        store.create_workspace("ai_systems", name="AI Systems", description="Transformers, FlashAttention, GPU kernels, and KV cache")
        store.create_workspace("bio_med", name="Biomedical Science", description="Genetics, CRISPR, mRNA transcription, and AlphaFold proteins")

        # Add concept to ai_systems
        c1 = Concept(
            id="flash_attn_v2",
            name="FlashAttention-2",
            type="architecture",
            workspace="ai_systems",
            summary="Faster GPU IO-aware exact attention tiling and forward pass optimization",
            tags=["gpu", "attention", "transformer"]
        )
        store.save_concept(c1)

        # Add concept to bio_med
        c2 = Concept(
            id="alphafold_multimer",
            name="AlphaFold-Multimer",
            type="architecture",
            workspace="bio_med",
            summary="Deep learning model for protein complex structural prediction and folding",
            tags=["protein", "biology", "alphafold"]
        )
        store.save_concept(c2)

        # List by workspace
        ai_concepts = store.list_concepts(workspace="ai_systems")
        self.assertEqual(len(ai_concepts), 1)
        self.assertEqual(ai_concepts[0]["id"], "flash_attn_v2")

        bio_concepts = store.list_concepts(workspace="bio_med")
        self.assertEqual(len(bio_concepts), 1)
        self.assertEqual(bio_concepts[0]["id"], "alphafold_multimer")

        # Search with explicit workspace scoping
        ai_hits = store.search("attention", mode="hybrid", workspace="ai_systems")
        self.assertGreaterEqual(len(ai_hits), 1)
        self.assertEqual(ai_hits[0].concept_id, "flash_attn_v2")
        self.assertEqual(ai_hits[0].workspace, "ai_systems")

        # Searching bio_med for attention returns nothing in that workspace
        bio_hits_for_ai = store.search("attention", mode="keyword", workspace="bio_med")
        self.assertEqual(len(bio_hits_for_ai), 0)

        # Search all workspaces
        all_hits = store.search("attention", mode="hybrid", workspace="all")
        self.assertTrue(any(h.concept_id == "flash_attn_v2" for h in all_hits))

    def test_semantic_workspace_routing(self):
        store = self.store

        store.create_workspace("ai_systems", name="AI Systems", description="Deep learning, neural networks, CUDA kernels, and LLM inference")
        store.create_workspace("bio_med", name="Biomedical", description="Genomics, DNA sequencing, cellular immunology, and mRNA biology")

        c1 = Concept(
            id="cuda_kernel_fusion",
            name="CUDA Kernel Fusion",
            type="algorithm",
            workspace="ai_systems",
            summary="Combining multiple GPU kernel operations into a single kernel to eliminate HBM bandwidth round trips",
            tags=["cuda", "gpu", "kernel"]
        )
        store.save_concept(c1)

        c2 = Concept(
            id="crispr_cas9_editing",
            name="CRISPR Cas9 Gene Editing",
            type="mechanism",
            workspace="bio_med",
            summary="Targeted endonuclease genomic sequence cutting and DNA repair system",
            tags=["gene", "dna", "crispr"]
        )
        store.save_concept(c2)

        # Sync workspace vectors
        store.vectors.sync_all_workspace_vectors()

        # Route AI query
        ai_route, ai_score = store.route_workspace("GPU memory bandwidth optimization CUDA")
        self.assertEqual(ai_route, "ai_systems")
        self.assertGreater(ai_score, 0.0)

        # Route Bio query
        bio_route, bio_score = store.route_workspace("Genomic DNA repair CRISPR sequence")
        self.assertEqual(bio_route, "bio_med")
        self.assertGreater(bio_score, 0.0)

        # Auto search should route query to respective workspace
        auto_hits_ai = store.search("CUDA kernel fusion", mode="hybrid", workspace="auto")
        self.assertGreaterEqual(len(auto_hits_ai), 1)
        self.assertEqual(auto_hits_ai[0].workspace, "ai_systems")

if __name__ == "__main__":
    unittest.main()
