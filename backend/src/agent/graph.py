"""
graph.py — LangGraph definition for the Company Brain Synchronization & Wiki Agent.

Graph Topology:
                  ┌─► build_falkordb_graph ──┐
  START ─► collect_data                      ├──► consistency_audit ─► END
                  └─► build_llm_wiki ────────┘
                  (Parallel Branching)
"""
from typing import Any, Dict
from langgraph.graph import StateGraph, START, END

from .state import WikiState
from .nodes.collect_data import collect_data
from ..config import logger


def build_falkordb_graph_placeholder(state: WikiState) -> Dict[str, Any]:
    """
    Parallel Branch 1: Ingests canonical entities, foreign-keys, and explicit
    relationship edges into FalkorDB.
    """
    total_entities = sum(len(recs) for recs in state.get("canonical_records", {}).values())
    total_rels = len(state.get("relationships_records", []))
    logger.info(f"--- [Parallel Branch: FalkorDB] Ready with {total_entities} entities and {total_rels} edges ---")
    return {
        "branch_status": {"falkordb": "READY_OR_PROCESSED"},
        "stage": "falkordb_synced",
    }


def build_llm_wiki_placeholder(state: WikiState) -> Dict[str, Any]:
    """
    Parallel Branch 2: Generates structured Markdown pages with YAML frontmatter,
    properties tables, and links for LLM exploration.
    """
    total_entities = sum(len(recs) for recs in state.get("canonical_records", {}).values())
    logger.info(f"--- [Parallel Branch: LLM Wiki] Ready with {total_entities} entities to generate markdown pages ---")
    return {
        "branch_status": {"wiki": "READY_OR_PROCESSED"},
        "stage": "wiki_synced",
    }


def consistency_audit_placeholder(state: WikiState) -> Dict[str, Any]:
    """
    Fan-in / Join Node: Audits parity and consistency between CSV source,
    FalkorDB graph, and Markdown Wiki pages.
    """
    logger.info("--- [Fan-in: Consistency Audit] Verifying synchronization parity ---")
    return {
        "stage": "completed",
    }


def build_wiki_graph(parallel_branches: bool = True):
    """
    Build and compile the LangGraph for Company Brain data ingestion and synchronization.

    When parallel_branches is True:
      collect_data branches concurrently to build_falkordb_graph and build_llm_wiki,
      and joins at consistency_audit.
    """
    graph_builder = StateGraph(WikiState)

    # ── Nodes ────────────────────────────────────────────────────────────
    graph_builder.add_node("collect_data", collect_data)
    
    if parallel_branches:
        graph_builder.add_node("build_falkordb_graph", build_falkordb_graph_placeholder)
        graph_builder.add_node("build_llm_wiki", build_llm_wiki_placeholder)
        graph_builder.add_node("consistency_audit", consistency_audit_placeholder)

        # ── Edges with Parallel Fan-out & Fan-in ───────────────────────────
        graph_builder.add_edge(START, "collect_data")
        
        # Parallel branch: collect_data triggers both graph and wiki builds
        graph_builder.add_edge("collect_data", "build_falkordb_graph")
        graph_builder.add_edge("collect_data", "build_llm_wiki")
        
        # Fan-in join: both branches complete before consistency audit
        graph_builder.add_edge("build_falkordb_graph", "consistency_audit")
        graph_builder.add_edge("build_llm_wiki", "consistency_audit")
        
        graph_builder.add_edge("consistency_audit", END)
    else:
        # Focused Stage 1 data collection graph
        graph_builder.add_edge(START, "collect_data")
        graph_builder.add_edge("collect_data", END)

    return graph_builder.compile()
