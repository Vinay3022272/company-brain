"""
collect_data.py — LangGraph node for chunked, parallel data collection with Step 1 validation.
Extracts canonical records across all enterprise CSV files with change detection,
batch checkpointing, conflict quarantine, and unresolved foreign-key tracking.
"""
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from ...config import DATA_DIR, logger
from ...ingestion import (
    CanonicalRecord,
    execute_incremental_ingestion_batch,
    process_file_with_validation,
)
from ...schema import SCHEMA_CONFIG
from ..state import WikiState


def collect_data(state: WikiState) -> Dict[str, Any]:
    """
    LangGraph Node: Collect and parse company data in chunks across files with Step 1 validation.

    Performs:
    1. Checkpoint & batch ID initialization.
    2. In-chunk conflict & duplicate quarantine.
    3. Delta classification: added, updated (with field diffs), unchanged.
    4. Detection of missing parents / unresolved foreign-keys.
    5. Storage into WikiState for downstream graph synchronization.
    """
    logger.info("--- [LangGraph Node: collect_data] Executing Step 1 Ingestion Pipeline ---")

    chunk_size = state.get("chunk_size", 500)
    max_workers = state.get("max_workers", 4)
    data_dir_str = state.get("data_dir")
    data_dir = Path(data_dir_str) if data_dir_str else DATA_DIR
    batch_id = state.get("batch_id")

    # Dynamically determine files to process
    files_to_process = state.get("files_to_process")
    if not files_to_process:
        files_to_process = list(SCHEMA_CONFIG.keys())
        if (data_dir / "relationships.csv").exists():
            files_to_process.append("relationships.csv")

    logger.info(f"Ingesting {len(files_to_process)} CSV files with chunk_size={chunk_size}, workers={max_workers}")

    # Run the Step 1 validated batch ingestion engine
    batch_result = execute_incremental_ingestion_batch(
        filenames=files_to_process,
        data_dir=data_dir,
        chunk_size=chunk_size,
        batch_id=batch_id,
        max_workers=max_workers,
        persist_manifest=True,
    )

    records_by_type = batch_result["records_by_type"]
    relationships = batch_result["relationships"]
    all_hashes = batch_result["current_hashes"]
    quarantined = batch_result["quarantined"]
    unresolved_fks = batch_result["unresolved_foreign_keys"]
    now_resolved_fks = batch_result["now_resolved_foreign_keys"]
    chunk_meta = batch_result["chunk_metadata"]
    errors = batch_result["errors"]
    stats = batch_result["stats"]

    # Compute categorized list of changed entity IDs
    added_ids: List[str] = []
    updated_ids: List[str] = []
    unchanged_ids: List[str] = []

    for label, records in records_by_type.items():
        for rec in records:
            eid = rec["entity_id"]
            ctype = rec.get("change_type", "added")
            if ctype == "added":
                added_ids.append(eid)
            elif ctype == "updated":
                updated_ids.append(eid)
            else:
                unchanged_ids.append(eid)

    changed_summary = {
        "added": added_ids,
        "updated": updated_ids,
        "unchanged": unchanged_ids,
    }

    logger.info(
        f"Data collection complete [Batch {batch_result['batch_id']}]: "
        f"{stats['total_canonical_entities']} entities, {stats['total_relationships']} edges. "
        f"(Added: {len(added_ids)}, Updated: {len(updated_ids)}, Unchanged: {len(unchanged_ids)}, "
        f"Quarantined: {len(quarantined)}, Unresolved FKs: {len(unresolved_fks)})"
    )

    # Return partial state update for LangGraph reducers
    return {
        "batch_id": batch_result["batch_id"],
        "canonical_records": records_by_type,
        "relationships_records": relationships,
        "current_hashes": all_hashes,
        "changed_entities": changed_summary,
        "quarantined_records": quarantined,
        "unresolved_foreign_keys": unresolved_fks,
        "now_resolved_foreign_keys": now_resolved_fks,
        "chunk_metadata": chunk_meta,
        "stats": stats,
        "errors": errors,
        "stage": "data_collected",
    }
