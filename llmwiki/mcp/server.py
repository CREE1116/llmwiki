"""
Model Context Protocol (MCP) Server for LLMWiki.
Implements the JSON-RPC 2.0 stdio specification.
Allows any AI agent (Antigravity, Claude Code, Cursor, etc.) to query,
traverse, and ingest knowledge in sub-millisecond local time.
"""
import sys
import json
import traceback
from typing import Dict, Any, List, Optional
from ..storage.store import Store
from ..storage.graph import KnowledgeGraph
from ..parser import parse_source
from ..synthesizer.distiller import Distiller

class MCPServer:
    def __init__(self):
        self.store = Store()
        self.graph = KnowledgeGraph(db=self.store.db)
        self.distiller = Distiller(store=self.store)

    def get_tool_definitions(self) -> List[Dict[str, Any]]:
        return [
            {
                "name": "wiki_search",
                "description": "Fast search across the local LLMWiki knowledge warehouse. Returns concise concept IDs, types, and dense summaries.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search query keywords or semantic concept"},
                        "mode": {
                            "type": "string",
                            "enum": ["hybrid", "semantic", "keyword"],
                            "default": "hybrid",
                            "description": "Search mode: hybrid (semantic vector + FTS5), semantic (vector only), keyword (BM25 only)"
                        },
                        "limit": {"type": "integer", "default": 5, "description": "Max results to return"}
                    },
                    "required": ["query"]
                }
            },
            {
                "name": "wiki_fetch",
                "description": "Fetch the high-density Layer 2 concept card by its ID, with optional 1-hop relation links and Layer 1 raw document excerpts.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "concept_id": {"type": "string", "description": "ID of the concept (e.g. 'flash_attention')"},
                        "include_neighbors": {"type": "boolean", "default": True, "description": "Include 1-hop graph relations"},
                        "include_raw": {"type": "boolean", "default": False, "description": "Include original Layer 1 raw document text"}
                    },
                    "required": ["concept_id"]
                }
            },
            {
                "name": "wiki_read_raw",
                "description": "Retrieve the exact Layer 1 ground-truth raw source document (for verifications, mathematical proofs, or raw code).",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "doc_id": {"type": "string", "description": "ID of the raw document (e.g. 'doc_abc123' or from concept.sources)"}
                    },
                    "required": ["doc_id"]
                }
            },
            {
                "name": "wiki_traverse",
                "description": "Traverse knowledge graph relations from a starting concept to discover related architectures, algorithms, or tradeoffs.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "concept_id": {"type": "string", "description": "Starting concept ID"},
                        "hops": {"type": "integer", "default": 1, "description": "Search depth (1 or 2)"}
                    },
                    "required": ["concept_id"]
                }
            },
            {
                "name": "wiki_list_concepts",
                "description": "List all distilled concepts currently in the warehouse.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "limit": {"type": "integer", "default": 30, "description": "Maximum concepts to list"}
                    }
                }
            },
            {
                "name": "wiki_ingest",
                "description": "Ingest a new file (PDF, Markdown, TXT) or web URL into the 3-tier knowledge warehouse.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "source": {"type": "string", "description": "File path or HTTP/HTTPS URL"}
                    },
                    "required": ["source"]
                }
            },
            {
                "name": "wiki_stats",
                "description": "Return summary statistics of the knowledge warehouse (concept count, relation count, raw doc count).",
                "inputSchema": {
                    "type": "object",
                    "properties": {}
                }
            }
        ]

    def call_tool(self, name: str, args: Dict[str, Any]) -> str:
        """Route tool invocation to appropriate handler."""
        if name == "wiki_search":
            query = args.get("query", "")
            mode = args.get("mode", "hybrid")
            limit = args.get("limit", 5)
            hits = self.store.search(query=query, mode=mode, limit=limit)
            if not hits:
                return f"No concepts found matching '{query}'."

            output = [f"Found {len(hits)} matching concepts:"]
            for h in hits:
                tags = f"[{', '.join(h.tags)}]" if h.tags else ""
                output.append(f"\n- **ID**: `{h.concept_id}` ({h.name}) | Type: `{h.type}` {tags} [Score: {h.score}]")
                output.append(f"  Summary: {h.summary}")
            return "\n".join(output)

        elif name == "wiki_fetch":
            cid = args.get("concept_id", "").strip()
            concept = self.store.get_concept(cid)
            if not concept:
                return f"Error: Concept '{cid}' not found in warehouse."

            md = concept.to_markdown()
            extra = []

            if args.get("include_neighbors", True):
                neighbors = self.graph.get_neighbors(cid)
                if neighbors["connections"]:
                    extra.append("\n### Graph Connections")
                    for conn in neighbors["connections"]:
                        dir_arrow = "->" if conn["direction"] == "outgoing" else "<-"
                        rel = conn["relation"]
                        target = conn.get("target_id") or conn.get("source_id")
                        reason = f" ({conn['reason']})" if conn.get("reason") else ""
                        extra.append(f"- `{cid}` {dir_arrow} `[[{target}]]` [{rel}]{reason}")

            if args.get("include_raw", False):
                raw_ids = [s.replace("raw:", "") for s in concept.sources if s.startswith("raw:")]
                for rid in raw_ids:
                    raw_doc = self.store.raw.get(rid)
                    if raw_doc:
                        snippet = raw_doc["content"][:2000]
                        extra.append(f"\n### Raw Document Excerpt ({raw_doc['title']})\n```text\n{snippet}...\n```")

            return md + ("\n".join(extra) if extra else "")

        elif name == "wiki_read_raw":
            doc_id = args.get("doc_id", "").strip().replace("raw:", "")
            raw_doc = self.store.raw.get(doc_id)
            if not raw_doc:
                return f"Error: Raw document '{doc_id}' not found."
            return f"# Raw Document: {raw_doc['title']}\nSource: {raw_doc['source_uri']}\nLength: {raw_doc['char_count']} chars\n\n{raw_doc['content']}"

        elif name == "wiki_traverse":
            cid = args.get("concept_id", "").strip()
            neighbors = self.graph.get_neighbors(cid, hops=args.get("hops", 1))
            if not neighbors["connections"]:
                return f"Concept `{cid}` has no indexed relation links."

            lines = [f"Connections for `{cid}`:"]
            for c in neighbors["connections"]:
                if c["direction"] == "outgoing":
                    lines.append(f"- ({c['relation']}) -> `[[{c['target_id']}]]`: {c['reason']}")
                else:
                    lines.append(f"- <- ({c['relation']}) from `[[{c['source_id']}]]`: {c['reason']}")
            return "\n".join(lines)

        elif name == "wiki_list_concepts":
            limit = args.get("limit", 30)
            concepts = self.store.list_concepts(limit=limit)
            if not concepts:
                return "The knowledge warehouse is currently empty."
            lines = [f"Total {len(concepts)} concepts:"]
            for c in concepts:
                lines.append(f"- `{c['id']}`: {c['name']} [{c['type']}] - {c['summary'][:100]}...")
            return "\n".join(lines)

        elif name == "wiki_ingest":
            source = args.get("source", "").strip()
            try:
                parsed = parse_source(source)
                created = self.distiller.distill(
                    doc_title=parsed["title"],
                    text=parsed["text"],
                    source=parsed["source"]
                )
                self.graph.build_graph()
                c_names = [f"`{c.id}` ({c.name})" for c in created]
                return f"Successfully ingested '{parsed['title']}'. Distilled {len(created)} concepts: {', '.join(c_names)}"
            except Exception as e:
                return f"Ingestion failed: {e}\n{traceback.format_exc()}"

        elif name == "wiki_stats":
            stats = self.store.db.stats()
            return (
                f"LLMWiki Statistics:\n"
                f"- Total Concepts (Layer 2): {stats['total_concepts']}\n"
                f"- Total Knowledge Edges: {stats['total_relations']}\n"
                f"- Total Raw Documents (Layer 1): {stats['total_sources']}"
            )

        return f"Unknown tool: {name}"

    def run_stdio(self):
        """Standard MCP JSON-RPC 2.0 read-eval loop over stdin/stdout."""
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue

            try:
                req = json.loads(line)
            except Exception:
                continue

            req_id = req.get("id")
            method = req.get("method")
            params = req.get("params", {})

            if method == "initialize":
                resp = {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {
                            "tools": {}
                        },
                        "serverInfo": {
                            "name": "llmwiki",
                            "version": "1.0.0"
                        }
                    }
                }
                sys.stdout.write(json.dumps(resp) + "\n")
                sys.stdout.flush()

            elif method == "notifications/initialized":
                # Notification, no response required
                pass

            elif method == "tools/list":
                resp = {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "tools": self.get_tool_definitions()
                    }
                }
                sys.stdout.write(json.dumps(resp) + "\n")
                sys.stdout.flush()

            elif method == "tools/call":
                tool_name = params.get("name")
                tool_args = params.get("arguments", {})
                tool_output = self.call_tool(tool_name, tool_args)

                resp = {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [
                            {
                                "type": "text",
                                "text": tool_output
                            }
                        ],
                        "isError": False
                    }
                }
                sys.stdout.write(json.dumps(resp) + "\n")
                sys.stdout.flush()

            elif method == "ping":
                resp = {"jsonrpc": "2.0", "id": req_id, "result": {}}
                sys.stdout.write(json.dumps(resp) + "\n")
                sys.stdout.flush()

            elif req_id is not None:
                # Unsupported method
                resp = {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32601, "message": f"Method not found: {method}"}
                }
                sys.stdout.write(json.dumps(resp) + "\n")
                sys.stdout.flush()


def main():
    server = MCPServer()
    server.run_stdio()

if __name__ == "__main__":
    main()
