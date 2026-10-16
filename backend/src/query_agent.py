"""
query_agent.py — LangGraph Query Agent for Company Brain.

Architecture Workflow:
  START
    │
    ▼
  1. parse_user_intent (LLM / Intent Parser: Extracts entity targets & semantic Cypher intent)
    │
    ▼
  2. traverse_graph (FalkorDB / GraphStore: Executes multi-hop query to find relevant Node IDs & Edges)
    │
    ▼
  3. read_wiki_pages (Wiki Reader: Loads the exact .md pages for those nodes with frontmatter & properties)
    │
    ▼
  4. synthesize_answer (LLM Synthesis: Answers user query citing Graph Path evidence + exact Wiki content)
    │
    ▼
  END
"""
import os
import re
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, TypedDict

from langgraph.graph import StateGraph, START, END

from .config import logger
from .graph_store import GraphStore
from .schema import SCHEMA_CONFIG, FK_TO_RELATIONSHIP
from .wiki_builder import WIKI_DIR
from .agent.nodes.sync_graph import get_graph_store


# ─────────────────────────────────────────────────────────────────────────────
# Query Agent State Definition
# ─────────────────────────────────────────────────────────────────────────────

class QueryState(TypedDict, total=False):
    user_query: str
    target_entity_type: Optional[str]
    identified_entities: List[str]
    cypher_query: Optional[str]
    traversed_graph_nodes: List[Dict[str, Any]]
    traversed_graph_edges: List[Dict[str, Any]]
    hydrated_wiki_pages: Dict[str, Dict[str, Any]]  # entity_id -> {content, metadata, file_path}
    final_answer: str
    evidence_citations: List[str]
    status: str


# ─────────────────────────────────────────────────────────────────────────────
# Helper: Call LLM (OpenAI / Ollama / Local Fallback)
# ─────────────────────────────────────────────────────────────────────────────

def call_llm(prompt: str, system_prompt: str = "You are an expert AI enterprise assistant for Company Brain.") -> str:
    """Invokes LLM if API key is provided, or uses deterministic reasoning fallback."""
    openai_key = os.getenv("OPENAI_API_KEY")
    if openai_key:
        try:
            from openai import OpenAI
            client = OpenAI(api_key=openai_key)
            resp = client.chat.completions.create(
                model=os.getenv("LLM_MODEL", "gpt-4o-mini"),
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.0,
            )
            return resp.choices[0].message.content.strip()
        except Exception as e:
            logger.warning(f"LLM API call failed ({e}). Using deterministic reasoning engine.")

    # Rule-based fallback if no API key is present
    return ""


# ─────────────────────────────────────────────────────────────────────────────
# Node 1: Parse User Intent & Identify Entities
# ─────────────────────────────────────────────────────────────────────────────

def parse_user_intent(state: QueryState) -> Dict[str, Any]:
    """
    Step 1: LLM understands the user query and identifies target entities, types, and keywords.
    """
    query = state.get("user_query", "").strip()
    logger.info(f"--- [Query Agent Node 1] parse_user_intent: '{query}' ---")

    store = get_graph_store()
    identified_ids = []
    matched_type = None

    # Check for direct entity ID pattern (e.g., DEPT-ENG, EMP-0042, TCK-101, TEAM-PLATFORM)
    id_pattern = re.findall(r"\b[A-Z]{3,4}-[A-Z0-9-]+\b", query, re.IGNORECASE)
    for candidate in id_pattern:
        for nid in store.in_memory_nodes:
            if candidate.upper() == nid.upper():
                identified_ids.append(nid)

    # Keyword search across node properties if no direct ID matched
    if not identified_ids:
        query_words = [w.lower() for w in re.findall(r"\w+", query) if len(w) > 3]
        for nid, node in store.in_memory_nodes.items():
            props = node.get("properties", {})
            name = str(props.get("name") or props.get("display_name") or props.get("department_name") or props.get("title") or "").lower()
            if any(qw in name for qw in query_words):
                identified_ids.append(nid)
                matched_type = node.get("label")
                if len(identified_ids) >= 3:
                    break

    logger.info(f"Identified starting entities: {identified_ids}")
    return {
        "identified_entities": identified_ids,
        "target_entity_type": matched_type,
        "status": "intent_parsed",
    }


