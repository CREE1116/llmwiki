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
from .config import get_active_workspace, set_active_workspace
from .parser import parse_source
from .parser.web_parser import WebParser
from .synthesizer.distiller import Distiller
from .mcp.server import MCPServer

def cmd_ingest(args):
    sources = args.sources
    target_ws = getattr(args, "workspace", None)
    store = Store(workspace=target_ws)
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
                    greedy=getattr(args, "greedy", False),
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
    ws = getattr(args, "workspace", "auto")
    hits = store.search(query=args.query, mode=args.mode, limit=args.limit, workspace=ws)

    # Log query
    store.db.log_query(
        caller=caller,
        action="search",
        query=args.query,
        details={"mode": args.mode, "workspace": ws, "matched_ids": [h.concept_id for h in hits]},
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
                "matched_by": h.matched_by,
                "workspace": h.workspace
            }
            for h in hits
        ]
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return

    if not hits:
        scope_str = f" in workspace '{ws}'" if ws and ws not in ("auto", "all") else ""
        print(f"[-] No concepts found matching '{args.query}' (mode: {args.mode}{scope_str})")
        return

    scope_info = f" [Scope: {ws}]" if ws else ""
    print(f"[+] Found {len(hits)} match(es) for '{args.query}' [Mode: {args.mode}]{scope_info}:\n")
    for i, h in enumerate(hits, 1):
        ws_badge = f" | Workspace: [{h.workspace}]" if h.workspace else ""
        print(f"[{i}] {h.name} (`{h.concept_id}`) | Score: {h.score:.4f}{ws_badge} | Type: {h.type}")
        print(f"    Summary: {h.summary}")
        if h.tags:
            print(f"    Tags: {', '.join(h.tags)}")
        print()


