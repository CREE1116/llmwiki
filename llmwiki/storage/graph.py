"""
Knowledge Graph engine for LLMWiki.
Inspired by state-of-the-art GraphRAG (Microsoft) and HippoRAG (NeurIPS 2024):
- Personalized PageRank (PPR) spreading activation for multi-hop associative retrieval
- Modularity-based Community Detection for thematic clustering
- Centrality & Bridge analysis (PageRank, Betweenness)
- Path discovery, k-hop subgraph extraction, and orphan node detection
"""
import networkx as nx
import numpy as np
from typing import List, Dict, Any, Optional, Tuple, Set
from .db import Database


def _power_iteration_pagerank(
    G: nx.Graph,
    alpha: float = 0.85,
    personalization: Optional[Dict[str, float]] = None,
    max_iter: int = 100,
    tol: float = 1e-6
) -> Dict[str, float]:
    """
    Pure NumPy power iteration PageRank / Personalized PageRank.
    Zero dependency on scipy, ultra-fast and deterministic across platforms.
    """
    nodes = list(G.nodes())
    N = len(nodes)
    if N == 0:
        return {}
    if N == 1:
        return {nodes[0]: 1.0}

    node_idx = {nid: i for i, nid in enumerate(nodes)}

    # Personalization vector p
    if personalization is None:
        p = np.full(N, 1.0 / N, dtype=np.float64)
    else:
        p = np.zeros(N, dtype=np.float64)
        for nid, val in personalization.items():
            if nid in node_idx:
                p[node_idx[nid]] = max(0.0, float(val))
        sum_p = np.sum(p)
        if sum_p > 0:
            p /= sum_p
        else:
            p = np.full(N, 1.0 / N, dtype=np.float64)

    # Adjacency list and out-degrees
    out_degrees = np.zeros(N, dtype=np.float64)
    adj: List[List[int]] = [[] for _ in range(N)]
    for u, v in G.edges():
        if u in node_idx and v in node_idx:
            i, j = node_idx[u], node_idx[v]
            adj[i].append(j)
            out_degrees[i] += 1.0
            if not G.is_directed():
                adj[j].append(i)
                out_degrees[j] += 1.0

    x = p.copy()
    for _ in range(max_iter):
        xlast = x.copy()
        x = np.zeros(N, dtype=np.float64)

        danglesum = 0.0
        for i in range(N):
            if out_degrees[i] == 0:
                danglesum += xlast[i]
            else:
                share = xlast[i] / out_degrees[i]
                for j in adj[i]:
                    x[j] += share

        x = alpha * (x + danglesum * p) + (1.0 - alpha) * p

        if np.sum(np.abs(x - xlast)) < N * tol:
            break

    total = np.sum(x)
    if total > 0:
        x /= total

    return {nodes[i]: float(x[i]) for i in range(N)}



