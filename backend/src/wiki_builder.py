"""
wiki_builder.py — Deterministic Living Wiki Generator for Company Brain.

Core Principles:
1. Code-rendered deterministic tables, IDs, frontmatter, and relative markdown links.
2. Protection of manual human notes between <!-- BEGIN_MANUAL_NOTES --> and <!-- END_MANUAL_NOTES --> tags.
3. Revision Changelog & Audit History section (incremental history preservation).
4. Graph-hydrated 1-hop neighborhood relations (using GraphStore).
5. Safe relative link resolution and soft-delete/tombstone support.
"""
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from .config import BACKEND_DIR, DATA_DIR, logger
from .graph_store import GraphStore
from .schema import SCHEMA_CONFIG

WIKI_DIR = BACKEND_DIR / "wiki"


class WikiBuilder:
    """Builds and incrementally synchronizes the Markdown Wiki from CanonicalRecords and GraphStore."""

    def __init__(self, wiki_dir: Optional[Path] = None, graph_store: Optional[GraphStore] = None):
        self.wiki_dir = wiki_dir or WIKI_DIR
        self.graph_store = graph_store or GraphStore()

    def ensure_directories(self):
        """Create base wiki directory and subfolders for each entity type."""
        self.wiki_dir.mkdir(parents=True, exist_ok=True)
        for cfg in SCHEMA_CONFIG.values():
            folder = cfg.get("wiki_folder")
            if folder:
                (self.wiki_dir / folder).mkdir(parents=True, exist_ok=True)

    def _format_value(self, val: Any) -> str:
        """Format an attribute value safely for Markdown tables."""
        if val is None or val == "":
            return "*None*"
        if isinstance(val, list):
            return ", ".join(f"`{x}`" for x in val)
        clean_str = str(val).replace("\n", " ").strip()
        # Escape markdown table pipes
        return clean_str.replace("|", "\\|")

    def _resolve_link(self, target_label: str, target_id: str, current_folder: str) -> str:
        """Generate relative Markdown link to target entity wiki page."""
        target_cfg = next((c for c in SCHEMA_CONFIG.values() if c["label"] == target_label), None)
        if not target_cfg or not target_cfg.get("wiki_folder"):
            return f"`{target_id}`"
        tgt_folder = target_cfg["wiki_folder"]
        return f"[{target_id}](../{tgt_folder}/{target_id}.md)"

    def extract_manual_notes(self, existing_content: str) -> str:
        """Extract user-written manual notes preserved between marker tags."""
        match = re.search(
            r"<!-- BEGIN_MANUAL_NOTES -->(.*?)<!-- END_MANUAL_NOTES -->",
            existing_content,
            re.DOTALL
        )
        if match:
            return match.group(1).strip()
        return ""

    def extract_existing_changelog(self, existing_content: str) -> List[str]:
        """Extract existing changelog rows so historical audit events are preserved."""
        match = re.search(
            r"## Revision & Timeline History\s*\n.*?\n\| :---.*?\|\n(.*?)(?=\n## |\n---|\Z)",
            existing_content,
            re.DOTALL
        )
        if match:
            raw_lines = match.group(1).strip().split("\n")
            return [line.strip() for line in raw_lines if line.strip().startswith("|")]
        return []

    def generate_entity_page(
        self,
        entity_id: str,
        entity_type: str,
        display_name: str,
        properties: Dict[str, Any],
        source_file: str = "",
        row_hash: str = "",
        change_type: str = "added",
        changed_fields: Optional[List[str]] = None,
        is_tombstone: bool = False,
    ) -> str:
        """
        Render a structured Markdown page for an entity:
        - Frontmatter
        - Core Properties Table
        - 1-Hop Graph Neighbors (from GraphStore)
        - Revision & Timeline History
        - Protected Manual Notes
        """
        cfg = next((c for c in SCHEMA_CONFIG.values() if c["label"] == entity_type), None)
        folder = cfg.get("wiki_folder", "entities") if cfg else "entities"
        file_path = self.wiki_dir / folder / f"{entity_id}.md"

        existing_content = ""
        manual_notes = ""
        existing_history: List[str] = []

        if file_path.exists():
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    existing_content = f.read()
                manual_notes = self.extract_manual_notes(existing_content)
                existing_history = self.extract_existing_changelog(existing_content)
            except Exception as e:
                logger.warning(f"Could not read existing page {file_path}: {e}")

        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
        safe_display = display_name.replace('"', '')

        # 1. Frontmatter
        lines = [
            "---",
            f'entity_id: "{entity_id}"',
            f'entity_type: "{entity_type}"',
            f'display_name: "{safe_display}"',
            f'source_file: "{source_file}"',
            f'row_hash: "{row_hash}"',
            f'status: "{"TOMBSTONED" if is_tombstone else "ACTIVE"}"',
            f'last_synced: "{now_iso}"',
            "---",
            "",
            f"# {display_name} (`{entity_id}`)",
            "",
            f"> **Type**: {entity_type}  ",
            f"> **Source**: `{source_file}` (Hash: `{row_hash}`)  ",
            f"> **Status**: `{'ARCHIVED / TOMBSTONED' if is_tombstone else 'ACTIVE'}`",
            "",
            "## Core Properties",
            "",
            "| Property | Value |",
            "| :--- | :--- |",
        ]

        pk_col = cfg.get("pk") if cfg else "id"
        for k, v in properties.items():
            if k == pk_col:
                continue
            fmt_v = self._format_value(v)
            lines.append(f"| **{k}** | {fmt_v} |")

        lines.append("")

        # 2. Graph Neighborhood Relationships (1-Hop Traversal)
        lines.append("## 🔗 Connected Graph Relations")
        lines.append("")

        neighbors = self.graph_store.get_neighbors(entity_id, direction="both")
        if neighbors:
            incoming = [n for n in neighbors if n["direction"] == "incoming"]
            outgoing = [n for n in neighbors if n["direction"] == "outgoing"]

            if outgoing:
                lines.append("### Outgoing Relations")
                for n in outgoing:
                    link = self._resolve_link(n["neighbor_label"], n["neighbor_id"], folder)
                    lines.append(f"- **{n['relationship']}** &rarr; {n['neighbor_label']}: {link} ({n['neighbor_name']})")
                lines.append("")

            if incoming:
                lines.append("### Incoming Connected Entities")
                for n in incoming:
                    link = self._resolve_link(n["neighbor_label"], n["neighbor_id"], folder)
                    lines.append(f"- **{n['relationship']}** &larr; {n['neighbor_label']}: {link} ({n['neighbor_name']})")
                lines.append("")
        else:
            lines.append("*No active graph connections registered.*")
            lines.append("")

        # 3. Revision & Timeline History Section (Incremental)
        lines.append("##  Revision & Timeline History")
        lines.append("")
        lines.append("| Timestamp | Event / Change | Details | Hash |")
        lines.append("| :--- | :--- | :--- | :--- |")

        # Re-insert existing history
        for hist_row in existing_history:
            lines.append(hist_row)

        # Add current event row
        if change_type == "added":
            detail = "Initial entity record created and linked."
        elif change_type == "updated":
            fields_str = ", ".join(changed_fields) if changed_fields else "properties updated"
            detail = f"Updated fields: `{fields_str}`"
        elif is_tombstone:
            detail = "Entity soft-deleted / tombstoned."
        else:
            detail = "Graph relationship re-synced."

        current_event_row = f"| **{now_iso}** | `{change_type.upper()}` | {detail} | `{row_hash[:8] if row_hash else 'N/A'}` |"
        lines.append(current_event_row)
        lines.append("")

        # 4. Protected Manual Notes Section
        lines.append("##  Protected Manual Notes")
        lines.append("<!-- BEGIN_MANUAL_NOTES -->")
        if manual_notes:
            lines.append(manual_notes)
        else:
            lines.append("*Human reviewers can add manual notes and annotations here. They are preserved across syncs.*")
        lines.append("<!-- END_MANUAL_NOTES -->")
        lines.append("")

        lines.append("---")
        lines.append(f"*Company Brain Knowledge Wiki — Last updated: {now_iso}*")
        lines.append("")

        return "\n".join(lines)

    def write_page(
        self,
        entity_id: str,
        entity_type: str,
        display_name: str,
        properties: Dict[str, Any],
        source_file: str = "",
        row_hash: str = "",
        change_type: str = "added",
        changed_fields: Optional[List[str]] = None,
        is_tombstone: bool = False,
    ) -> Optional[Path]:
        """Write Markdown file for an entity."""
        cfg = next((c for c in SCHEMA_CONFIG.values() if c["label"] == entity_type), None)
        if not cfg or not cfg.get("wiki_folder"):
            return None

        folder = cfg["wiki_folder"]
        target_dir = self.wiki_dir / folder
        target_dir.mkdir(parents=True, exist_ok=True)
        file_path = target_dir / f"{entity_id}.md"

        content = self.generate_entity_page(
            entity_id=entity_id,
            entity_type=entity_type,
            display_name=display_name,
            properties=properties,
            source_file=source_file,
            row_hash=row_hash,
            change_type=change_type,
            changed_fields=changed_fields,
            is_tombstone=is_tombstone,
        )

        with open(file_path, "w", encoding="utf-8") as f:
            f.write(content)

        return file_path

    def sync_dirty_pages(
        self,
        dirty_entity_ids: List[str],
        canonical_records: Dict[str, List[Dict[str, Any]]],
    ) -> Dict[str, Any]:
        """
        Regenerate Wiki pages for dirty entities:
        - Added / updated records from canonical_records.
        - Existing neighbor entities whose relationships changed.
        """
        self.ensure_directories()
        
        # Build lookup map of incoming canonical records
        records_map: Dict[str, Dict[str, Any]] = {}
        for label, recs in canonical_records.items():
            for r in recs:
                records_map[r["entity_id"]] = r

        rendered_count = 0
        written_pages: List[str] = []

        for eid in dirty_entity_ids:
            if eid in records_map:
                rec = records_map[eid]
                p = self.write_page(
                    entity_id=eid,
                    entity_type=rec["entity_type"],
                    display_name=rec.get("display_name", eid),
                    properties=rec.get("properties", {}),
                    source_file=rec.get("source_file", ""),
                    row_hash=rec.get("row_hash", ""),
                    change_type=rec.get("change_type", "updated"),
                    changed_fields=rec.get("changed_fields", []),
                    is_tombstone=rec.get("is_tombstone", False),
                )
                if p:
                    rendered_count += 1
                    written_pages.append(str(p))
            else:
                # Affected neighbor whose relationship changed but wasn't in current CSV chunk
                node = self.graph_store.in_memory_nodes.get(eid)
                if node:
                    label = node.get("label", "Entity")
                    props = node.get("properties", {})
                    disp = props.get("display_name") or props.get("name") or eid
                    p = self.write_page(
                        entity_id=eid,
                        entity_type=label,
                        display_name=disp,
                        properties=props,
                        source_file="graph_store",
                        row_hash="",
                        change_type="relationship_synced",
                    )
                    if p:
                        rendered_count += 1
                        written_pages.append(str(p))

        # Generate index catalog
        self.generate_master_index()

        return {
            "total_rendered": rendered_count,
            "written_pages": written_pages,
            "wiki_directory": str(self.wiki_dir),
        }

    def generate_master_index(self):
        """Generate top-level index.md cataloging all available entity directories."""
        self.ensure_directories()
        lines = [
            "# Company Brain Knowledge Wiki",
            "",
            "Welcome to the synchronized LLM Living Wiki for the company knowledge base.",
            "All pages are automatically derived from verified canonical data records and linked with FalkorDB.",
            "",
            "## Available Entity Catalogs",
            "",
        ]

        for cfg in SCHEMA_CONFIG.values():
            folder = cfg.get("wiki_folder")
            if not folder:
                continue
            label = cfg["label"]
            target_dir = self.wiki_dir / folder
            page_count = len(list(target_dir.glob("*.md"))) if target_dir.exists() else 0
            lines.append(f"- **[{label} Catalog]({folder}/)**: {page_count} verified pages")

        lines.append("")
        lines.append("---")
        lines.append(f"*Last updated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%SZ')}*")

        with open(self.wiki_dir / "index.md", "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
