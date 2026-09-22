"""
Knowledge Graph engine for LLMWiki.
Fast relation traversal, multi-hop context retrieval, and path discovery using NetworkX.
"""
import networkx as nx
from typing import List, Dict, Any, Optional
from .db import Database

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
                self.graph.add_edge(
                    row["source_id"],
                    row["target_id"],
                    relation=row["relation_type"],
                    reason=row["reason"] or ""
                )

    def get_neighbors(self, concept_id: str, hops: int = 1) -> Dict[str, Any]:
        """
        Get 1 or 2-hop connected concepts with directional edge details.
        Optimized for prompt context injection.
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
                    steps.append({"from": u, "to": v, "relation": edge["relation"], "reason": edge["reason"]})
                elif self.graph.has_edge(v, u):
                    edge = self.graph.get_edge_data(v, u)
                    steps.append({"from": v, "to": u, "relation": edge["relation"], "reason": edge["reason"]})
            return steps
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            return None
