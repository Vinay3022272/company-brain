"""
collect_data.py — LangGraph node for chunked, parallel data collection.
Extracts canonical records across all enterprise CSV files with change detection.
"""
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from ...config import DATA_DIR, logger
from ...ingestion import (
    CanonicalRecord,
    extract_all_data_parallel,
    process_single_csv_file,
)
from ...schema import SCHEMA_CONFIG
from ..state import WikiState


def collect_data(state: WikiState) -> Dict[str, Any]:
    """
    LangGraph Node: Collect and parse company data in chunks across files.

    Performs:
    1. Parallel chunked streaming of all configured CSV files (15 files + relationships).
    2. Conversion to CanonicalRecord structures with foreign key extraction.
    3. Change detection comparing against state['previous_hashes'].
    4. Saving canonical records, relationship edges, and chunk metadata into state.
    """
    logger.info("--- [LangGraph Node] collect_data: Starting chunked data ingestion ---")

    chunk_size = state.get("chunk_size", 500)
    max_workers = state.get("max_workers", 4)
    data_dir_str = state.get("data_dir")
    data_dir = Path(data_dir_str) if data_dir_str else DATA_DIR
    previous_hashes = state.get("previous_hashes") or {}

    # Dynamically determine files to process
    files_to_process = state.get("files_to_process")
    if not files_to_process:
        # Generic: all schema-configured CSV files plus relationships.csv
        files_to_process = list(SCHEMA_CONFIG.keys())
        if (data_dir / "relationships.csv").exists():
            files_to_process.append("relationships.csv")

    logger.info(f"Targeting {len(files_to_process)} CSV files with chunk_size={chunk_size}, workers={max_workers}")

    # Execute parallel chunked ingestion
    extraction_result = extract_all_data_parallel(
        filenames=files_to_process,
        data_dir=data_dir,
        chunk_size=chunk_size,
        previous_hashes=previous_hashes,
        max_workers=max_workers,
    )

    records_by_type = extraction_result["records_by_type"]
    relationships = extraction_result["relationships"]
    all_hashes = extraction_result["hashes"]
    chunk_meta = extraction_result["chunk_metadata"]
    errors = extraction_result["errors"]
    stats = extraction_result["stats"]

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
        f"Data collection completed: {stats['total_canonical_entities']} entities, "
        f"{stats['total_relationships']} edges across {stats['total_chunks']} chunks. "
        f"(Added: {len(added_ids)}, Updated: {len(updated_ids)}, Unchanged: {len(unchanged_ids)})"
    )

    # Return partial state update for LangGraph reducers
    return {
        "canonical_records": records_by_type,
        "relationships_records": relationships,
        "current_hashes": all_hashes,
        "changed_entities": changed_summary,
        "chunk_metadata": chunk_meta,
        "stats": stats,
        "errors": errors,
        "stage": "data_collected",
    }


def collect_file_worker(state_payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Sub-worker node for individual file processing if using LangGraph Send() fan-out.
    """
    filename = state_payload["filename"]
    chunk_size = state_payload.get("chunk_size", 500)
    data_dir_str = state_payload.get("data_dir")
    data_dir = Path(data_dir_str) if data_dir_str else DATA_DIR
    previous_hashes = state_payload.get("previous_hashes") or {}

    res = process_single_csv_file(
        filename=filename,
        data_dir=data_dir,
        chunk_size=chunk_size,
        previous_hashes=previous_hashes,
    )

    if res["status"] != "SUCCESS":
        return {
            "errors": [f"File {filename}: {res['status']}"],
            "chunk_metadata": [],
        }

    records_by_type = {}
    relationships = []
    if res.get("is_relationship_file"):
        relationships = res.get("relationships", [])
    else:
        label = res["label"]
        records_by_type[label] = res.get("records", [])

    st = res["stats"]
    meta = [{
        "filename": filename,
        "label": res.get("label", "Relationship" if res.get("is_relationship_file") else "Unknown"),
        "total_rows": st.get("total", 0),
        "chunks": st.get("chunks", 0),
        "added": st.get("added", 0),
        "updated": st.get("updated", 0),
        "unchanged": st.get("unchanged", 0),
    }]

    return {
        "canonical_records": records_by_type,
        "relationships_records": relationships,
        "current_hashes": res.get("hashes", {}),
        "chunk_metadata": meta,
    }
