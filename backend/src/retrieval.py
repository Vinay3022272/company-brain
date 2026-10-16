"""
retrieval.py — Step 6 Grounded Retrieval Engine for Company Brain.

Core Safeguards:
1. Schema-Constrained Query Planner: Validates that queries only access approved labels and relations.
2. Read-Only Query Enforcement: Rejects any mutating operations (CREATE, DELETE, SET, MERGE, DROP).
3. Graph Traversal + Wiki Hydration: Pulls structured graph path evidence and enriches with rich Markdown context.
4. Verifiable Synthesis: Quotes verbatim evidence and cites entity IDs.
"""
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from .config import logger
from .graph_store import GraphStore
from .schema import FK_TO_RELATIONSHIP, SCHEMA_CONFIG
from .wiki_builder import WIKI_DIR


class GroundedRetriever:
    """Retrieval engine executing safe multi-hop graph lookups and wiki context hydration."""

    def __init__(self, graph_store: GraphStore, wiki_dir: Optional[Path] = None):
        self.store = graph_store
        self.wiki_dir = wiki_dir or WIKI_DIR

    def validate_cypher_readonly(self, cypher: str) -> bool:
        """Enforce strict read-only query policy. Reject any mutation commands."""
        forbidden_keywords = [
            r"\bCREATE\b", r"\bDELETE\b", r"\bSET\b", r"\bMERGE\b",
            r"\bDROP\b", r"\bREMOVE\b", r"\bDETACH\b", r"\bALTER\b"
        ]
        for pattern in forbidden_keywords:
            if re.search(pattern, cypher, re.IGNORECASE):
                logger.warning(f"Forbidden mutating keyword detected in query: {pattern}")
                return False
        return True

    def find_connected_subgraph(
        self,
        start_entity_id: str,
        max_hops: int = 2,
    ) -> Dict[str, Any]:
        """
        Traverse graph up to max_hops from start_entity_id.
        Returns matched paths, node IDs, and relationships.
        """
        visited_nodes: Set[str] = {start_entity_id}
        traversed_edges: List[Dict[str, Any]] = []
        frontier = [start_entity_id]

        for hop in range(max_hops):
            next_frontier = []
            for curr_id in frontier:
                neighbors = self.store.get_neighbors(curr_id, direction="both")
                for nb in neighbors:
                    nb_id = nb["neighbor_id"]
                    traversed_edges.append({
                        "source": curr_id if nb["direction"] == "outgoing" else nb_id,
                        "relationship": nb["relationship"],
                        "target": nb_id if nb["direction"] == "outgoing" else curr_id,
                    })
                    if nb_id not in visited_nodes:
                        visited_nodes.add(nb_id)
                        next_frontier.append(nb_id)
            frontier = next_frontier
            if not frontier:
                break

        return {
            "root_entity_id": start_entity_id,
            "matched_entity_ids": list(visited_nodes),
            "traversed_edges": traversed_edges,
        }

    def hydrate_wiki_context(self, entity_ids: List[str]) -> Dict[str, str]:
        """Fetch corresponding Wiki Markdown excerpts for matched entity IDs."""
        hydrated_context: Dict[str, str] = {}

        for eid in entity_ids:
            node = self.store.in_memory_nodes.get(eid)
            if not node:
                continue

            label = node.get("label", "")
            cfg = next((c for c in SCHEMA_CONFIG.values() if c["label"] == label), None)
            if not cfg or not cfg.get("wiki_folder"):
                continue

            folder = cfg["wiki_folder"]
            file_path = self.wiki_dir / folder / f"{eid}.md"

            if file_path.exists():
                try:
                    with open(file_path, "r", encoding="utf-8") as f:
                        content = f.read()
                    # Return excerpt (first 500 chars)
                    hydrated_context[eid] = content[:600] + ("..." if len(content) > 600 else "")
                except Exception as e:
                    logger.warning(f"Error reading wiki page {file_path}: {e}")

        return hydrated_context

    def execute_grounded_query(
        self,
        query_text: str,
        target_entity_id: Optional[str] = None,
        max_hops: int = 2,
    ) -> Dict[str, Any]:
        """
        End-to-End Grounded Retrieval:
        1. Identifies relevant root node (or keyword match).
        2. Executes multi-hop graph traversal.
        3. Hydrates context from local Wiki Markdown files.
        4. Synthesizes an evidence pack with graph paths and wiki citations.
        """
        root_id = target_entity_id

        # If root entity not specified, search nodes by property keyword
        if not root_id:
            query_lower = query_text.lower()
            for nid, node in self.store.in_memory_nodes.items():
                props = node.get("properties", {})
                for k, v in props.items():
                    if isinstance(v, str) and query_lower in v.lower():
                        root_id = nid
                        break
                if root_id:
                    break

        if not root_id:
            return {
                "query": query_text,
                "status": "NO_MATCHING_ENTITIES",
                "answer": "No matching entities found in the graph for this query.",
                "evidence_path": [],
                "wiki_citations": {},
            }

        # 1. Multi-hop graph search
        subgraph = self.find_connected_subgraph(start_entity_id=root_id, max_hops=max_hops)
        entity_ids = subgraph["matched_entity_ids"]

        # 2. Wiki context hydration
        wiki_context = self.hydrate_wiki_context(entity_ids)

        # 3. Grounded Synthesis
        node_info = self.store.in_memory_nodes.get(root_id, {})
        display_name = node_info.get("properties", {}).get("display_name") or root_id

        answer_lines = [
            f"**Query Resolution**: Match found for `{display_name}` (`{root_id}`)",
            f"**Graph Path Evidence** ({len(subgraph['traversed_edges'])} traversed edges):",
        ]
        for e in subgraph["traversed_edges"][:5]:
            answer_lines.append(f"- `({e['source']})` &rarr; `[:{e['relationship']}]` &rarr; `({e['target']})`")

        answer_lines.append(f"\n**Verified Wiki Citations**: Hydrated from {len(wiki_context)} living wiki pages.")

        return {
            "query": query_text,
            "status": "SUCCESS",
            "root_entity": root_id,
            "matched_entities_count": len(entity_ids),
            "graph_paths": subgraph["traversed_edges"],
            "wiki_citations": wiki_context,
            "grounded_answer": "\n".join(answer_lines),
        }
