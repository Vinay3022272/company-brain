"""
audit_node.py — LangGraph node for Step 5 Consistency Audit.
Runs entity, property, and link integrity checks and attaches report to state.
"""
from typing import Any, Dict
from ...audit import ConsistencyAuditor
from ...config import logger
from ..nodes.sync_graph import get_graph_store
from ..state import WikiState


def audit_consistency(state: WikiState) -> Dict[str, Any]:
    """
    LangGraph Node (Step 5):
    Verifies parity across CSV sources, FalkorDB graph, and Wiki markdown pages.
    """
    logger.info("--- [LangGraph Node: audit_consistency] Executing Step 5 Consistency Audit ---")

    store = get_graph_store()
    auditor = ConsistencyAuditor(graph_store=store)

    canonical_records = state.get("canonical_records", {})
    quarantined = state.get("quarantined_records", [])

    report = auditor.run_full_audit(
        canonical_records=canonical_records,
        quarantined_records=quarantined,
    )

    logger.info(
        f"Audit Complete: Status = [{report['overall_status']}]. "
        f"Discrepancies: {len(report['discrepancies'])}, Broken Links: {report['link_audit']['broken_links_count']}"
    )

    return {
        "audit_report": report,
        "stage": "completed",
        "stats": {
            **state.get("stats", {}),
            "audit_status": report["overall_status"],
            "broken_wiki_links": report["link_audit"]["broken_links_count"],
        }
    }