def cmd_deep_dive(args):
    store = Store(workspace=getattr(args, "workspace", None))
    caller = getattr(args, "caller", "cli")
    cid = args.concept_id

    if not getattr(args, "json", False):
        greedy_str = " (Greedy Unbounded Mode)" if getattr(args, "greedy", False) else ""
        print(f"[*] Starting autonomous web deep-dive for concept `{cid}`{greedy_str}...")

    res = store.deep_dive(
        concept_id=cid,
        depth=getattr(args, "depth", 1),
        max_pages=getattr(args, "max_pages", 5),
        greedy=getattr(args, "greedy", False),
        extra_query=getattr(args, "query", None)
    )

    store.db.log_query(
        caller=caller,
        action="deep_dive",
        query=cid,
        details={"crawled_pages": res.get("crawled_pages", 0), "new_concepts": len(res.get("new_concepts", []))},
        result_count=len(res.get("new_concepts", [])),
        workspace=res.get("workspace", "default")
    )

    if getattr(args, "json", False):
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return

    if res.get("error"):
        print(f"[-] {res['error']}", file=sys.stderr)
        sys.exit(1)

    print(f"[+] {res['message']}")
    if res.get("new_concepts"):
        print("\n    Synthesized Concepts:")
        for nc in res["new_concepts"]:
            print(f"      * `{nc['id']}` ({nc['name']}) [{nc['type']}]")
        print("\n    Explore new links with `llmwiki graph " + cid + " --hops 2`")


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

    # 1. PageRank hubs query
    if getattr(args, "pagerank", False):
        hubs = graph.get_central_concepts(top_k=getattr(args, "top_k", 10))
        if getattr(args, "json", False):
            print(json.dumps(hubs, ensure_ascii=False, indent=2))
            return
        print(f"[+] Top {len(hubs)} Core Hub Concepts (PageRank):")
        for i, h in enumerate(hubs, 1):
            print(f"  [{i}] {h['name']} (`{h['concept_id']}`) | Score: {h['score']:.4f} | Degree: {h['degree']}")
        return

    # 2. Bridge concepts query
    if getattr(args, "bridges", False):
        bridges = graph.get_bridges(top_k=getattr(args, "top_k", 5))
        if getattr(args, "json", False):
            print(json.dumps(bridges, ensure_ascii=False, indent=2))
            return
        print(f"[+] Top {len(bridges)} Knowledge Bridges (Betweenness Centrality):")
        for i, b in enumerate(bridges, 1):
            print(f"  [{i}] {b['name']} (`{b['concept_id']}`) | Betweenness: {b['betweenness']:.4f} | Degree: {b['degree']}")
        return

    # 3. Community detection query (GraphRAG-style)
    if getattr(args, "communities", False):
        comms = graph.detect_communities()
        if getattr(args, "json", False):
            print(json.dumps(comms, ensure_ascii=False, indent=2))
            return
        print(f"[+] Detected {len(comms)} Knowledge Communities (Thematic Clusters):")
        for c in comms:
            members = ", ".join([f"`{m['id']}`" for m in c["concepts"][:5]])
            suffix = f" ... (+{c['size'] - 5} more)" if c["size"] > 5 else ""
            print(f"\n  * Community `{c['community_id']}` (Size: {c['size']}, Hub: `{c['hub_concept']}`):")
            print(f"    Concepts: {members}{suffix}")
        return

    # 4. Orphan nodes query
    if getattr(args, "orphans", False):
        orphans = graph.get_orphans()
        if getattr(args, "json", False):
            print(json.dumps(orphans, ensure_ascii=False, indent=2))
            return
        print(f"[+] Found {len(orphans)} Isolated/Orphan Concept(s):")
        for o in orphans:
            print(f"  * {o['name']} (`{o['concept_id']}`)")
        return

    # 5. Shortest reasoning path between two concepts
    if getattr(args, "path", None):
        target_id = args.path
        start_id = args.concept_id
        if not start_id:
            print("[-] Please provide the starting concept_id to find path.", file=sys.stderr)
            sys.exit(1)
        path = graph.find_path(start_id, target_id)
        if getattr(args, "json", False):
            print(json.dumps({"from": start_id, "to": target_id, "path": path}, ensure_ascii=False, indent=2))
            return
        if not path:
            print(f"[-] No knowledge path found between `{start_id}` and `{target_id}`.")
            return
        print(f"[+] Reasoning Path from `{start_id}` to `{target_id}` ({len(path)} step(s)):")
        for step in path:
            reason = f" ({step['reason']})" if step.get("reason") else ""
            print(f"  `{step['from']}` ==[{step['relation']}]==> `{step['to']}`{reason}")
        return

    # 6. Default: Neighbors of concept_id
    cid = args.concept_id
    if not cid:
        # Default to showing global graph metrics and top hubs
        metrics = graph.get_metrics()
        hubs = graph.get_central_concepts(top_k=5)
        if getattr(args, "json", False):
            print(json.dumps({"metrics": metrics, "top_hubs": hubs}, ensure_ascii=False, indent=2))
            return
        print("[+] Knowledge Graph Overview:")
        print(f"    Nodes: {metrics['node_count']} | Edges: {metrics['edge_count']} | Density: {metrics['density']}")
        print(f"    Connected Components: {metrics['connected_components']} | Avg Degree: {metrics['avg_degree']}")
        if hubs:
            print("\n    Top Hub Concepts:")
            for h in hubs:
                print(f"      * {h['name']} (`{h['concept_id']}`) [degree: {h['degree']}]")
        print("\n    Tip: Run `llmwiki graph <concept_id>` to view links, or `--communities` / `--pagerank`.")
        return

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
    target_ws = getattr(args, "workspace", None)
    active_ws = get_active_workspace()
    stats = store.db.stats(workspace=target_ws)
    raw_docs = store.raw.list_all(limit=5)
    graph = KnowledgeGraph(db=store.db)
    metrics = graph.get_metrics()
    top_hubs = graph.get_central_concepts(top_k=3)
    workspaces = store.list_workspaces()

    if getattr(args, "json", False):
        stats["recent_sources"] = raw_docs
        stats["graph_metrics"] = metrics
        stats["top_hubs"] = top_hubs
        stats["active_workspace"] = active_ws
        stats["workspaces"] = [w.to_dict() for w in workspaces]
        print(json.dumps(stats, ensure_ascii=False, indent=2))
        return

    ws_header = f" (Workspace: '{target_ws}')" if target_ws else ""
    print(f"[+] LLMWiki 3-Tier Warehouse Statistics{ws_header}:")
    print(f"    - Active Workspace: `{active_ws}` (Total Workspaces: {len(workspaces)})")
    print(f"    - Layer 1 Raw Documents: {stats['total_sources']}")
    print(f"    - Layer 2 Atomic Concepts: {stats['total_concepts']}")
    print(f"    - Layer 3 Knowledge Graph: {metrics['edge_count']} edges (Density: {metrics['density']}, Avg Degree: {metrics['avg_degree']})")
    print(f"    - Total Agent Query Logs: {stats['total_queries']}")
    if top_hubs:
        hub_names = [f"`{h['concept_id']}` ({h['name']})" for h in top_hubs]
        print(f"    - Core Hub Concepts (PageRank): {', '.join(hub_names)}")
    if raw_docs:
        print("\n    Recent Ingested Sources:")
        for r in raw_docs:
            print(f"      * [{r['id']}] {r['title']} ({r['char_count']} chars)")