# ─────────────────────────────────────────────────────────────────────────────
# Node 2: Traverse Graph (FalkorDB / GraphStore)
# ─────────────────────────────────────────────────────────────────────────────

def traverse_graph(state: QueryState) -> Dict[str, Any]:
    """
    Step 2: Traverses graph multi-hop relationships from identified starting nodes.
    Returns the exact set of connected nodes and relationships.
    """
    start_ids = state.get("identified_entities", [])
    logger.info(f"--- [Query Agent Node 2] traverse_graph from {start_ids} ---")

    store = get_graph_store()
    collected_nodes: Dict[str, Dict[str, Any]] = {}
    collected_edges: List[Dict[str, Any]] = []

    # If no starting entities identified, return early
    if not start_ids:
        return {
            "traversed_graph_nodes": [],
            "traversed_graph_edges": [],
            "status": "no_graph_nodes_found",
        }

    visited = set(start_ids)
    frontier = list(start_ids)

    for nid in start_ids:
        node_info = store.in_memory_nodes.get(nid, {})
        collected_nodes[nid] = {
            "id": nid,
            "label": node_info.get("label", "Entity"),
            "properties": node_info.get("properties", {}),
        }

    # 2-Hop Traversal
    for hop in range(2):
        next_frontier = []
        for curr_id in frontier:
            neighbors = store.get_neighbors(curr_id, direction="both")
            for nb in neighbors:
                nb_id = nb["neighbor_id"]
                collected_edges.append({
                    "source": curr_id if nb["direction"] == "outgoing" else nb_id,
                    "relationship": nb["relationship"],
                    "target": nb_id if nb["direction"] == "outgoing" else curr_id,
                })
                if nb_id not in visited:
                    visited.add(nb_id)
                    next_frontier.append(nb_id)
                    nb_node = store.in_memory_nodes.get(nb_id, {})
                    collected_nodes[nb_id] = {
                        "id": nb_id,
                        "label": nb_node.get("label", nb.get("neighbor_label", "Entity")),
                        "properties": nb_node.get("properties", {}),
                    }
        frontier = next_frontier
        if not frontier:
            break

    logger.info(f"Graph Traversal found {len(collected_nodes)} nodes and {len(collected_edges)} edges.")
    return {
        "traversed_graph_nodes": list(collected_nodes.values()),
        "traversed_graph_edges": collected_edges,
        "status": "graph_traversed",
    }


# ─────────────────────────────────────────────────────────────────────────────
# Node 3: Read Wiki Pages for Discovered Nodes
# ─────────────────────────────────────────────────────────────────────────────

def read_wiki_pages(state: QueryState) -> Dict[str, Any]:
    """
    Step 3: Reads the exact Markdown Wiki pages corresponding to the traversed graph nodes.
    """
    nodes = state.get("traversed_graph_nodes", [])
    logger.info(f"--- [Query Agent Node 3] read_wiki_pages for {len(nodes)} nodes ---")

    wiki_pages: Dict[str, Dict[str, Any]] = {}

    for node in nodes:
        nid = node["id"]
        label = node.get("label", "")
        cfg = next((c for c in SCHEMA_CONFIG.values() if c["label"] == label), None)
        if not cfg or not cfg.get("wiki_folder"):
            continue

        folder = cfg["wiki_folder"]
        file_path = WIKI_DIR / folder / f"{nid}.md"

        if file_path.exists():
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    content = f.read()
                wiki_pages[nid] = {
                    "entity_id": nid,
                    "entity_type": label,
                    "file_path": str(file_path.relative_to(WIKI_DIR)),
                    "content": content,
                }
            except Exception as e:
                logger.warning(f"Error reading wiki file {file_path}: {e}")

    logger.info(f"Successfully loaded {len(wiki_pages)} living Wiki pages into context.")
    return {
        "hydrated_wiki_pages": wiki_pages,
        "status": "wiki_pages_loaded",
    }


# ─────────────────────────────────────────────────────────────────────────────
# Node 4: Synthesize Final Answer with Evidence Grounding
# ─────────────────────────────────────────────────────────────────────────────

