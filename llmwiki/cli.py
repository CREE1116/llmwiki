"""
Command-line interface for LLMWiki.
Fast, clean CLI for ingesting documents, querying knowledge, and integrating with Agent Skills.
Supports both human-readable and --json machine-readable outputs for AI Agents.
"""
import argparse
import sys
import json
from pathlib import Path
from .storage.store import Store
from .storage.graph import KnowledgeGraph
from .storage.models import Relation
from .parser import parse_source
from .parser.web_parser import WebParser
from .synthesizer.distiller import Distiller
from .mcp.server import MCPServer

def cmd_ingest(args):
    sources = args.sources
    store = Store()
    distiller = Distiller(store=store)
    total = len(sources)
    all_results = []
    page_primary = {}
    page_links = {}

    for i, source in enumerate(sources, 1):
        try:
            if source == "-":
                parsed_items = [{
                    "title": "Piped Text",
                    "text": sys.stdin.read(),
                    "source": "stdin",
                    "source_type": "text"
                }]
            elif (
                getattr(args, "explore", False)
                and (source.startswith("http://") or source.startswith("https://"))
            ):
                parsed_items = WebParser.crawl(
                    source,
                    depth=getattr(args, "depth", 1),
                    max_pages=getattr(args, "max_pages", 8),
                    same_site=True,
                )
                if not getattr(args, "json", False):
                    print(f"[*] [{i}/{total}] Found {len(parsed_items)} relevant page(s) from '{source}'")
            else:
                parsed_items = [parse_source(source)]
        except Exception as e:
            if getattr(args, "json", False):
                print(f"Failed to parse '{source}': {e}", file=sys.stderr)
            else:
                print(f"[-] [{i}/{total}] Failed to parse '{source}': {e}")
            continue

        for page_index, parsed in enumerate(parsed_items, 1):
            if not getattr(args, "json", False):
                suffix = f" [{page_index}/{len(parsed_items)}]" if len(parsed_items) > 1 else ""
                print(f"[*] [{i}/{total}]{suffix} Distilling '{parsed['title']}'...")

            try:
                concepts = distiller.distill(
                    doc_title=parsed["title"],
                    text=parsed["text"],
                    source=parsed["source"]
                )
                item_res = {
                    "title": parsed["title"],
                    "source": parsed["source"],
                    "crawl_depth": parsed.get("crawl_depth"),
                    "parent_url": parsed.get("parent_url"),
                    "concepts": [
                        {
                            "id": c.id,
                            "name": c.name,
                            "type": c.type,
                            "summary": c.summary,
                            "tags": c.tags,
                            "relations": [{"target": r.target, "type": r.type, "reason": r.reason} for r in c.relations]
                        }
                        for c in concepts
                    ]
                }
                all_results.append(item_res)

                if concepts and parsed.get("source_type") == "web":
                    page_primary[parsed["source"]] = concepts[0].id
                    page_links[parsed["source"]] = parsed.get("links", [])

                if not getattr(args, "json", False):
                    c_names = [f"`{c.id}`" for c in concepts]
                    print(f"    [+] Created {len(concepts)} concept(s): {', '.join(c_names)}")
            except Exception as e:
                if getattr(args, "json", False):
                    print(f"Distillation failed for '{parsed['title']}': {e}", file=sys.stderr)
                else:
                    print(f"[-] Distillation failed for '{parsed['title']}': {e}")

    references_by_concept = {}
    for source_url, links in page_links.items():
        source_id = page_primary.get(source_url)
        if not source_id:
            continue
        for link in links:
            target_url = link.get("url")
            target_id = page_primary.get(target_url)
            if not target_id or target_id == source_id:
                continue
            anchor = (link.get("text") or "").strip()
            reason = f"원문 링크: {anchor}" if anchor else "원문 문서에서 직접 연결됨"
            references_by_concept.setdefault(source_id, {})[target_id] = reason

    # Persist authored page topology into the concept Markdown itself so the
    # app's startup reindex cannot discard these edges.
    for source_id, targets in references_by_concept.items():
        concept = store.get_concept(source_id)
        if not concept:
            continue

        # Preserve outgoing DB-only links created during semantic dedup/linking
        # before save_concept rebuilds this concept's relation rows.
        for row in store.db.get_relations_for(source_id):
            if row.get("direction") != "outgoing":
                continue
            target_id = row.get("related_id")
            relation_type = row.get("relation_type", "related_to")
            if not target_id or target_id == source_id:
                continue
            if not any(r.target == target_id and r.type == relation_type for r in concept.relations):
                concept.relations.append(Relation(
                    type=relation_type,
                    target=target_id,
                    reason=row.get("reason", ""),
                ))

        for target_id, reason in targets.items():
            existing = next(
                (r for r in concept.relations if r.target == target_id and r.type == "references"),
                None,
            )
            if existing:
                if not existing.reason and reason:
                    existing.reason = reason
            else:
                concept.relations.append(Relation(
                    type="references",
                    target=target_id,
                    reason=reason,
                ))
        store.save_concept(concept)

    if getattr(args, "json", False):
        output = all_results if len(all_results) > 1 else (all_results[0] if all_results else {})
        print(json.dumps(output, ensure_ascii=False, indent=2))