class KnowledgeGraph:
    def __init__(self, db: Optional[Database] = None):
        self.db = db or Database()
        self.graph = nx.DiGraph()
        self.build_graph()

    def build_graph(self):
        """Build in-memory graph from SQLite relations."""
        self.graph.clear()
        with self.db.get_connection() as conn:
            # Add nodes
            cursor = conn.execute("SELECT id, name, type FROM concepts")
            for row in cursor.fetchall():
                self.graph.add_node(row["id"], name=row["name"], type=row["type"])

            # Add edges
            cursor = conn.execute("SELECT source_id, relation_type, target_id, reason FROM relations")
            for row in cursor.fetchall():
                # Add edge with weight and metadata
                self.graph.add_edge(
                    row["source_id"],
                    row["target_id"],
                    relation=row["relation_type"],
                    reason=row["reason"] or "",
                    weight=1.0
                )

    def get_neighbors(self, concept_id: str, hops: int = 1) -> Dict[str, Any]:
        """
        Get 1 or 2-hop connected concepts with directional edge details.
        Optimized for prompt context injection and agent reasoning.
        """
        if concept_id not in self.graph:
            self.build_graph()
            if concept_id not in self.graph:
                return {"concept_id": concept_id, "connections": []}

        connections = []
        # Outgoing
        for target in self.graph.successors(concept_id):
            edge_data = self.graph.get_edge_data(concept_id, target)
            connections.append({
                "direction": "outgoing",
                "relation": edge_data.get("relation", "related_to"),
                "target_id": target,
                "target_name": self.graph.nodes[target].get("name", target),
                "reason": edge_data.get("reason", "")
            })

        # Incoming
        for source in self.graph.predecessors(concept_id):
            edge_data = self.graph.get_edge_data(source, concept_id)
            connections.append({
                "direction": "incoming",
                "relation": edge_data.get("relation", "related_to"),
                "source_id": source,
                "source_name": self.graph.nodes[source].get("name", source),
                "reason": edge_data.get("reason", "")
            })

        return {
            "concept_id": concept_id,
            "connections": connections
        }

    def personalized_pagerank(
        self,
        seed_weights: Dict[str, float],
        alpha: float = 0.85,
        max_iter: int = 100
    ) -> Dict[str, float]:
        """
        HippoRAG-style Personalized PageRank (PPR) spreading activation.
        Propagates semantic relevance from seed concepts across the entire knowledge graph,
        uncovering latent multi-hop associations without requiring direct text keyword match.
        """
        if not self.graph.nodes:
            self.build_graph()
        if not self.graph.nodes:
            return {}

        valid_seeds = {nid: max(0.0, float(w)) for nid, w in seed_weights.items() if nid in self.graph}
        undirected_view = self.graph.to_undirected()
        return _power_iteration_pagerank(
            undirected_view,
            alpha=alpha,
            personalization=valid_seeds if valid_seeds else None,
            max_iter=max_iter
        )

    def get_central_concepts(self, top_k: int = 10) -> List[Dict[str, Any]]:
        """
        Find global core hub concepts using standard PageRank centrality.
        """
        if not self.graph.nodes:
            self.build_graph()
        if not self.graph.nodes:
            return []

        undirected_view = self.graph.to_undirected()
        scores = _power_iteration_pagerank(undirected_view, alpha=0.85)
        sorted_nodes = sorted(scores.items(), key=lambda x: x[1], reverse=True)[:top_k]
        return [
            {
                "concept_id": nid,
                "name": self.graph.nodes[nid].get("name", nid),
                "type": self.graph.nodes[nid].get("type", "concept"),
                "score": round(score, 5),
                "degree": self.graph.degree(nid)
            }
            for nid, score in sorted_nodes
        ]

    def get_bridges(self, top_k: int = 5) -> List[Dict[str, Any]]:

        """
        Find bridge concepts connecting disparate knowledge clusters
        using Betweenness Centrality.
        """
        if len(self.graph.nodes) < 3:
            return []

        try:
            undirected_view = self.graph.to_undirected()
            betweenness = nx.betweenness_centrality(undirected_view)
            sorted_nodes = sorted(betweenness.items(), key=lambda x: x[1], reverse=True)[:top_k]
            return [
                {
                    "concept_id": nid,
                    "name": self.graph.nodes[nid].get("name", nid),
                    "betweenness": round(score, 5),
                    "degree": self.graph.degree(nid)
                }
                for nid, score in sorted_nodes if score > 0
            ]
        except Exception:
            return []

    def detect_communities(self) -> List[Dict[str, Any]]:
        """
        GraphRAG-style Community Detection using greedy modularity maximization.
        Groups atomic concepts into cohesive functional clusters/themes.
        """
        if len(self.graph.nodes) < 2:
            return []

        undirected_view = self.graph.to_undirected()
        try:
            import networkx.algorithms.community as nx_comm
            communities = list(nx_comm.greedy_modularity_communities(undirected_view))

            results = []
            for i, comm in enumerate(communities, 1):
                nodes_in_comm = list(comm)
                # Find hub node in this community (highest internal degree)
                subgraph = self.graph.subgraph(nodes_in_comm)
                hub_node = max(nodes_in_comm, key=lambda n: subgraph.degree(n)) if nodes_in_comm else ""
                hub_name = self.graph.nodes[hub_node].get("name", hub_node) if hub_node else ""

                results.append({
                    "community_id": f"cluster_{i}",
                    "size": len(nodes_in_comm),
                    "hub_concept": hub_node,
                    "hub_name": hub_name,
                    "concepts": [
                        {
                            "id": nid,
                            "name": self.graph.nodes[nid].get("name", nid),
                            "type": self.graph.nodes[nid].get("type", "concept")
                        }
                        for nid in nodes_in_comm
                    ]
                })

            results.sort(key=lambda x: x["size"], reverse=True)
            return results
        except Exception:
            return []

    def get_orphans(self) -> List[Dict[str, str]]:
        """
        Find isolated concepts that have zero incoming or outgoing relations.
        Useful for proactive knowledge linking.
        """
        if not self.graph.nodes:
            self.build_graph()

        orphans = []
        for nid in self.graph.nodes:
            if self.graph.degree(nid) == 0:
                orphans.append({
                    "concept_id": nid,
                    "name": self.graph.nodes[nid].get("name", nid),
                    "type": self.graph.nodes[nid].get("type", "concept")
                })
        return orphans

    def get_subgraph(self, concept_id: str, hops: int = 2) -> Dict[str, Any]:
        """
        Extract a localized k-hop ego subgraph around a concept.
        Returns node set and edge set for visualization or targeted LLM prompting.
        """
        if concept_id not in self.graph:
            self.build_graph()
            if concept_id not in self.graph:
                return {"nodes": [], "edges": []}

        # Collect nodes within k hops (undirected)
        undirected_view = self.graph.to_undirected()
        lengths = nx.single_source_shortest_path_length(undirected_view, concept_id, cutoff=hops)
        sub_nodes = set(lengths.keys())

        # Extract induced subgraph
        nodes = []
        for nid in sub_nodes:
            nodes.append({
                "id": nid,
                "name": self.graph.nodes[nid].get("name", nid),
                "type": self.graph.nodes[nid].get("type", "concept"),
                "distance": lengths[nid]
            })

        edges = []
        for u in sub_nodes:
            for v in self.graph.successors(u):
                if v in sub_nodes:
                    data = self.graph.get_edge_data(u, v)
                    edges.append({
                        "source": u,
                        "target": v,
                        "relation": data.get("relation", "related_to"),
                        "reason": data.get("reason", "")
                    })

        return {"center": concept_id, "hops": hops, "nodes": nodes, "edges": edges}

    def find_path(self, start_id: str, end_id: str) -> Optional[List[Dict[str, Any]]]:
        """Find the shortest knowledge path between two concepts."""
        self.build_graph()
        try:
            path = nx.shortest_path(self.graph.to_undirected(), source=start_id, target=end_id)
            steps = []
            for i in range(len(path) - 1):
                u, v = path[i], path[i+1]
                if self.graph.has_edge(u, v):
                    edge = self.graph.get_edge_data(u, v)
                    steps.append({"from": u, "to": v, "relation": edge.get("relation", "related_to"), "reason": edge.get("reason", "")})
                elif self.graph.has_edge(v, u):
                    edge = self.graph.get_edge_data(v, u)
                    steps.append({"from": v, "to": u, "relation": edge.get("relation", "related_to"), "reason": edge.get("reason", "")})
            return steps
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            return None

    def get_metrics(self) -> Dict[str, Any]:
        """Compute high-level knowledge graph topology metrics."""
        if not self.graph.nodes:
            self.build_graph()

        n_nodes = self.graph.number_of_nodes()
        n_edges = self.graph.number_of_edges()
        density = nx.density(self.graph) if n_nodes > 1 else 0.0

        undirected_view = self.graph.to_undirected()
        num_components = nx.number_connected_components(undirected_view) if n_nodes > 0 else 0

        return {
            "node_count": n_nodes,
            "edge_count": n_edges,
            "density": round(density, 4),
            "connected_components": num_components,
            "avg_degree": round(2 * n_edges / n_nodes, 2) if n_nodes > 0 else 0.0
        }

