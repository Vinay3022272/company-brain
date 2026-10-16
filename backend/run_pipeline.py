"""
run_pipeline.py — Unified CLI Entry Point for Company Brain.

Usage:
  python run_pipeline.py --help
  python run_pipeline.py --sync
  python run_pipeline.py --files departments.csv teams.csv
  python run_pipeline.py --query "Who manages Platform Engineering?"
  python run_pipeline.py --test-scenarios
"""
import argparse
import json
import sys
from pathlib import Path

# Add backend directory to path
backend_dir = Path(__file__).resolve().parent
sys.path.insert(0, str(backend_dir))

from src.config import logger
from src.agent.graph import build_wiki_graph
from src.agent.state import create_initial_wiki_state
from src.agent.nodes.sync_graph import get_graph_store
from src.retrieval import GroundedRetriever


def execute_pipeline(files=None, chunk_size=500, query_text=None, target_entity_id=None):
    """Compile and invoke the complete LangGraph pipeline."""
    logger.info("Initializing Company Brain LangGraph Pipeline...")
    graph = build_wiki_graph()

    initial_state = create_initial_wiki_state(
        files_to_process=files,
        chunk_size=chunk_size,
        query_text=query_text,
        target_entity_id=target_entity_id,
    )

    result = graph.invoke(initial_state)
    return result


def run_scenarios():
    """Execute Step 7 Scenarios A, B, C, D to validate incremental lifecycle."""
    print("=" * 70)
    print("STEP 7: INCREMENTAL SCENARIO VALIDATION SUITE")
    print("=" * 70)

    graph = build_wiki_graph()
    store = get_graph_store()
    store.clear()  # Start clean for pristine scenario testing

    # ─────────────────────────────────────────────────────────────────────────
    # Scenario A: Initial upload of departments.csv
    # ─────────────────────────────────────────────────────────────────────────
    print("\n[SCENARIO A] Initial upload of departments.csv...")
    state_a = create_initial_wiki_state(
        files_to_process=["departments.csv"], 
        chunk_size=100,
        reset_manifest=True,
    )
    res_a = graph.invoke(state_a)

    dept_count = len(res_a["canonical_records"].get("Department", []))
    graph_nodes_a = res_a["stats"].get("graph_nodes", 0)
    dirty_a = len(res_a.get("dirty_entity_ids", []))
    print(f"  -> Extracted: {dept_count} Departments")
    print(f"  -> FalkorDB Nodes: {graph_nodes_a}")
    print(f"  -> Dirty Entities Generated: {dirty_a}")
    print(f"  -> Audit Status: {res_a.get('stats', {}).get('audit_status')}")

    # Check DEPT-ENG.md
    dept_file = backend_dir / "wiki" / "departments" / "DEPT-ENG.md"
    assert dept_file.exists(), "DEPT-ENG.md was not created!"
    with open(dept_file, "r", encoding="utf-8") as f:
        content_a = f.read()
    print("  -> Verified DEPT-ENG.md exists on disk.")

    # ─────────────────────────────────────────────────────────────────────────
    # Scenario B: Subsequent upload of teams.csv (child of departments)
    # ─────────────────────────────────────────────────────────────────────────
    print("\n[SCENARIO B] Subsequent upload of teams.csv (incremental linking)...")
    state_b = create_initial_wiki_state(
        files_to_process=["teams.csv"],
        chunk_size=100,
        previous_hashes=res_a["current_hashes"],
    )
    res_b = graph.invoke(state_b)

    team_count = len(res_b["canonical_records"].get("Team", []))
    graph_nodes_b = res_b["stats"].get("graph_nodes", 0)
    graph_edges_b = res_b["stats"].get("graph_edges", 0)
    print(f"  -> Extracted: {team_count} Teams")
    print(f"  -> FalkorDB Total Nodes: {graph_nodes_b}, Total Edges: {graph_edges_b}")
    print(f"  -> Impacted Neighbor Pages (Departments Refreshed): {res_b['stats'].get('impacted_neighbor_pages', 0)}")

    # Verify DEPT-ENG.md was updated with links to its new teams!
    with open(dept_file, "r", encoding="utf-8") as f:
        content_b = f.read()
    assert "TEAM-ENG" in content_b or "BELONGS_TO" in content_b, "DEPT-ENG.md did not receive new team relations!"
    print("  -> Verified DEPT-ENG.md dynamically received new team cross-links!")

    # ─────────────────────────────────────────────────────────────────────────
    # Scenario C: Update existing team (transfer department) & Edge Reconciliation
    # ─────────────────────────────────────────────────────────────────────────
    print("\n[SCENARIO C] Transfer Team & Stale Edge Reconciliation...")
    # Reassign TEAM-ENG-PLAT from DEPT-ENG to DEPT-PROD
    reconcile_res = store.reconcile_relationship(
        source_id="TEAM-ENG-PLAT",
        source_type="Team",
        relationship_type="BELONGS_TO",
        new_target_id="DEPT-PROD",
        target_type="Department",
    )
    print(f"  -> Transferred TEAM-ENG-PLAT to DEPT-PROD")
    print(f"  -> Stale targets closed: {reconcile_res['stale_targets_closed']}")

    # Refresh wiki pages for both affected departments
    from src.wiki_builder import WikiBuilder
    builder = WikiBuilder(graph_store=store)
    builder.sync_dirty_pages(["DEPT-ENG", "DEPT-PROD", "TEAM-ENG-PLAT"], {})

    eng_teams = [n["neighbor_id"] for n in store.get_neighbors("DEPT-ENG", direction="incoming")]
    prod_teams = [n["neighbor_id"] for n in store.get_neighbors("DEPT-PROD", direction="incoming")]
    assert "TEAM-ENG-PLAT" not in eng_teams, "Stale edge was not removed from DEPT-ENG!"
    assert "TEAM-ENG-PLAT" in prod_teams, "New edge was not registered in DEPT-PROD!"
    print(f"  -> Verified DEPT-ENG no longer has TEAM-ENG-PLAT: {eng_teams[:3]}")
    print(f"  -> Verified DEPT-PROD now has TEAM-ENG-PLAT: {prod_teams}")

    # ─────────────────────────────────────────────────────────────────────────
    # Scenario D: Complex Multi-Hop Grounded Retrieval Test
    # ─────────────────────────────────────────────────────────────────────────
    print("\n[SCENARIO D] Grounded Multi-Hop Query Retrieval...")
    retriever = GroundedRetriever(graph_store=store)
    ans = retriever.execute_grounded_query(
        query_text="Engineering",
        target_entity_id="DEPT-ENG",
        max_hops=2,
    )
    print("  -> Query Result:")
    print("     " + ans["grounded_answer"].replace("\n", "\n     "))
    print(f"  -> Hydrated {len(ans['wiki_citations'])} living wiki citations.")

    print("\n" + "=" * 70)
    print("ALL STEP 7 SCENARIOS (A, B, C, D) PASSED SUCCESSFULLY!")
    print("=" * 70)