def cmd_search(args):
    store = Store()
    caller = getattr(args, "caller", "cli")
    hits = store.search(query=args.query, mode=args.mode, limit=args.limit)

    # Log query
    store.db.log_query(
        caller=caller,
        action="search",
        query=args.query,
        details={"mode": args.mode, "matched_ids": [h.concept_id for h in hits]},
        result_count=len(hits)
    )

    if getattr(args, "json", False):
        res = [
            {
                "concept_id": h.concept_id,
                "name": h.name,
                "type": h.type,
                "summary": h.summary,
                "tags": h.tags,
                "score": h.score,
                "matched_by": h.matched_by
            }
            for h in hits
        ]
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return

    if not hits:
        print(f"[-] No concepts found matching '{args.query}' (mode: {args.mode})")
        return

    print(f"[+] Found {len(hits)} match(es) for '{args.query}' [Mode: {args.mode}]:\n")
    for i, h in enumerate(hits, 1):
        print(f"[{i}] {h.name} (`{h.concept_id}`) | Score: {h.score:.4f} | Type: {h.type}")
        print(f"    Summary: {h.summary}")
        if h.tags:
            print(f"    Tags: {', '.join(h.tags)}")
        print()

def cmd_get(args):
    store = Store()
    caller = getattr(args, "caller", "cli")
    cid = args.concept_id
    concept = store.get_concept(cid)

    store.db.log_query(
        caller=caller,
        action="get",
        query=cid,
        details={"found": bool(concept), "raw": getattr(args, "raw", False)},
        result_count=1 if concept else 0
    )
    if not concept:
        if getattr(args, "json", False):
            print(json.dumps({"error": f"Concept '{cid}' not found"}, ensure_ascii=False))
        else:
            print(f"[-] Concept '{cid}' not found.")
        sys.exit(1)

    graph_connections = []
    if args.neighbors:
        graph = KnowledgeGraph(db=store.db)
        neighbors = graph.get_neighbors(cid)
        graph_connections = neighbors["connections"]

    raw_snippets = []
    if args.raw:
        raw_ids = [s.replace("raw:", "") for s in concept.sources if s.startswith("raw:")]
        for rid in raw_ids:
            doc = store.raw.get(rid)
            if doc:
                raw_snippets.append({
                    "doc_id": rid,
                    "title": doc["title"],
                    "source_uri": doc["source_uri"],
                    "content": doc["content"][:2000]
                })

    if getattr(args, "json", False):
        import dataclasses
        c_dict = dataclasses.asdict(concept)
        c_dict["graph_connections"] = graph_connections
        c_dict["raw_snippets"] = raw_snippets
        print(json.dumps(c_dict, ensure_ascii=False, indent=2))
        return

    print(concept.to_markdown())

    if graph_connections:
        print("\n### Graph Connections")
        for c in graph_connections:
            arrow = "->" if c["direction"] == "outgoing" else "<-"
            target = c.get("target_id") or c.get("source_id")
            reason = f" ({c['reason']})" if c.get("reason") else ""
            print(f"- `{cid}` {arrow} `[[{target}]]` [{c['relation']}]{reason}")

    if raw_snippets:
        for snip in raw_snippets:
            print(f"\n### Layer 1 Raw Document Excerpt: {snip['title']}")
            print(snip["content"] + ("\n... [truncated]" if len(snip["content"]) >= 2000 else ""))

