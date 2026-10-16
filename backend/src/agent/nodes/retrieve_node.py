"""
retrieve_node.py — LangGraph node for Step 6/7 Grounded Retrieval Interface.
Allows the LangGraph pipeline to execute graph traversal + wiki hydration queries.
"""
from typing import Any, Dict
from ...config import logger
from ...retrieval import GroundedRetriever
from ..nodes.sync_graph import get_graph_store
from ..state import WikiState


def query_knowledge_base(state: WikiState) -> Dict[str, Any]:
    """
    LangGraph Node (Retrieval Interface):
    If `query_text` is present in state, executes Grounded Retrieval:
    - Finds root entity in graph
    - Performs multi-hop traversal
    - Hydrates context from Living Wiki markdown pages
    - Produces query_result with citations & evidence paths
    """
    query_text = state.get("query_text")
    if not query_text:
        return {"stage": "ready"}

    logger.info(f"--- [LangGraph Node: query_knowledge_base] Query: '{query_text}' ---")

    store = get_graph_store()
    retriever = GroundedRetriever(graph_store=store)

    target_id = state.get("target_entity_id")
    result = retriever.execute_grounded_query(
        query_text=query_text,
        target_entity_id=target_id,
        max_hops=2,
    )

    return {
        "query_result": result,
        "stage": "query_answered",
    }