def cmd_workspace(args):
    store = Store()
    action = args.ws_action
    active_ws = get_active_workspace()

    if action == "list":
        workspaces = store.list_workspaces()
        ws_data = []
        for ws in workspaces:
            st = store.db.stats(workspace=ws.id)
            is_active = (ws.id == active_ws)
            ws_data.append({
                "id": ws.id,
                "name": ws.name,
                "description": ws.description,
                "concepts": st["total_concepts"],
                "active": is_active,
                "created_at": ws.created_at
            })

        if getattr(args, "json", False):
            print(json.dumps(ws_data, ensure_ascii=False, indent=2))
            return

        print("[+] LLMWiki Workspaces:\n")
        for w in ws_data:
            marker = "* " if w["active"] else "  "
            desc_str = f" - {w['description']}" if w["description"] else ""
            print(f"{marker}`{w['id']}` ({w['name'] or 'Unnamed'}) [{w['concepts']} concepts]{desc_str}")
        print(f"\n    Active: `{active_ws}` (Switch with `llmwiki workspace use <id>`)")

    elif action == "create":
        ws = store.create_workspace(
            workspace_id=args.id,
            name=getattr(args, "name", "") or "",
            description=getattr(args, "desc", "") or ""
        )
        if getattr(args, "json", False):
            print(json.dumps(ws.to_dict(), ensure_ascii=False, indent=2))
            return
        print(f"[+] Workspace `{ws.id}` created successfully ({ws.name}).")

    elif action == "use":
        target = args.id.strip()
        ws = store.get_workspace(target)
        if not ws and target != "default":
            ws = store.create_workspace(target, name=target.capitalize())
            print(f"[*] Workspace `{target}` did not exist; created automatically.")
        new_active = set_active_workspace(target)
        if getattr(args, "json", False):
            print(json.dumps({"active_workspace": new_active}, ensure_ascii=False))
            return
        print(f"[+] Switched active workspace to `{new_active}`.")

    elif action == "current":
        if getattr(args, "json", False):
            print(json.dumps({"active_workspace": active_ws}, ensure_ascii=False))
            return
        print(f"[+] Current active workspace: `{active_ws}`")

    elif action == "delete":
        target = args.id.strip()
        if target == "default":
            print("[-] Cannot delete the default workspace.", file=sys.stderr)
            sys.exit(1)
        success = store.delete_workspace(target)
        if target == active_ws:
            set_active_workspace("default")
        if getattr(args, "json", False):
            print(json.dumps({"deleted": target, "success": success}, ensure_ascii=False))
            return
        if success:
            print(f"[+] Workspace `{target}` deleted. (Active reverted to 'default')")
        else:
            print(f"[-] Workspace `{target}` not found.")

    elif action == "sync":
        store.vectors.sync_all_workspace_vectors()
        if getattr(args, "json", False):
            print(json.dumps({"status": "synced"}, ensure_ascii=False))
            return
        print("[+] All workspace representative centroid vectors synchronized.")

    elif action == "route":
        routed_ws, score = store.route_workspace(args.query)
        if getattr(args, "json", False):
            print(json.dumps({"query": args.query, "recommended_workspace": routed_ws, "score": score}, ensure_ascii=False))
            return
        print(f"[+] Query Semantic Routing for: '{args.query}'")
        if routed_ws:
            print(f"    -> Best-matching workspace: `{routed_ws}` (similarity: {score:.4f})")
        else:
            print("    -> No matching workspace found (using global search).")


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
    target_ws = getattr(args, "workspace", None)
    data = store.db.get_full_graph_data(workspace=target_ws)
    print(json.dumps(data, ensure_ascii=False, indent=2 if getattr(args, "pretty", False) else None))