def main():
    parser = argparse.ArgumentParser(description="Company Brain CLI & Pipeline Runner")
    parser.add_argument("--sync", action="store_true", help="Run full synchronization of all CSVs")
    parser.add_argument("--files", nargs="+", help="Specific CSV files to process")
    parser.add_argument("--query", type=str, help="Natural language query to retrieve with grounded evidence")
    parser.add_argument("--ask", type=str, help="Execute full LangGraph Query Agent: User Query -> Graph Traversal -> Wiki Reading -> LLM Answer")
    parser.add_argument("--target", type=str, help="Optional root entity ID for query")
    parser.add_argument("--chunk-size", type=int, default=500, help="Ingestion chunk size")
    parser.add_argument("--test-scenarios", action="store_true", help="Execute Step 7 scenario test suite")

    args = parser.parse_args()

    if args.test_scenarios:
        run_scenarios()
        return

    if args.ask:
        from src.query_agent import ask_company_brain
        res = ask_company_brain(args.ask)
        print("\n" + res["final_answer"])
        return

    # Run LangGraph pipeline
    result = execute_pipeline(
        files=args.files,
        chunk_size=args.chunk_size,
        query_text=args.query,
        target_entity_id=args.target,
    )

    print("\n=== PIPELINE EXECUTION SUMMARY ===")
    print(f"Batch ID: {result.get('batch_id')}")
    print(f"Final Stage: {result.get('stage')}")
    stats = result.get("stats", {})
    print(f"Entities Processed: {stats.get('total_canonical_entities')}")
    print(f"Graph Nodes: {stats.get('graph_nodes')}")
    print(f"Graph Relationships: {stats.get('graph_edges')}")
    print(f"Wiki Pages Rendered: {stats.get('wiki_pages_rendered')}")
    print(f"Audit Status: {stats.get('audit_status')}")

    if result.get("query_result"):
        print("\n=== QUERY ANSWER ===")
        print(result["query_result"]["grounded_answer"])


if __name__ == "__main__":
    main()
