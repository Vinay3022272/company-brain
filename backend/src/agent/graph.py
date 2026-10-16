"""
graph.py — End-to-End LangGraph definition for Company Brain.

Topology:
  START ──► collect_data (Step 1: Checkpoint & Diff)
        ──► sync_falkordb_graph (Step 2: Idempotent Graph Sync & Reconcile)
        ──► detect_dirty_entities (Step 3: 1-Hop Neighbor Impact)
        ──► sync_llm_wiki (Step 4: Living Wiki Markdown Generation)
        ──► audit_consistency (Step 5: Parity & Link Integrity)
        ──► query_knowledge_base (Step 6/7: Grounded Retrieval Interface)
        ──► END
"""
from typing import Any, Dict
from langgraph.graph import StateGraph, START, END

from .state import WikiState
from .nodes.collect_data import collect_data
from .nodes.sync_graph import sync_falkordb_graph
from .nodes.detect_dirty import detect_dirty_entities
from .nodes.sync_wiki import sync_llm_wiki
from .nodes.audit_node import audit_consistency
from .nodes.retrieve_node import query_knowledge_base
from ..config import logger


def build_wiki_graph():
    """Build and compile the end-to-end LangGraph pipeline for Company Brain."""
    graph_builder = StateGraph(WikiState)

    # ── Nodes ────────────────────────────────────────────────────────────
    graph_builder.add_node("collect_data", collect_data)
    graph_builder.add_node("sync_falkordb_graph", sync_falkordb_graph)
    graph_builder.add_node("detect_dirty_entities", detect_dirty_entities)
    graph_builder.add_node("sync_llm_wiki", sync_llm_wiki)
    graph_builder.add_node("audit_consistency", audit_consistency)
    graph_builder.add_node("query_knowledge_base", query_knowledge_base)

    # ── Sequential Graph-First Pipeline Topology ─────────────────────────
    graph_builder.add_edge(START, "collect_data")
    graph_builder.add_edge("collect_data", "sync_falkordb_graph")
    graph_builder.add_edge("sync_falkordb_graph", "detect_dirty_entities")
    graph_builder.add_edge("detect_dirty_entities", "sync_llm_wiki")
    graph_builder.add_edge("sync_llm_wiki", "audit_consistency")
    graph_builder.add_edge("audit_consistency", "query_knowledge_base")
    graph_builder.add_edge("query_knowledge_base", END)

    return graph_builder.compile()
