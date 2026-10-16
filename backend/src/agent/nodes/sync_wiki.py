"""
sync_wiki.py — LangGraph node for Step 4 Living Wiki Generation.
Renders deterministic Markdown files for dirty entities with graph hydration and manual notes protection.
"""
from typing import Any, Dict, List
from ...config import logger
from ...wiki_builder import WikiBuilder
from ..nodes.sync_graph import get_graph_store
from ..state import WikiState


def sync_llm_wiki(state: WikiState) -> Dict[str, Any]:
    """
    LangGraph Node (Step 4):
    Builds or updates living Wiki pages for all detected dirty entities.
    """
    logger.info("--- [LangGraph Node: sync_llm_wiki] Executing Step 4 Wiki Generation ---")

    store = get_graph_store()
    builder = WikiBuilder(graph_store=store)
    dirty_entity_ids = state.get("dirty_entity_ids", [])
    canonical_records = state.get("canonical_records", {})

    # If dirty_entity_ids is empty, default to rendering all canonical records in state
    if not dirty_entity_ids:
        all_eids = []
        for recs in canonical_records.values():
            all_eids.extend([r["entity_id"] for r in recs])
        dirty_entity_ids = all_eids

    wiki_res = builder.sync_dirty_pages(
        dirty_entity_ids=dirty_entity_ids,
        canonical_records=canonical_records,
    )

    logger.info(f"Living Wiki Sync Complete: {wiki_res['total_rendered']} pages generated/updated in {wiki_res['wiki_directory']}")

    return {
        "branch_status": {"wiki": "SYNCED"},
        "stage": "wiki_synced",
        "stats": {
            **state.get("stats", {}),
            "wiki_pages_rendered": wiki_res["total_rendered"],
        }
    }