def cmd_raw(args):
    store = Store()
    doc = store.raw.get(args.doc_id)
    if not doc:
        if getattr(args, "json", False):
            print(json.dumps({"error": f"Raw doc '{args.doc_id}' not found"}, ensure_ascii=False))
        else:
            print(f"[-] Raw document '{args.doc_id}' not found.")
        sys.exit(1)

    if getattr(args, "json", False):
        print(json.dumps(doc, ensure_ascii=False, indent=2))
        return

    print(f"Title: {doc['title']}")
    print(f"Source: {doc['source_uri']}")
    print(f"Chars: {doc['char_count']} | Created: {doc['created_at']}")
    print("=" * 60)
    print(doc["content"])

def cmd_delete_raw(args):
    store = Store()
    success = store.delete_raw_document(args.doc_id)
    result = {"success": success, "doc_id": args.doc_id}
    if not success:
        result["error"] = f"Failed to delete raw doc '{args.doc_id}' (not found or inaccessible)"

    if getattr(args, "json", False):
        print(json.dumps(result, ensure_ascii=False))
        return

    if success:
        print(f"[+] Successfully deleted raw document '{args.doc_id}'.")
    else:
        print(f"[-] Raw document '{args.doc_id}' not found.", file=sys.stderr)
        sys.exit(1)

def cmd_delete_concept(args):
    store = Store()
    success = store.delete_concept(args.concept_id)
    result = {"success": success, "concept_id": args.concept_id}
    if not success:
        result["error"] = f"Concept '{args.concept_id}' not found or could not be deleted"

    if getattr(args, "json", False):
        print(json.dumps(result, ensure_ascii=False))
        return

    if success:
        print(f"[+] Successfully deleted concept '{args.concept_id}'.")
    else:
        print(f"[-] Concept '{args.concept_id}' not found.", file=sys.stderr)
        sys.exit(1)

def cmd_graph(args):
    store = Store()
    graph = KnowledgeGraph(db=store.db)
    cid = args.concept_id
    neighbors = graph.get_neighbors(cid, hops=args.hops)

    if getattr(args, "json", False):
        print(json.dumps(neighbors, ensure_ascii=False, indent=2))
        return

    print(f"[+] Knowledge Links for `{cid}`:")
    if not neighbors["connections"]:
        print(f"    (No indexed links for `{cid}`)")
        return
    for c in neighbors["connections"]:
        if c["direction"] == "outgoing":
            print(f"  --> [{c['relation']}] `[[{c['target_id']}]]`: {c['reason']}")
        else:
            print(f"  <-- [{c['relation']}] `[[{c['source_id']}]]`: {c['reason']}")

def cmd_stats(args):
    store = Store()
    stats = store.db.stats()
    raw_docs = store.raw.list_all(limit=5)

    if getattr(args, "json", False):
        stats["recent_sources"] = raw_docs
        print(json.dumps(stats, ensure_ascii=False, indent=2))
        return

    print("[+] LLMWiki Warehouse Statistics:")
    print(f"    - Layer 1 Raw Documents: {stats['total_sources']}")
    print(f"    - Layer 2 Atomic Concepts: {stats['total_concepts']}")
    print(f"    - Layer 3 Knowledge Edges: {stats['total_relations']}")
    if raw_docs:
        print("\n    Recent Ingested Sources:")
        for r in raw_docs:
            print(f"      * [{r['id']}] {r['title']} ({r['char_count']} chars)")

def cmd_logs(args):
    store = Store()
    logs = store.db.get_query_logs(limit=args.limit)
    if getattr(args, "json", False):
        print(json.dumps(logs, ensure_ascii=False, indent=2))
        return

    if not logs:
        print("[-] No query logs recorded yet.")
        return

    print(f"[+] Last {len(logs)} LLM / Agent Retrieval Log(s):\n")
    for r in logs:
        details_str = f" | {r['details']}" if r.get("details") else ""
        print(f"[{r['timestamp']}] [{r['caller'].upper()}] {r['action']}: '{r['query']}' (Hits: {r['result_count']}){details_str}")

def cmd_graph_data(args):
    store = Store()
    data = store.db.get_full_graph_data()
    print(json.dumps(data, ensure_ascii=False, indent=2 if getattr(args, "pretty", False) else None))