def cmd_list_concepts(args):
    store = Store()
    target_ws = getattr(args, "workspace", None)
    concepts = store.list_concepts(limit=args.limit, workspace=target_ws)
    if getattr(args, "json", False):
        print(json.dumps(concepts, ensure_ascii=False, indent=2))
        return
    ws_note = f" (Workspace: {target_ws})" if target_ws else ""
    print(f"[+] Concepts{ws_note}:")
    for c in concepts:
        ws_badge = f" [{c.get('workspace', 'default')}]" if c.get("workspace") else ""
        print(f"- `{c['id']}`{ws_badge}: {c['name']} [{c['type']}] ({c['summary'][:80]}...)")


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
    from .installer import install_antigravity_skill, install_antigravity_mcp, install_claude_mcp, install_codex_mcp, install_cli_symlink
    results = {}
    install_all = args.all or not (args.antigravity or args.claude or args.codex or args.symlink)
    if args.antigravity or install_all:
        skill_res = install_antigravity_skill()
        mcp_res = install_antigravity_mcp()
        results["antigravity"] = bool(skill_res or mcp_res)
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
    p_ingest.add_argument("--depth", type=int, default=1, help="Web exploration depth (default: 1)")
    p_ingest.add_argument("--max-pages", type=int, default=8, help="Maximum web pages per starting URL (default: 8, 0 for unlimited)")
    p_ingest.add_argument("--greedy", action="store_true", help="Greedy unbounded exploration of all connected links")
    p_ingest.add_argument("--workspace", "-w", default=None, help="Target workspace for ingested concepts (defaults to active workspace)")
    p_ingest.add_argument("--json", action="store_true", help="Output JSON format")

    # Deep dive
    p_deep_dive = subparsers.add_parser("deep-dive", help="Autonomous web deep-dive: crawl and synthesize knowledge around a concept")
    p_deep_dive.add_argument("concept_id", help="Target concept ID to expand")
    p_deep_dive.add_argument("--depth", type=int, default=1, help="Web crawl depth (default: 1)")
    p_deep_dive.add_argument("--max-pages", type=int, default=5, help="Maximum web pages to crawl (default: 5)")
    p_deep_dive.add_argument("--greedy", action="store_true", help="Greedy unbounded web exploration")
    p_deep_dive.add_argument("--query", "-q", default=None, help="Supplemental search keywords")
    p_deep_dive.add_argument("--workspace", "-w", default=None, help="Target workspace")
    p_deep_dive.add_argument("--caller", default="cli", help="Caller identity for logging")
    p_deep_dive.add_argument("--json", action="store_true", help="Output JSON format")


    # Search
    p_search = subparsers.add_parser("search", help="Search the knowledge warehouse")
    p_search.add_argument("query", help="Keywords or semantic query")
    p_search.add_argument(
        "--mode",
        choices=["hybrid", "semantic", "keyword", "graph", "hipporag"],
        default="hybrid",
        help="Search mode: hybrid (RRF), semantic (Vector), keyword (FTS5), graph/hipporag (Personalized PageRank)"
    )
    p_search.add_argument("--workspace", "-w", default="auto", help="Workspace filter: 'auto' (vector routing), 'all' (all workspaces), or specific ID")
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
    p_graph = subparsers.add_parser("graph", help="Explore concept relations, communities, and topology in the knowledge graph")
    p_graph.add_argument("concept_id", nargs="?", default=None, help="Starting concept ID (optional for global graph queries)")
    p_graph.add_argument("--hops", type=int, default=1, help="Hop depth for neighborhood search")
    p_graph.add_argument("--pagerank", action="store_true", help="List core hub concepts by PageRank centrality")
    p_graph.add_argument("--bridges", action="store_true", help="List knowledge bridges by Betweenness centrality")
    p_graph.add_argument("--communities", action="store_true", help="Detect thematic knowledge clusters (GraphRAG)")
    p_graph.add_argument("--orphans", action="store_true", help="List unlinked/isolated concepts")
    p_graph.add_argument("--path", type=str, default=None, help="Find shortest knowledge reasoning path to target concept ID")
    p_graph.add_argument("--top-k", type=int, default=10, help="Max items for ranking queries")
    p_graph.add_argument("--json", action="store_true", help="Output JSON format")


    # Graph-Data (Full JSON for Electron/D3/Cytoscape)
    p_graph_data = subparsers.add_parser("graph-data", help="Export full nodes and edges in JSON")
    p_graph_data.add_argument("--workspace", "-w", default=None, help="Scope graph data to workspace")
    p_graph_data.add_argument("--pretty", action="store_true", help="Pretty print JSON")

    # Workspace
    p_ws = subparsers.add_parser("workspace", help="Manage multi-workspace knowledge domains and semantic routing")
    ws_sub = p_ws.add_subparsers(dest="ws_action", required=True)

    p_ws_list = ws_sub.add_parser("list", help="List all workspaces")
    p_ws_list.add_argument("--json", action="store_true", help="Output JSON format")

    p_ws_create = ws_sub.add_parser("create", help="Create a new workspace")
    p_ws_create.add_argument("id", help="Workspace ID (alphanumeric and underscores)")
    p_ws_create.add_argument("--name", help="Display name for workspace")
    p_ws_create.add_argument("--desc", help="Description of workspace knowledge domain")
    p_ws_create.add_argument("--json", action="store_true", help="Output JSON format")

    p_ws_use = ws_sub.add_parser("use", help="Set active default workspace")
    p_ws_use.add_argument("id", help="Workspace ID to activate")
    p_ws_use.add_argument("--json", action="store_true", help="Output JSON format")

    p_ws_cur = ws_sub.add_parser("current", help="Show currently active workspace")
    p_ws_cur.add_argument("--json", action="store_true", help="Output JSON format")

    p_ws_del = ws_sub.add_parser("delete", help="Delete a workspace")
    p_ws_del.add_argument("id", help="Workspace ID to delete")
    p_ws_del.add_argument("--json", action="store_true", help="Output JSON format")

    p_ws_sync = ws_sub.add_parser("sync", help="Sync representative centroid vectors of all workspaces")
    p_ws_sync.add_argument("--json", action="store_true", help="Output JSON format")

    p_ws_route = ws_sub.add_parser("route", help="Test semantic routing for a query against workspaces")
    p_ws_route.add_argument("query", help="Query string to route")
    p_ws_route.add_argument("--json", action="store_true", help="Output JSON format")

    # List concepts
    p_list_c = subparsers.add_parser("list-concepts", help="List all concepts")
    p_list_c.add_argument("--limit", type=int, default=100, help="Max items")
    p_list_c.add_argument("--workspace", "-w", default=None, help="Filter by workspace ID or 'all'")
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
    p_stats.add_argument("--workspace", "-w", default=None, help="Scope statistics to workspace")
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
    elif args.command == "deep-dive":
        cmd_deep_dive(args)
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
    elif args.command == "workspace":
        cmd_workspace(args)
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