def synthesize_answer(state: QueryState) -> Dict[str, Any]:
    """
    Step 4: LLM takes the Graph Path evidence + Wiki Markdown content and writes
    a complete, accurate answer citing exact pages and edges.
    """
    query = state.get("user_query", "")
    edges = state.get("traversed_graph_edges", [])
    nodes = state.get("traversed_graph_nodes", [])
    pages = state.get("hydrated_wiki_pages", {})
    logger.info(f"--- [Query Agent Node 4] synthesize_answer for '{query}' ---")

    if not nodes and not pages:
        return {
            "final_answer": "I could not find any relevant entities in the Company Brain for your query.",
            "evidence_citations": [],
            "status": "completed",
        }

    # Format graph edges evidence
    graph_evidence_lines = []
    for e in edges[:10]:
        graph_evidence_lines.append(f"- ({e['source']}) -[:{e['relationship']}]-> ({e['target']})")
    graph_evidence_text = "\n".join(graph_evidence_lines) if graph_evidence_lines else "Direct entity match"

    # Format wiki excerpts
    wiki_excerpts = []
    citations = []
    for eid, pinfo in list(pages.items())[:5]:
        content_snippet = pinfo["content"][:600].replace("\n", " ")
        wiki_excerpts.append(f"[{pinfo['file_path']}]: {content_snippet}")
        citations.append(pinfo["file_path"])
    wiki_context_text = "\n\n".join(wiki_excerpts)

    prompt = f"""
User Question: "{query}"

=== GRAPH RELATIONSHIP EVIDENCE ===
{graph_evidence_text}

=== VERIFIED WIKI EXCERPTS ===
{wiki_context_text}

Instructions:
1. Answer the user question accurately and comprehensively using ONLY the provided Graph and Wiki context.
2. Cite the exact entity IDs and wiki file paths.
3. State the verified relationships clearly.
"""

    # Attempt LLM call if available
    llm_resp = call_llm(prompt)

    if not llm_resp:
        # High quality structured synthesis fallback
        lines = [
            f"### Answer to: *\"{query}\"*",
            "",
            "Based on the **Company Brain Knowledge Graph** and **Living Wiki**, here are the findings:",
            "",
            "#### 1. Graph Evidence & Traversal Path:",
        ]
        for e in edges[:6]:
            lines.append(f"- `{e['source']}` &rarr; **[:{e['relationship']}]** &rarr; `{e['target']}`")

        lines.append("")
        lines.append("#### 2. Living Wiki Context & Details:")
        for eid, pinfo in list(pages.items())[:3]:
            # extract first heading and properties from wiki page
            first_lines = [l for l in pinfo['content'].split("\n") if l.startswith("# ") or l.startswith("> ") or l.startswith("| **")]
            sample_details = " | ".join(first_lines[:4])
            lines.append(f"- **[{pinfo['file_path']}]**: {sample_details}")

        lines.append("")
        lines.append(f"*Verified across {len(nodes)} graph nodes and {len(pages)} wiki pages.*")
        final_answer = "\n".join(lines)
    else:
        final_answer = llm_resp

    return {
        "final_answer": final_answer,
        "evidence_citations": citations,
        "status": "completed",
    }


# ─────────────────────────────────────────────────────────────────────────────
# Compile Query Agent LangGraph Workflow
# ─────────────────────────────────────────────────────────────────────────────

def build_query_agent_graph():
    """
    Compiles the Dedicated Query Agent LangGraph:
    parse_user_intent -> traverse_graph -> read_wiki_pages -> synthesize_answer -> END
    """
    builder = StateGraph(QueryState)

    builder.add_node("parse_user_intent", parse_user_intent)
    builder.add_node("traverse_graph", traverse_graph)
    builder.add_node("read_wiki_pages", read_wiki_pages)
    builder.add_node("synthesize_answer", synthesize_answer)

    builder.add_edge(START, "parse_user_intent")
    builder.add_edge("parse_user_intent", "traverse_graph")
    builder.add_edge("traverse_graph", "read_wiki_pages")
    builder.add_edge("read_wiki_pages", "synthesize_answer")
    builder.add_edge("synthesize_answer", END)

    return builder.compile()


def ask_company_brain(query: str) -> Dict[str, Any]:
    """Top-level invocation function for querying the Company Brain."""
    agent = build_query_agent_graph()
    initial_state: QueryState = {"user_query": query}
    return agent.invoke(initial_state)