def cmd_list_concepts(args):
    store = Store()
    concepts = store.list_concepts(limit=args.limit)
    if getattr(args, "json", False):
        print(json.dumps(concepts, ensure_ascii=False, indent=2))
        return
    for c in concepts:
        print(f"- `{c['id']}`: {c['name']} [{c['type']}] ({c['summary'][:80]}...)")

def cmd_list_raw(args):
    store = Store()
    docs = store.raw.list_all(limit=args.limit)
    if getattr(args, "json", False):
        print(json.dumps(docs, ensure_ascii=False, indent=2))
        return
    for d in docs:
        print(f"- [{d['id']}] {d['title']} ({d['char_count']} chars, {d['created_at']})")

def cmd_check_env(args):
    from .installer import check_environment
    from .config import load_config
    env = check_environment()
    cfg = load_config()
    cfg["installed_skills"] = {
        **cfg.get("installed_skills", {}),
        "antigravity": bool(env["antigravity"].get("skill_installed")),
        "claude": bool(env["claude"].get("mcp_installed")),
        "codex": bool(env["codex"].get("mcp_installed")),
    }
    res = {"env": env, "config": cfg}
    if getattr(args, "json", False):
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return

    print("[+] LLMWiki Environment Status:")
    ollama = env["ollama"]
    print(f"    - Ollama: {'Running' if ollama['running'] else 'Not running'}")
    if ollama["models"]:
        print(f"      Models: {', '.join(ollama['models'])}")
    print(f"    - Antigravity: {'Detected' if env['antigravity']['detected'] else 'Not detected'}")
    print(f"    - Claude Code: {'Detected' if env['claude']['detected'] else 'Not detected'}")
    print(f"    - Current Provider: {cfg.get('provider')} ({cfg.get('model')})")

def cmd_install_skills(args):
    from .installer import install_antigravity_skill, install_claude_mcp, install_codex_mcp, install_cli_symlink
    results = {}
    install_all = args.all or not (args.antigravity or args.claude or args.codex or args.symlink)
    if args.antigravity or install_all:
        results["antigravity"] = install_antigravity_skill()
    if args.claude or install_all:
        results["claude"] = install_claude_mcp()
    if args.codex or install_all:
        results["codex"] = install_codex_mcp()
    if args.symlink or install_all:
        results["cli_symlink"] = install_cli_symlink()

    if getattr(args, "json", False):
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return

    print("[+] Skill & MCP Installation Results:")
    for k, v in results.items():
        print(f"    - {k}: {'Installed successfully' if v else 'Failed or skipped'}")

def cmd_bootstrap(args):
    from .installer import bootstrap_installation
    results = bootstrap_installation()
    if getattr(args, "json", False):
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return
    print("[+] LLMWiki desktop setup complete.")
    print(f"    - CLI: {results.get('cli_path') or 'not installed'}")
    print(f"    - Provider: {results.get('config', {}).get('provider', 'unknown')}")

def cmd_save_config(args):
    from .config import save_config
    updates = {}
    if args.provider:
        updates["provider"] = args.provider
    if args.model:
        updates["model"] = args.model
    if args.api_key is not None:
        updates["api_key"] = args.api_key
    if args.initialized:
        updates["initialized"] = True

    saved = save_config(updates)
    if getattr(args, "json", False):
        print(json.dumps(saved, ensure_ascii=False, indent=2))
        return
    print(f"[+] Configuration saved. Provider: {saved['provider']}, Model: {saved['model']}")

def cmd_reindex(args):
    store = Store()
    count = store.reindex_all()
    result = {"indexed": count}
    if getattr(args, "json", False):
        print(json.dumps(result, ensure_ascii=False))
    else:
        print(f"[+] Re-indexed {count} concept(s).")

