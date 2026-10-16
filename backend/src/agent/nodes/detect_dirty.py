"""
detect_dirty.py — LangGraph node for Step 3 Dependency-Aware Dirty-Page Detection.
Identifies precisely which Wiki pages must be created or refreshed based on:
1. Newly added or updated entities.
2. Modified relationships (both source and target endpoints).
3. 1-hop graph dependency neighborhood rules.
"""
from typing import Any, Dict, List, Set
from ...config import logger
from ...graph_store import GraphStore
from ..nodes.sync_graph import get_graph_store
from ..state import WikiState
from ...schema import SCHEMA_CONFIG


def get_dynamic_dependency_rules() -> Dict[str, Set[str]]:
    """
    Dynamically generates the dirty dependency rules by inverting foreign keys
    defined in SCHEMA_CONFIG.
    
    If entity X points to entity Y via foreign key (e.g. Team -> Department),
    then when X changes or is added, Y is affected and marked dirty.
    Also handles bidirectional relationships (e.g. Employee <-> Team).
    """
    rules: Dict[str, Set[str]] = {}

    for file_name, config in SCHEMA_CONFIG.items():
        source_label = config.get("label")
        if not source_label:
            continue

        if source_label not in rules:
            rules[source_label] = set()

        fk_map = config.get("fk", {})
        for fk_col, target_spec in fk_map.items():
            if isinstance(target_spec, (list, tuple)) and len(target_spec) >= 1:
                target_label = target_spec[0]
                # If source points to target, target is an impacted parent/dependency
                rules[source_label].add(target_label)

    return rules


def detect_dirty_entities(state: WikiState) -> Dict[str, Any]:
    """
    LangGraph Node (Step 3):
    Computes a deduplicated set of dirty entity IDs that need Wiki page generation/regeneration:
    - Added entities (need new pages)
    - Updated entities (need refreshed pages)
    - 1-hop graph neighbors dynamically derived from SCHEMA_CONFIG
    """
    logger.info("--- [LangGraph Node: detect_dirty_entities] Executing Step 3 Dirty Detection ---")

    store = get_graph_store()
    changed_entities = state.get("changed_entities", {})
    added_ids = set(changed_entities.get("added", []))
    updated_ids = set(changed_entities.get("updated", []))

    dirty_set: Set[str] = set()

    # 1. Direct dirty entities (added + updated)
    dirty_set.update(added_ids)
    dirty_set.update(updated_ids)

    # 2. Dynamic Dependency Rules from SCHEMA_CONFIG
    dependency_rules = get_dynamic_dependency_rules()
    impacted_neighbors: Set[str] = set()

    for eid in (added_ids | updated_ids):
        node = store.in_memory_nodes.get(eid)
        if not node:
            continue
        entity_label = node.get("label", "")
        allowed_neighbor_labels = dependency_rules.get(entity_label, set())

        if not allowed_neighbor_labels:
            continue

        neighbors = store.get_neighbors(eid, direction="both")
        for nb in neighbors:
            nb_label = nb.get("neighbor_label")
            nb_id = nb.get("neighbor_id")
            if nb_label in allowed_neighbor_labels and nb_id:
                impacted_neighbors.add(nb_id)

    # 3. Add impacted neighbors to the dirty set
    dirty_set.update(impacted_neighbors)

    logger.info(
        f"Dirty Detection Complete: {len(dirty_set)} total dirty entities "
        f"({len(added_ids)} added, {len(updated_ids)} updated, {len(impacted_neighbors)} impacted neighbors)."
    )

    return {
        "dirty_entity_ids": list(dirty_set),
        "stage": "dirty_entities_detected",
        "stats": {
            **state.get("stats", {}),
            "total_dirty_pages": len(dirty_set),
            "impacted_neighbor_pages": len(impacted_neighbors),
        }
    }
