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
                "description": "Fast search across the local LLMWiki knowledge warehouse. Supports hybrid RRF, vector semantic, keyword, and HippoRAG graph spreading activation with automatic workspace routing.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search query keywords or semantic concept"},
                        "mode": {
                            "type": "string",
                            "enum": ["hybrid", "semantic", "keyword", "graph", "hipporag"],
                            "default": "hybrid",
                            "description": "Search mode: hybrid (semantic vector + FTS5 with RRF), semantic (vector only), keyword (BM25 only), graph/hipporag (Personalized PageRank spreading activation)"
                        },
                        "limit": {"type": "integer", "default": 5, "description": "Max results to return"},
                        "workspace": {
                            "type": "string",
                            "default": "auto",
                            "description": "Workspace scope: 'auto' (semantic vector routing), 'all' (entire warehouse), or specific workspace ID"
                        }
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
                "name": "wiki_find_path",
                "description": "Find the shortest knowledge reasoning path between two concepts in the knowledge graph.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "start_id": {"type": "string", "description": "Starting concept ID"},
                        "target_id": {"type": "string", "description": "Target concept ID"}
                    },
                    "required": ["start_id", "target_id"]
                }
            },
            {
                "name": "wiki_graph_analytics",
                "description": "Run advanced graph analytics: GraphRAG community detection, PageRank core hub concepts, and knowledge bridges.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "analysis_type": {
                            "type": "string",
                            "enum": ["communities", "pagerank_hubs", "bridges", "metrics"],
                            "default": "metrics",
                            "description": "Type of graph analysis to perform"
                        },
                        "top_k": {"type": "integer", "default": 10, "description": "Max items to return"}
                    }
                }
            },
            {
                "name": "wiki_list_concepts",
                "description": "List all distilled concepts currently in the warehouse, optionally filtered by workspace.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "limit": {"type": "integer", "default": 30, "description": "Maximum concepts to list"},
                        "workspace": {"type": "string", "description": "Workspace filter ID, or 'all'"}
                    }
                }
            },
            {
                "name": "wiki_list_workspaces",
                "description": "List all isolated knowledge workspaces and domain categories with their concept counts.",
                "inputSchema": {
                    "type": "object",
                    "properties": {}
                }
            },
            {
                "name": "wiki_create_workspace",
                "description": "Create a new isolated knowledge workspace domain.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "workspace_id": {"type": "string", "description": "Unique workspace ID"},
                        "name": {"type": "string", "description": "Display name"},
                        "description": {"type": "string", "description": "Domain scope description for automatic semantic routing"}
                    },
                    "required": ["workspace_id"]
                }
            },
            {
                "name": "wiki_ingest",
                "description": "Ingest a new file (PDF, Markdown, TXT) or web URL into the 3-tier knowledge warehouse.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "source": {"type": "string", "description": "File path or HTTP/HTTPS URL"},
                        "workspace": {"type": "string", "description": "Target workspace ID (defaults to active workspace)"}
                    },
                    "required": ["source"]
                }
            },
            {
                "name": "wiki_stats",
                "description": "Return summary statistics of the knowledge warehouse (concept count, relation count, raw doc count, active workspace).",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "workspace": {"type": "string", "description": "Optional workspace filter"}
                    }
                }
            }


        ]

    def call_tool(self, name: str, args: Dict[str, Any]) -> str:
        """Route tool invocation to appropriate handler."""
        if name == "wiki_search":
            query = args.get("query", "")
            mode = args.get("mode", "hybrid")
            limit = args.get("limit", 5)
            ws = args.get("workspace", "auto")
            hits = self.store.search(query=query, mode=mode, limit=limit, workspace=ws)
            if not hits:
                scope_str = f" in workspace '{ws}'" if ws and ws not in ("auto", "all") else ""
                return f"No concepts found matching '{query}' (mode: {mode}{scope_str})."

            scope_note = f" (Workspace scope: {ws})" if ws else ""
            output = [f"Found {len(hits)} matching concepts{scope_note}:"]
            for h in hits:
                tags = f"[{', '.join(h.tags)}]" if h.tags else ""
                ws_str = f" [Workspace: {h.workspace}]" if h.workspace else ""
                output.append(f"\n- **ID**: `{h.concept_id}` ({h.name}) | Type: `{h.type}`{ws_str} {tags} [Score: {h.score}]")
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

        elif name == "wiki_find_path":
            start_id = args.get("start_id", "").strip()
            target_id = args.get("target_id", "").strip()
            path = self.graph.find_path(start_id, target_id)
            if not path:
                return f"No knowledge reasoning path found between `{start_id}` and `{target_id}`."
            lines = [f"Reasoning path from `{start_id}` to `{target_id}` ({len(path)} steps):"]
            for step in path:
                reason = f" ({step['reason']})" if step.get("reason") else ""
                lines.append(f"- `{step['from']}` ==[{step['relation']}]==> `{step['to']}`{reason}")
            return "\n".join(lines)

        elif name == "wiki_graph_analytics":
            atype = args.get("analysis_type", "metrics")
            top_k = args.get("top_k", 10)

            if atype == "communities":
                comms = self.graph.detect_communities()
                if not comms:
                    return "No distinct communities detected."
                lines = [f"Detected {len(comms)} Knowledge Communities:"]
                for c in comms:
                    c_ids = [m["id"] for m in c["concepts"][:5]]
                    lines.append(f"- Community `{c['community_id']}` (Size: {c['size']}, Hub: `{c['hub_concept']}`): {', '.join(c_ids)}")
                return "\n".join(lines)

            elif atype == "pagerank_hubs":
                hubs = self.graph.get_central_concepts(top_k=top_k)
                lines = [f"Top {len(hubs)} Core Hub Concepts (PageRank):"]
                for h in hubs:
                    lines.append(f"- `{h['concept_id']}` ({h['name']}): score={h['score']}, degree={h['degree']}")
                return "\n".join(lines)

            elif atype == "bridges":
                bridges = self.graph.get_bridges(top_k=top_k)
                if not bridges:
                    return "No bridge concepts found."
                lines = [f"Top {len(bridges)} Knowledge Bridges:"]
                for b in bridges:
                    lines.append(f"- `{b['concept_id']}` ({b['name']}): betweenness={b['betweenness']}")
                return "\n".join(lines)

            else:
                metrics = self.graph.get_metrics()
                return (
                    f"Knowledge Graph Metrics:\n"
                    f"- Node Count: {metrics['node_count']}\n"
                    f"- Edge Count: {metrics['edge_count']}\n"
                    f"- Graph Density: {metrics['density']}\n"
                    f"- Connected Components: {metrics['connected_components']}\n"
                    f"- Average Degree: {metrics['avg_degree']}"
                )

        elif name == "wiki_list_concepts":
            limit = args.get("limit", 30)
            ws = args.get("workspace")
            concepts = self.store.list_concepts(limit=limit, workspace=ws)
            if not concepts:
                ws_note = f" in workspace '{ws}'" if ws else ""
                return f"No concepts found{ws_note}."
            ws_note = f" [Workspace: {ws}]" if ws else ""
            lines = [f"Total {len(concepts)} concepts{ws_note}:"]
            for c in concepts:
                ws_badge = f" [{c.get('workspace', 'default')}]" if c.get("workspace") else ""
                lines.append(f"- `{c['id']}`{ws_badge}: {c['name']} [{c['type']}] - {c['summary'][:100]}...")
            return "\n".join(lines)

        elif name == "wiki_list_workspaces":
            workspaces = self.store.list_workspaces()
            if not workspaces:
                return "No workspaces found."
            lines = [f"Workspaces ({len(workspaces)} total):"]
            for w in workspaces:
                st = self.store.db.stats(workspace=w.id)
                desc = f" - {w.description}" if w.description else ""
                lines.append(f"- `{w.id}` ({w.name or 'Unnamed'}) [{st['total_concepts']} concepts]{desc}")
            return "\n".join(lines)

        elif name == "wiki_create_workspace":
            ws_id = args.get("workspace_id", "").strip()
            if not ws_id:
                return "Error: 'workspace_id' is required."
            ws = self.store.create_workspace(
                workspace_id=ws_id,
                name=args.get("name", ""),
                description=args.get("description", "")
            )
            return f"Successfully created workspace `{ws.id}` ({ws.name or 'Unnamed'})."

        elif name == "wiki_ingest":
            source = args.get("source", "").strip()
            ws = args.get("workspace")
            target_store = Store(workspace=ws) if ws else self.store
            target_distiller = Distiller(store=target_store) if ws else self.distiller
            try:
                parsed = parse_source(source)
                created = target_distiller.distill(
                    doc_title=parsed["title"],
                    text=parsed["text"],
                    source=parsed["source"]
                )
                self.graph.build_graph()
                c_names = [f"`{c.id}` ({c.name})" for c in created]
                ws_note = f" into workspace '{ws}'" if ws else ""
                return f"Successfully ingested '{parsed['title']}'{ws_note}. Distilled {len(created)} concepts: {', '.join(c_names)}"
            except Exception as e:
                return f"Ingestion failed: {e}\n{traceback.format_exc()}"

        elif name == "wiki_stats":
            ws = args.get("workspace")
            stats = self.store.db.stats(workspace=ws)
            ws_note = f" (Workspace: {ws})" if ws else ""
            return (
                f"LLMWiki Statistics{ws_note}:\n"
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