def cmd_re_distill(args):
    store = Store()
    distiller = Distiller(store=store)
    docs = store.raw.list_all(limit=args.limit)
    total = len(docs)
    processed = 0
    total_concepts = 0

    if not getattr(args, "json", False):
        print(f"[*] Re-distilling atomic knowledge concepts from {total} raw documents...")

    for i, d in enumerate(docs, 1):
        doc_data = store.raw.get(d["id"])
        if not doc_data:
            continue
        try:
            concepts = distiller._heuristic_distill_fallback(
                doc_title=doc_data["title"],
                text=doc_data["content"],
                source=doc_data["source_uri"],
                raw_doc_id=doc_data["id"]
            )
            processed += 1
            total_concepts += len(concepts)
            if not getattr(args, "json", False):
                print(f"    [{i}/{total}] '{doc_data['title'][:32]}' -> {len(concepts)} atomic concepts")
        except Exception as e:
            if not getattr(args, "json", False):
                print(f"    [-] Failed for {d['id']}: {e}", file=sys.stderr)

    if getattr(args, "json", False):
        print(json.dumps({"processed_docs": processed, "atomic_concepts": total_concepts}, ensure_ascii=False))
        return
    print(f"[+] Re-distillation complete: {processed} documents processed, {total_concepts} atomic concepts linked.")

def cmd_serve_mcp(args):
    server = MCPServer()
    server.run_stdio()

