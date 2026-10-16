"""
graph.py — End-to-End LangGraph definition for Company Brain.

Topology:
  START ──► collect_data (Step 1)
        ──► sync_falkordb_graph (Step 2)
        ──► detect_dirty_entities (Step 3)
        ──► sync_llm_wiki (Step 4)
        ──► audit_consistency (Step 5)
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

    # ── Sequential Graph-First Pipeline Topology ─────────────────────────
    graph_builder.add_edge(START, "collect_data")
    graph_builder.add_edge("collect_data", "sync_falkordb_graph")
    graph_builder.add_edge("sync_falkordb_graph", "detect_dirty_entities")
    graph_builder.add_edge("detect_dirty_entities", "sync_llm_wiki")
    graph_builder.add_edge("sync_llm_wiki", "audit_consistency")
    graph_builder.add_edge("audit_consistency", END)

    return graph_builder.compile()
