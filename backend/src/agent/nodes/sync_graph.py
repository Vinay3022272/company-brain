"""
sync_graph.py — LangGraph node for Step 2 FalkorDB Synchronization & Edge Reconciliation.
Processes canonical records and relationships from state, reconciles edges, and manages placeholders.
"""
from typing import Any, Dict, List
from ...config import logger
from ...graph_store import GraphStore
from ..state import WikiState
from ...schema import FK_TO_RELATIONSHIP


# Shared graph store singleton across pipeline runs
_GRAPH_STORE_INSTANCE: GraphStore = None


def get_graph_store() -> GraphStore:
    """Retrieve or initialize the active GraphStore instance."""
    global _GRAPH_STORE_INSTANCE
    if _GRAPH_STORE_INSTANCE is None:
        _GRAPH_STORE_INSTANCE = GraphStore()
        _GRAPH_STORE_INSTANCE.connect()
    return _GRAPH_STORE_INSTANCE


def sync_falkordb_graph(state: WikiState) -> Dict[str, Any]:
    """
    LangGraph Node (Step 2):
    1. Ingests canonical entities into FalkorDB/GraphStore idempotently.
    2. Upserts foreign-key relationships and reconciles stale edges.
    3. Ingests explicit relationships from relationships.csv.
    4. Tracks resolved placeholder parent nodes.
    """
    logger.info("--- [LangGraph Node: sync_falkordb_graph] Executing Step 2 Graph Synchronization ---")

    store = get_graph_store()
    canonical_records = state.get("canonical_records", {})
    relationships_records = state.get("relationships_records", [])

    total_entities_upserted = 0
    reconciled_edges_count = 0
    stale_edges_closed = []

    # 1. Upsert Canonical Entities
    for label, records in canonical_records.items():
        res = store.upsert_entities_batch(records)
        total_entities_upserted += len(records)

    # 2. Reconcile Foreign Key Relationships
    for label, records in canonical_records.items():
        for rec in records:
            src_id = rec["entity_id"]
            src_type = rec["entity_type"]
            fks = rec.get("foreign_keys", {})

            for fk_col, fk_info in fks.items():
                if isinstance(fk_info, (list, tuple)) and len(fk_info) == 3:
                    target_type, target_pk, target_val = fk_info[0], fk_info[1], fk_info[2]
                    rel_type = FK_TO_RELATIONSHIP.get(fk_col, "RELATED_TO")

                    recon_res = store.reconcile_relationship(
                        source_id=src_id,
                        source_type=src_type,
                        relationship_type=rel_type,
                        new_target_id=target_val,
                        target_type=target_type,
                    )
                    reconciled_edges_count += 1
                    if recon_res.get("stale_targets_closed"):
                        stale_edges_closed.extend(recon_res["stale_targets_closed"])

    # 3. Upsert Explicit Relationships (from relationships.csv)
    for rel in relationships_records:
        recon_res = store.reconcile_relationship(
            source_id=rel["source_id"],
            source_type=rel["source_type"],
            relationship_type=rel["relationship_type"],
            new_target_id=rel["target_id"],
            target_type=rel["target_type"],
            evidence_text=rel.get("evidence_text"),
            valid_from=rel.get("valid_from"),
        )
        reconciled_edges_count += 1

    graph_stats = store.get_stats()
    logger.info(
        f"Graph Sync Complete: {graph_stats['total_nodes']} total nodes ({graph_stats['placeholder_nodes']} placeholders), "
        f"{graph_stats['total_relationships']} active edges. (Closed {len(stale_edges_closed)} stale edges)."
    )

    return {
        "branch_status": {"falkordb": "SYNCED"},
        "stage": "graph_synced",
        "stats": {
            **state.get("stats", {}),
            "graph_nodes": graph_stats["total_nodes"],
            "graph_edges": graph_stats["total_relationships"],
            "placeholder_nodes": graph_stats["placeholder_nodes"],
            "stale_edges_closed": len(stale_edges_closed),
        }
    }