def main():
    parser = argparse.ArgumentParser(description="LLMWiki: Lightweight LLM-Native Knowledge Warehouse")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Ingest
    p_ingest = subparsers.add_parser("ingest", help="Ingest file(s) (PDF, MD, TXT), URL(s), or stdin (-)")
    p_ingest.add_argument("sources", nargs="+", help="Path to file(s), web URL(s), or '-' for stdin")
    p_ingest.add_argument("--explore", action="store_true", help="Follow relevant same-site links for web URLs")
    p_ingest.add_argument("--depth", type=int, choices=[0, 1, 2, 3], default=1, help="Web exploration depth (default: 1)")
    p_ingest.add_argument("--max-pages", type=int, default=8, help="Maximum web pages per starting URL (default: 8, hard cap: 40)")
    p_ingest.add_argument("--json", action="store_true", help="Output JSON format")

    # Search
    p_search = subparsers.add_parser("search", help="Search the knowledge warehouse")
    p_search.add_argument("query", help="Keywords or semantic query")
    p_search.add_argument("--mode", choices=["hybrid", "semantic", "keyword"], default="hybrid", help="Search mode")
    p_search.add_argument("--limit", type=int, default=5, help="Max results")
    p_search.add_argument("--caller", default="cli", help="Caller identity for logging (e.g. skill, cli, app)")
    p_search.add_argument("--json", action="store_true", help="Output JSON format")

    # Get
    p_get = subparsers.add_parser("get", help="Fetch a concept card by ID")
    p_get.add_argument("concept_id", help="Concept ID (e.g. flash_attention)")
    p_get.add_argument("--neighbors", action="store_true", help="Include 1-hop relation links")
    p_get.add_argument("--raw", action="store_true", help="Include Layer 1 raw document excerpt")
    p_get.add_argument("--caller", default="cli", help="Caller identity for logging")
    p_get.add_argument("--json", action="store_true", help="Output JSON format")

    # Raw
    p_raw = subparsers.add_parser("raw", help="View full Layer 1 ground-truth document")
    p_raw.add_argument("doc_id", help="Raw doc ID")
    p_raw.add_argument("--json", action="store_true", help="Output JSON format")

    # Delete Raw
    p_del_raw = subparsers.add_parser("delete-raw", help="Delete a Layer 1 raw document")
    p_del_raw.add_argument("doc_id", help="Raw doc ID to delete")
    p_del_raw.add_argument("--json", action="store_true", help="Output JSON format")

    p_del_concept = subparsers.add_parser("delete-concept", help="Delete a distilled concept")
    p_del_concept.add_argument("concept_id", help="Concept ID to delete")
    p_del_concept.add_argument("--json", action="store_true", help="Output JSON format")

    # Graph
    p_graph = subparsers.add_parser("graph", help="Explore concept relations in the knowledge graph")
    p_graph.add_argument("concept_id", help="Starting concept ID")
    p_graph.add_argument("--hops", type=int, default=1, help="Hop depth")
    p_graph.add_argument("--json", action="store_true", help="Output JSON format")

    # Graph-Data (Full JSON for Electron/D3/Cytoscape)
    p_graph_data = subparsers.add_parser("graph-data", help="Export full nodes and edges in JSON")
    p_graph_data.add_argument("--pretty", action="store_true", help="Pretty print JSON")

    # List concepts
    p_list_c = subparsers.add_parser("list-concepts", help="List all concepts")
    p_list_c.add_argument("--limit", type=int, default=100, help="Max items")
    p_list_c.add_argument("--json", action="store_true", help="Output JSON format")

    # List raw documents
    p_list_r = subparsers.add_parser("list-raw", help="List all Layer 1 raw documents")
    p_list_r.add_argument("--limit", type=int, default=100, help="Max items")
    p_list_r.add_argument("--json", action="store_true", help="Output JSON format")

    # Logs
    p_logs = subparsers.add_parser("logs", help="View LLM retrieval audit logs")
    p_logs.add_argument("--limit", type=int, default=50, help="Max logs")
    p_logs.add_argument("--json", action="store_true", help="Output JSON format")

    # Stats
    p_stats = subparsers.add_parser("stats", help="Show warehouse statistics")
    p_stats.add_argument("--json", action="store_true", help="Output JSON format")

    # Check-env
    p_check = subparsers.add_parser("check-env", help="Check local models and agent environments")
    p_check.add_argument("--json", action="store_true", help="Output JSON format")

    # Install-skills
    p_inst = subparsers.add_parser("install-skills", help="Connect LLMWiki to installed agent CLIs")
    p_inst.add_argument("--antigravity", action="store_true", help="Install Antigravity skill")
    p_inst.add_argument("--claude", action="store_true", help="Register Claude MCP")
    p_inst.add_argument("--codex", action="store_true", help="Register Codex MCP")
    p_inst.add_argument("--symlink", action="store_true", help="Install CLI symlink")
    p_inst.add_argument("--all", action="store_true", help="Install all available integrations")
    p_inst.add_argument("--json", action="store_true", help="Output JSON format")

    p_bootstrap = subparsers.add_parser("bootstrap", help="Configure a packaged desktop installation")
    p_bootstrap.add_argument("--json", action="store_true", help="Output JSON format")

    # Save-config
    p_cfg = subparsers.add_parser("save-config", help="Update settings")
    p_cfg.add_argument("--provider", choices=["ollama", "codex_cli", "claude_cli", "antigravity", "claude", "openai"])
    p_cfg.add_argument("--model", type=str)
    p_cfg.add_argument("--api-key", type=str)
    p_cfg.add_argument("--initialized", action="store_true")
    p_cfg.add_argument("--json", action="store_true", help="Output JSON format")

    p_reindex = subparsers.add_parser("reindex", help="Rebuild indexes from concept files")
    p_reindex.add_argument("--json", action="store_true", help="Output JSON format")

    p_redistill = subparsers.add_parser("re-distill", help="Re-distill atomic concepts and links from raw documents")
    p_redistill.add_argument("--limit", type=int, default=100, help="Max raw documents to process")
    p_redistill.add_argument("--json", action="store_true", help="Output JSON format")

    # Serve MCP
    subparsers.add_parser("serve-mcp", help="Run MCP JSON-RPC stdio server")

    args = parser.parse_args()
    if args.command == "ingest":
        cmd_ingest(args)
    elif args.command == "search":
        cmd_search(args)
    elif args.command == "get":
        cmd_get(args)
    elif args.command == "raw":
        cmd_raw(args)
    elif args.command == "delete-raw":
        cmd_delete_raw(args)
    elif args.command == "delete-concept":
        cmd_delete_concept(args)
    elif args.command == "graph":
        cmd_graph(args)
    elif args.command == "graph-data":
        cmd_graph_data(args)
    elif args.command == "list-concepts":
        cmd_list_concepts(args)
    elif args.command == "list-raw":
        cmd_list_raw(args)
    elif args.command == "logs":
        cmd_logs(args)
    elif args.command == "check-env":
        cmd_check_env(args)
    elif args.command == "install-skills":
        cmd_install_skills(args)
    elif args.command == "bootstrap":
        cmd_bootstrap(args)
    elif args.command == "save-config":
        cmd_save_config(args)
    elif args.command == "reindex":
        cmd_reindex(args)
    elif args.command == "re-distill":
        cmd_re_distill(args)
    elif args.command == "stats":
        cmd_stats(args)
    elif args.command == "serve-mcp":
        cmd_serve_mcp(args)

if __name__ == "__main__":
    main()
