---
name: llmwiki
description: >-
  Personal 3-tier local knowledge warehouse (Layer 1 Raw -> Layer 2 Distilled Concepts -> Layer 3 Vector index).
  Use whenever searching, retrieving, cross-referencing, traversing, or ingesting technical papers, architectures,
  algorithms, formulas, or web URLs into the local knowledge warehouse. Runs instantly via lightweight CLI with zero daemons.
---

# LLMWiki Skill: Local 3-Tier Knowledge Warehouse

This skill enables the agent to search, query, traverse, and ingest knowledge from the user's private local 3-tier knowledge warehouse without any background servers.

Base project path: `/Users/leejongmin/llmwiki`

---

## 1. Quick Decision Matrix

| What you want to do | Recommended Command |
|---|---|
| Search concepts (Hybrid RRF fusion) | `python3 -m llmwiki search "<query>" --limit 5` |
| Multi-hop associative search (HippoRAG) | `python3 -m llmwiki search "<query>" --mode graph` |
| Deep semantic search (Vector cosine) | `python3 -m llmwiki search "<query>" --mode semantic` |
| Exact keyword / FTS5 search | `python3 -m llmwiki search "<query>" --mode keyword` |
| Read distilled concept card & relations | `python3 -m llmwiki get <concept_id> --neighbors` |
| Drill down to Layer 1 ground-truth document | `python3 -m llmwiki get <concept_id> --raw` or `python3 -m llmwiki raw <doc_id>` |
| Traverse 1-hop graph connections | `python3 -m llmwiki graph <concept_id> --hops 1` |
| Find reasoning path between 2 concepts | `python3 -m llmwiki graph <start_id> --path <target_id>` |
| Detect thematic clusters (GraphRAG) | `python3 -m llmwiki graph --communities` |
| Find core hub concepts (PageRank) | `python3 -m llmwiki graph --pagerank` |
| Find knowledge bridge concepts | `python3 -m llmwiki graph --bridges` |
| View LLM retrieval audit logs | `python3 -m llmwiki logs --limit 20` |
| Ingest a new paper (PDF), file, or web URL | `python3 -m llmwiki ingest <file_path_or_url>` |
| Check warehouse & graph topology statistics | `python3 -m llmwiki stats` |
| Launch Desktop App (Graph & Logs UI) | `/Users/leejongmin/llmwiki/bin/llmwiki-app` |


> [!TIP]
> Add `--json` to any command (e.g. `python3 -m llmwiki search "..." --json`) to get structured JSON output suitable for programmatic processing.

---

## 2. Typical Workflows

### A. Answering Questions Using Local Knowledge
1. **Search**: Run `python3 -m llmwiki search "<user query>" --limit 3`.
2. **Inspect**: If a match looks promising, run `python3 -m llmwiki get <concept_id> --neighbors`.
3. **Verify (Optional)**: If exact math proofs, benchmark values, or raw code are needed, append `--raw` to inspect Layer 1 ground truth.
4. **Answer**: Synthesize the answer using the high-density facts extracted.

### B. Ingesting New Knowledge
When the user shares a paper PDF, technical markdown file, or website link:
```bash
python3 -m llmwiki ingest <path_or_url>
```
The local engine will automatically:
1. Archive full text into **Layer 1 (RawStore)**.
2. Distill atomic concepts with summary, mechanisms, trade-offs, and relations into **Layer 2 (Markdown + SQLite FTS5)**.
3. Synchronize vector embeddings into **Layer 3 (VectorIndex)**.
