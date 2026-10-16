"""
graph_store.py — FalkorDB client with fallback in-memory graph simulator.
Supports:
1. Idempotent entity node upsertion (is_placeholder tracking).
2. Edge reconciliation (detecting & closing/removing stale edges on reassignment).
3. Automatic resolution of pending foreign-keys when parents are uploaded.
4. Dependency-aware 1-hop graph neighborhood queries.
5. Production FalkorDB with seamless in-memory fallback if Redis/Docker is not running locally.
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from .config import DATA_DIR, logger
from .schema import FK_TO_RELATIONSHIP, SCHEMA_CONFIG


class GraphStore:
    """
    Graph storage layer for Company Brain.
    Connects to live FalkorDB instance when available, or seamlessly uses
    an in-memory graph engine for testing and local development.
    """

    def __init__(self, host: str = "localhost", port: int = 6379, graph_name: str = "CompanyBrain01"):
        self.host = host
        self.port = port
        self.graph_name = graph_name
        self.client = None
        self.graph = None
        self.is_connected = False
        self.in_memory_nodes: Dict[str, Dict[str, Any]] = {}  # id -> {label, properties, is_placeholder, ...}
        self.in_memory_edges: List[Dict[str, Any]] = []      # list of {id, src, rel, tgt, valid_from, valid_to, is_active}
        self.storage_file = DATA_DIR / "graph_store_backup.json"
        self.load_from_disk()

    def load_from_disk(self):
        """Load in-memory graph snapshot from disk cache if present."""
        if self.storage_file.exists():
            try:
                import json
                with open(self.storage_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.in_memory_nodes = data.get("nodes", {})
                self.in_memory_edges = data.get("edges", [])
            except Exception as e:
                logger.warning(f"Could not load graph cache from disk: {e}")

    def save_to_disk(self):
        """Persist in-memory graph snapshot to disk cache."""
        try:
            import json
            with open(self.storage_file, "w", encoding="utf-8") as f:
                json.dump({
                    "nodes": self.in_memory_nodes,
                    "edges": self.in_memory_edges,
                }, f, indent=2)
        except Exception as e:
            logger.warning(f"Could not save graph cache to disk: {e}")

    def connect(self) -> bool:
        """Attempt connection to FalkorDB. Fallback to in-memory graph on failure."""
        try:
            import falkordb
            self.client = falkordb.FalkorDB(host=self.host, port=self.port)
            self.graph = self.client.select_graph(self.graph_name)
            self.graph.query("RETURN 1")
            self.is_connected = True
            logger.info(f"Connected to FalkorDB on {self.host}:{self.port} (Graph: '{self.graph_name}')")
            return True
        except Exception as e:
            logger.warning(f"FalkorDB not reachable on {self.host}:{self.port} ({e}). Using robust In-Memory Graph Store.")
            self.is_connected = False
            return False

    def query(self, cypher: str, params: Optional[Dict[str, Any]] = None) -> Any:
        """Execute a Cypher query on FalkorDB if connected."""
        if self.is_connected and self.graph:
            return self.graph.query(cypher, params or {})
        return None

    def clear(self):
        """Reset the graph database."""
        if self.is_connected and self.graph:
            try:
                self.graph.delete()
                self.graph = self.client.select_graph(self.graph_name)
            except Exception as e:
                logger.warning(f"Error clearing FalkorDB graph: {e}")
        self.in_memory_nodes.clear()
        self.in_memory_edges.clear()
        if self.storage_file.exists():
            try:
                self.storage_file.unlink()
            except Exception:
                pass

    # ─────────────────────────────────────────────────────────────────────────
    # Step 2: Idempotent Entity Upsertion & Placeholder Management
    # ─────────────────────────────────────────────────────────────────────────

    def upsert_entity(
        self,
        entity_type: str,
        entity_id: str,
        properties: Dict[str, Any],
        is_placeholder: bool = False,
    ) -> Dict[str, Any]:
        """
        Idempotently upsert an entity node:
        - If node exists as a placeholder and full data arrives, promote it to a valid entity.
        - If node does not exist, create it with placeholder status flag.
        """
        now_iso = datetime.now(timezone.utc).isoformat()
        clean_props = {k: v for k, v in properties.items() if v is not None}

        # In-memory store update
        existing = self.in_memory_nodes.get(entity_id)
        if existing:
            was_placeholder = existing.get("is_placeholder", False)
            existing["properties"].update(clean_props)
            existing["updated_at"] = now_iso
            if was_placeholder and not is_placeholder:
                existing["is_placeholder"] = False
                existing["promoted_at"] = now_iso
                logger.info(f"Promoted placeholder entity [{entity_type}:{entity_id}] to fully validated entity.")
        else:
            self.in_memory_nodes[entity_id] = {
                "id": entity_id,
                "label": entity_type,
                "properties": clean_props,
                "is_placeholder": is_placeholder,
                "created_at": now_iso,
                "updated_at": now_iso,
            }

        # Live FalkorDB Cypher execution if connected
        if self.is_connected and self.graph:
            try:
                cypher = f"""
                MERGE (n:{entity_type} {{id: $id}})
                ON CREATE SET n += $props, n.is_placeholder = $is_placeholder, n.created_at = $now
                ON MATCH SET n += $props, 
                    n.is_placeholder = CASE WHEN n.is_placeholder = true AND $is_placeholder = false THEN false ELSE n.is_placeholder END,
                    n.updated_at = $now
                """
                self.graph.query(cypher, {
                    "id": entity_id,
                    "props": clean_props,
                    "is_placeholder": is_placeholder,
                    "now": now_iso,
                })
            except Exception as e:
                logger.error(f"FalkorDB upsert error for {entity_id}: {e}")

        return self.in_memory_nodes[entity_id]

    def upsert_entities_batch(self, records: List[Dict[str, Any]]) -> Dict[str, int]:
        """Batch upsert canonical entity records."""
        stats = {"inserted": 0, "updated": 0, "promoted": 0}
        for rec in records:
            entity_id = rec["entity_id"]
            entity_type = rec["entity_type"]
            props = rec.get("properties", {})
            was_in = entity_id in self.in_memory_nodes
            node = self.upsert_entity(entity_type, entity_id, props, is_placeholder=False)
            if was_in:
                stats["updated"] += 1
            else:
                stats["inserted"] += 1
        self.save_to_disk()
        return stats

    # ─────────────────────────────────────────────────────────────────────────
    # Step 2: Edge Reconciliation (Closing Stale Edges on Reassignment)
    # ─────────────────────────────────────────────────────────────────────────

    def reconcile_relationship(
        self,
        source_id: str,
        source_type: str,
        relationship_type: str,
        new_target_id: str,
        target_type: str,
        evidence_text: Optional[str] = None,
        valid_from: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Reconcile an edge:
        - If source entity already has an outgoing relationship of this type to a DIFFERENT target:
          mark previous edge as inactive / closed (stale edge reconciliation).
        - Connect source to new_target_id.
        - If new_target_id doesn't exist yet, create a placeholder node for it.
        """
        now_iso = datetime.now(timezone.utc).isoformat()
        
        # 1. Ensure target node exists (as placeholder if missing)
        if new_target_id not in self.in_memory_nodes:
            self.upsert_entity(target_type, new_target_id, {}, is_placeholder=True)

        stale_edges_closed = []

        # 2. Reconcile existing edges of this type for single-parent / functional relations
        # E.g. BELONGS_TO, MANAGED_BY, ASSIGNED_TO
        SINGLE_TARGET_RELATIONS = {
            "BELONGS_TO", "MANAGED_BY", "REPORTS_TO", "ASSIGNED_TO",
            "LED_BY", "OWNS", "SPONSORED_BY", "PRIMARILY_DELIVERED_BY",
            "CHILD_OF", "OWNED_BY_TEAM"
        }

        if relationship_type in SINGLE_TARGET_RELATIONS:
            for edge in self.in_memory_edges:
                if (
                    edge["src"] == source_id 
                    and edge["rel"] == relationship_type 
                    and edge["is_active"] 
                    and edge["tgt"] != new_target_id
                ):
                    edge["is_active"] = False
                    edge["valid_to"] = now_iso
                    edge["closed_reason"] = "REASSIGNED"
                    stale_edges_closed.append(edge["tgt"])
                    logger.info(f"Reconciled stale edge: ({source_id})-[:{relationship_type}]->({edge['tgt']}) closed.")

        # 3. Create or reactivate the new relationship
        existing_edge = next(
            (e for e in self.in_memory_edges if e["src"] == source_id and e["rel"] == relationship_type and e["tgt"] == new_target_id),
            None
        )

        if existing_edge:
            existing_edge["is_active"] = True
            existing_edge["evidence_text"] = evidence_text or existing_edge.get("evidence_text")
        else:
            self.in_memory_edges.append({
                "id": f"rel_{source_id}_{relationship_type}_{new_target_id}",
                "src": source_id,
                "src_type": source_type,
                "rel": relationship_type,
                "tgt": new_target_id,
                "tgt_type": target_type,
                "valid_from": valid_from or now_iso,
                "valid_to": None,
                "is_active": True,
                "evidence_text": evidence_text,
            })

        # Cypher execution if connected to FalkorDB
        if self.is_connected and self.graph:
            try:
                # Close stale edges if applicable
                if relationship_type in SINGLE_TARGET_RELATIONS:
                    close_cypher = f"""
                    MATCH (a {{id: $src}})-[r:{relationship_type}]->(old_b)
                    WHERE old_b.id <> $new_tgt
                    DELETE r
                    """
                    self.graph.query(close_cypher, {"src": source_id, "new_tgt": new_target_id})

                # Create target placeholder if needed
                self.graph.query(
                    f"MERGE (b:{target_type} {{id: $tgt}}) ON CREATE SET b.is_placeholder = true",
                    {"tgt": new_target_id}
                )

                # Merge active edge
                merge_edge_cypher = f"""
                MATCH (a:{source_type} {{id: $src}}), (b:{target_type} {{id: $tgt}})
                MERGE (a)-[r:{relationship_type}]->(b)
                SET r.is_active = true, r.valid_from = $vf
                """
                self.graph.query(merge_edge_cypher, {
                    "src": source_id,
                    "tgt": new_target_id,
                    "vf": valid_from or now_iso,
                })
            except Exception as e:
                logger.error(f"Error in FalkorDB edge reconciliation: {e}")

        self.save_to_disk()

        return {
            "source_id": source_id,
            "target_id": new_target_id,
            "relationship_type": relationship_type,
            "stale_targets_closed": stale_edges_closed,
        }

    # ─────────────────────────────────────────────────────────────────────────
    # Step 3: Dependency-Aware 1-Hop Neighborhood Traversal
    # ─────────────────────────────────────────────────────────────────────────

    def get_neighbors(self, entity_id: str, direction: str = "both") -> List[Dict[str, Any]]:
        """
        Fetch 1-hop active graph neighbors for an entity:
        Returns list of {neighbor_id, neighbor_label, neighbor_name, relationship, direction}.
        """
        neighbors = []

        # In-memory query
        for e in self.in_memory_edges:
            if not e.get("is_active", True):
                continue

            if e["src"] == entity_id and direction in ("outgoing", "both"):
                tgt_node = self.in_memory_nodes.get(e["tgt"], {})
                neighbors.append({
                    "neighbor_id": e["tgt"],
                    "neighbor_label": e.get("tgt_type", tgt_node.get("label", "Entity")),
                    "neighbor_name": tgt_node.get("properties", {}).get("display_name") 
                                     or tgt_node.get("properties", {}).get("name") 
                                     or e["tgt"],
                    "relationship": e["rel"],
                    "direction": "outgoing",
                })
            elif e["tgt"] == entity_id and direction in ("incoming", "both"):
                src_node = self.in_memory_nodes.get(e["src"], {})
                neighbors.append({
                    "neighbor_id": e["src"],
                    "neighbor_label": e.get("src_type", src_node.get("label", "Entity")),
                    "neighbor_name": src_node.get("properties", {}).get("display_name") 
                                     or src_node.get("properties", {}).get("name") 
                                     or e["src"],
                    "relationship": e["rel"],
                    "direction": "incoming",
                })

        return neighbors

    def get_stats(self) -> Dict[str, Any]:
        """Return total nodes, relationships, and placeholder count."""
        active_edges = [e for e in self.in_memory_edges if e.get("is_active", True)]
        placeholders = [nid for nid, n in self.in_memory_nodes.items() if n.get("is_placeholder", False)]
        return {
            "total_nodes": len(self.in_memory_nodes),
            "total_relationships": len(active_edges),
            "placeholder_nodes": len(placeholders),
            "placeholder_ids": placeholders,
            "is_live_falkordb": self.is_connected,
        }
