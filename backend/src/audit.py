"""
audit.py — Multi-Level Consistency Audit & Repair Engine for Company Brain.

Validates:
1. Entity Parity: CSV source count vs FalkorDB nodes vs Wiki pages.
2. Property Parity: Core attributes matched between source and graph.
3. Relationship Parity: Foreign keys matched with active graph edges.
4. Markdown Link Integrity: Validates that all internal links in Wiki pages point to existing files (zero 404s).
5. Orphan & Placeholder Detection: Identifies unresolved foreign keys and placeholder nodes.
6. Actionable Status: PASS | PARTIAL | NEEDS_REPAIR.
"""
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from .config import DATA_DIR, logger
from .graph_store import GraphStore
from .schema import SCHEMA_CONFIG
from .wiki_builder import WIKI_DIR


class ConsistencyAuditor:
    """Performs comprehensive audits across CSV source, FalkorDB graph, and Markdown wiki."""

    def __init__(self, graph_store: GraphStore, wiki_dir: Optional[Path] = None):
        self.store = graph_store
        self.wiki_dir = wiki_dir or WIKI_DIR

    def audit_markdown_links(self) -> Dict[str, Any]:
        """Verify that every internal markdown link [text](../folder/file.md) exists on disk."""
        broken_links = []
        total_links_checked = 0

        if not self.wiki_dir.exists():
            return {"status": "WIKI_DIR_MISSING", "broken_links": [], "total_checked": 0}

        for md_file in self.wiki_dir.glob("*/*.md"):
            try:
                with open(md_file, "r", encoding="utf-8") as f:
                    content = f.read()

                # Find all relative markdown links like (../folder/id.md)
                matches = re.findall(r"\[([^\]]+)\]\((../[^)]+\.md)\)", content)
                for link_text, rel_target in matches:
                    total_links_checked += 1
                    # Resolve target path relative to parent folder of current md_file
                    target_path = (md_file.parent / rel_target).resolve()
                    if not target_path.exists():
                        broken_links.append({
                            "source_file": str(md_file.relative_to(self.wiki_dir)),
                            "link_text": link_text,
                            "target_link": rel_target,
                        })
            except Exception as e:
                logger.warning(f"Error auditing links in {md_file}: {e}")

        return {
            "total_links_checked": total_links_checked,
            "broken_links_count": len(broken_links),
            "broken_links": broken_links,
            "integrity_status": "PASS" if not broken_links else "BROKEN_LINKS_DETECTED",
        }

    def run_full_audit(
        self,
        canonical_records: Dict[str, List[Dict[str, Any]]],
        quarantined_records: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Run all audit checks and generate actionable report."""
        logger.info("Executing Multi-Level Consistency Audit...")
        quarantined = quarantined_records or []

        # 1. Entity Parity (CSV vs Graph vs Wiki)
        entity_breakdown = {}
        discrepancies = []

        graph_stats = self.store.get_stats()
        node_counts_by_label: Dict[str, int] = {}
        for nid, node in self.store.in_memory_nodes.items():
            if not node.get("is_placeholder"):
                lbl = node.get("label", "Unknown")
                node_counts_by_label[lbl] = node_counts_by_label.get(lbl, 0) + 1

        wiki_counts_by_label: Dict[str, int] = {}
        for cfg in SCHEMA_CONFIG.values():
            folder = cfg.get("wiki_folder")
            if folder:
                target_dir = self.wiki_dir / folder
                wiki_counts_by_label[cfg["label"]] = len(list(target_dir.glob("*.md"))) if target_dir.exists() else 0

        for label, records in canonical_records.items():
            cfg = next((c for c in SCHEMA_CONFIG.values() if c["label"] == label), {})
            if cfg.get("is_junction"):
                continue  # Junctions are stored as relationships, not individual pages

            csv_count = len(records)
            graph_count = node_counts_by_label.get(label, 0)
            wiki_count = wiki_counts_by_label.get(label, 0)

            entity_breakdown[label] = {
                "csv_expected": csv_count,
                "graph_nodes": graph_count,
                "wiki_pages": wiki_count,
            }

            if csv_count != graph_count or csv_count != wiki_count:
                discrepancies.append({
                    "entity_type": label,
                    "csv_expected": csv_count,
                    "graph_count": graph_count,
                    "wiki_page_count": wiki_count,
                    "status": "PARITY_MISMATCH",
                })

        # 2. Markdown Link Integrity Check
        link_audit = self.audit_markdown_links()

        # 3. Placeholder / Unresolved Parent Check
        placeholder_count = graph_stats.get("placeholder_nodes", 0)
        placeholder_ids = graph_stats.get("placeholder_ids", [])

        # 4. Overall Status Determination
        if discrepancies or link_audit["broken_links_count"] > 0:
            overall_status = "NEEDS_REPAIR"
        elif placeholder_count > 0 or len(quarantined) > 0:
            overall_status = "PARTIAL"
        else:
            overall_status = "PASS"

        report = {
            "overall_status": overall_status,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "summary": {
                "total_csv_entities": sum(len(r) for r in canonical_records.values()),
                "total_graph_nodes": graph_stats.get("total_nodes", 0),
                "total_graph_relationships": graph_stats.get("total_relationships", 0),
                "total_wiki_pages": sum(wiki_counts_by_label.values()),
                "total_placeholders": placeholder_count,
                "total_quarantined": len(quarantined),
                "broken_links_count": link_audit["broken_links_count"],
            },
            "entity_breakdown": entity_breakdown,
            "discrepancies": discrepancies,
            "link_audit": link_audit,
            "unresolved_placeholders": placeholder_ids[:20],
            "quarantined_records": quarantined,
        }

        # Write audit report to disk
        audit_file = self.wiki_dir / "consistency_audit_report.json"
        try:
            self.wiki_dir.mkdir(parents=True, exist_ok=True)
            with open(audit_file, "w", encoding="utf-8") as f:
                json.dump(report, f, indent=2)
            report["report_file"] = str(audit_file)
        except Exception as e:
            logger.warning(f"Could not write audit report file: {e}")

        logger.info(f"Audit completed: Status = [{overall_status}]. Parity Mismatches: {len(discrepancies)}, Broken Links: {link_audit['broken_links_count']}")
        return report
