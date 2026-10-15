"""
ingestion.py — Generic, chunked canonical record extraction, row hashing, and change detection.
Supports scalable enterprise ingestion with chunking, change detection, and parallel processing.
"""
import csv
import hashlib
import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional, Set, Tuple

from .config import DATA_DIR, logger
from .schema import SCHEMA_CONFIG


@dataclass
class CanonicalRecord:
    """Canonical representation of an entity record shared by graph and wiki."""
    entity_type: str
    entity_id: str
    display_name: str
    properties: Dict[str, Any]
    foreign_keys: Dict[str, Tuple[str, str, str]]  # fk_col -> (target_label, target_pk, target_val)
    source_file: str
    row_hash: str
    chunk_index: int = 0
    change_type: str = "added"  # "added" | "updated" | "unchanged"

    def to_dict(self) -> Dict[str, Any]:
        """Convert record to a JSON-serializable dictionary for LangGraph state."""
        return {
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "display_name": self.display_name,
            "properties": self.properties,
            "foreign_keys": {
                k: list(v) if isinstance(v, tuple) else v 
                for k, v in self.foreign_keys.items()
            },
            "source_file": self.source_file,
            "row_hash": self.row_hash,
            "chunk_index": self.chunk_index,
            "change_type": self.change_type,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CanonicalRecord":
        """Reconstruct CanonicalRecord from state dictionary."""
        fks: Dict[str, Tuple[str, str, str]] = {}
        for k, v in data.get("foreign_keys", {}).items():
            if isinstance(v, list) and len(v) == 3:
                fks[k] = (v[0], v[1], v[2])
            elif isinstance(v, tuple):
                fks[k] = v
        return cls(
            entity_type=data["entity_type"],
            entity_id=data["entity_id"],
            display_name=data["display_name"],
            properties=data.get("properties", {}),
            foreign_keys=fks,
            source_file=data.get("source_file", ""),
            row_hash=data.get("row_hash", ""),
            chunk_index=data.get("chunk_index", 0),
            change_type=data.get("change_type", "added"),
        )


def compute_row_hash(row: Dict[str, str]) -> str:
    """Deterministic hash of a single CSV row for change detection."""
    sorted_items = sorted(f"{k}:{str(v).strip()}" for k, v in row.items())
    raw = "|".join(sorted_items).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def parse_cell_value(val: Any) -> Any:
    """Parse JSON strings (like JSON arrays/objects) or return cleaned value."""
    if val is None:
        return ""
    if not isinstance(val, str):
        return val
    val_clean = val.strip()
    if not val_clean:
        return ""
    if (val_clean.startswith("[") and val_clean.endswith("]")) or (
        val_clean.startswith("{") and val_clean.endswith("}")
    ):
        try:
            return json.loads(val_clean)
        except Exception:
            return val_clean
    return val_clean


def stream_csv_chunks(
    file_path: Path, 
    chunk_size: int = 500, 
    offset: int = 0
) -> Generator[Tuple[List[Dict[str, str]], int, bool], None, None]:
    """
    Stream rows from a CSV file in chunks without loading the entire file into memory.
    Yields (chunk_rows, chunk_index, is_last_chunk).
    """
    if not file_path.exists():
        logger.warning(f"File not found for streaming: {file_path}")
        return

    with open(file_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        
        # Skip rows up to offset
        for _ in range(offset):
            try:
                next(reader)
            except StopIteration:
                return

        chunk: List[Dict[str, str]] = []
        chunk_idx = 0

        for row in reader:
            chunk.append(row)
            if len(chunk) >= chunk_size:
                yield chunk, chunk_idx, False
                chunk = []
                chunk_idx += 1

        # Yield remaining rows
        if chunk:
            yield chunk, chunk_idx, True


def extract_canonical_chunk(
    filename: str,
    rows: List[Dict[str, str]],
    config: Dict[str, Any],
    chunk_index: int = 0,
    previous_hashes: Optional[Dict[str, str]] = None,
) -> Tuple[List[CanonicalRecord], Dict[str, str], Dict[str, int]]:
    """
    Extract canonical records from a single chunk of rows for a specific entity type.
    Tracks changes (added, updated, unchanged) against previous_hashes.
    """
    label = config["label"]
    pk_col = config["pk"]
    display_col = config.get("display_name", pk_col)
    fk_map = config.get("fk", {})
    prop_cols = config.get("properties", [])
    prev_hashes = previous_hashes or {}

    records: List[CanonicalRecord] = []
    chunk_hashes: Dict[str, str] = {}
    stats = {"added": 0, "updated": 0, "unchanged": 0}

    for row in rows:
        entity_id = row.get(pk_col, "").strip()
        if not entity_id:
            continue

        row_h = compute_row_hash(row)
        chunk_hashes[entity_id] = row_h

        # Change classification
        if entity_id not in prev_hashes:
            change_type = "added"
            stats["added"] += 1
        elif prev_hashes[entity_id] != row_h:
            change_type = "updated"
            stats["updated"] += 1
        else:
            change_type = "unchanged"
            stats["unchanged"] += 1

        # Extract properties
        properties: Dict[str, Any] = {}
        for col in prop_cols:
            properties[col] = parse_cell_value(row.get(col, ""))

        # Extract foreign keys
        fks: Dict[str, Tuple[str, str, str]] = {}
        for fk_col, (target_label, target_pk) in fk_map.items():
            val = row.get(fk_col, "").strip()
            if val:
                fks[fk_col] = (target_label, target_pk, val)

        # Clean display name
        display_name = str(row.get(display_col, entity_id)).strip()
        if len(display_name) > 120:
            display_name = display_name[:117] + "..."

        rec = CanonicalRecord(
            entity_type=label,
            entity_id=entity_id,
            display_name=display_name,
            properties=properties,
            foreign_keys=fks,
            source_file=filename,
            row_hash=row_h,
            chunk_index=chunk_index,
            change_type=change_type,
        )
        records.append(rec)

    return records, chunk_hashes, stats


def extract_relationships_file(
    file_path: Path,
    chunk_size: int = 1000,
    offset: int = 0
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    Extract explicit relationship edges from relationships.csv in chunks.
    """
    if not file_path.exists():
        logger.warning(f"Relationships file not found: {file_path}")
        return [], {"total_relationships": 0, "chunks": 0}

    all_relationships: List[Dict[str, Any]] = []
    chunk_count = 0

    for chunk_rows, chunk_idx, _ in stream_csv_chunks(file_path, chunk_size=chunk_size, offset=offset):
        chunk_count += 1
        for row in chunk_rows:
            rel_id = row.get("relationship_id", "").strip()
            if not rel_id:
                continue
            all_relationships.append({
                "relationship_id": rel_id,
                "source_type": row.get("source_type", "").strip(),
                "source_id": row.get("source_id", "").strip(),
                "relationship_type": row.get("relationship_type", "").strip(),
                "target_type": row.get("target_type", "").strip(),
                "target_id": row.get("target_id", "").strip(),
                "valid_from": row.get("valid_from", "").strip() or None,
                "valid_to": row.get("valid_to", "").strip() or None,
                "source_record_id": row.get("source_record_id", "").strip() or None,
                "evidence_text": row.get("evidence_text", "").strip() or None,
                "row_hash": compute_row_hash(row),
                "chunk_index": chunk_idx,
            })

    return all_relationships, {
        "total_relationships": len(all_relationships),
        "chunks": chunk_count,
    }


def process_single_csv_file(
    filename: str,
    data_dir: Optional[Path] = None,
    chunk_size: int = 500,
    previous_hashes: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """
    Process a single CSV file in chunks, dynamically using SCHEMA_CONFIG.
    Designed for parallel execution across multiple threads/nodes.
    """
    base_dir = data_dir or DATA_DIR
    file_path = base_dir / filename

    if not file_path.exists():
        return {
            "filename": filename,
            "status": "NOT_FOUND",
            "records": [],
            "hashes": {},
            "stats": {"added": 0, "updated": 0, "unchanged": 0, "total": 0, "chunks": 0},
        }

    # Handle relationships.csv as a special explicit relationship table
    if filename == "relationships.csv":
        rels, meta = extract_relationships_file(file_path, chunk_size=chunk_size)
        return {
            "filename": filename,
            "status": "SUCCESS",
            "is_relationship_file": True,
            "relationships": rels,
            "hashes": {r["relationship_id"]: r["row_hash"] for r in rels},
            "stats": {
                "total": meta["total_relationships"],
                "chunks": meta["chunks"],
                "added": meta["total_relationships"],
                "updated": 0,
                "unchanged": 0,
            },
        }

    config = SCHEMA_CONFIG.get(filename)
    if not config:
        return {
            "filename": filename,
            "status": "NO_SCHEMA_CONFIG",
            "records": [],
            "hashes": {},
            "stats": {"added": 0, "updated": 0, "unchanged": 0, "total": 0, "chunks": 0},
        }

    all_records: List[CanonicalRecord] = []
    file_hashes: Dict[str, str] = {}
    aggregated_stats = {"added": 0, "updated": 0, "unchanged": 0, "total": 0, "chunks": 0}

    for chunk_rows, chunk_idx, _ in stream_csv_chunks(file_path, chunk_size=chunk_size):
        records, chunk_hashes, chunk_stats = extract_canonical_chunk(
            filename=filename,
            rows=chunk_rows,
            config=config,
            chunk_index=chunk_idx,
            previous_hashes=previous_hashes,
        )
        all_records.extend(records)
        file_hashes.update(chunk_hashes)
        aggregated_stats["added"] += chunk_stats["added"]
        aggregated_stats["updated"] += chunk_stats["updated"]
        aggregated_stats["unchanged"] += chunk_stats["unchanged"]
        aggregated_stats["total"] += len(records)
        aggregated_stats["chunks"] += 1

    return {
        "filename": filename,
        "label": config["label"],
        "status": "SUCCESS",
        "is_relationship_file": False,
        "records": [r.to_dict() for r in all_records],
        "hashes": file_hashes,
        "stats": aggregated_stats,
    }


def extract_all_data_parallel(
    filenames: Optional[List[str]] = None,
    data_dir: Optional[Path] = None,
    chunk_size: int = 500,
    previous_hashes: Optional[Dict[str, str]] = None,
    max_workers: int = 4,
) -> Dict[str, Any]:
    """
    Ingest all configured CSV files concurrently in chunks using a thread pool.
    Returns aggregated records grouped by entity_type, relationships, hashes, and audit stats.
    """
    target_files = filenames or (list(SCHEMA_CONFIG.keys()) + ["relationships.csv"])
    logger.info(f"Extracting {len(target_files)} CSV files in parallel (max_workers={max_workers}, chunk_size={chunk_size})...")

    records_by_type: Dict[str, List[Dict[str, Any]]] = {}
    all_relationships: List[Dict[str, Any]] = []
    all_hashes: Dict[str, str] = {}
    chunk_audit_metadata: List[Dict[str, Any]] = []
    overall_stats = {
        "total_files": len(target_files),
        "total_canonical_entities": 0,
        "total_relationships": 0,
        "total_chunks": 0,
        "added": 0,
        "updated": 0,
        "unchanged": 0,
    }
    errors: List[str] = []

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_file = {
            executor.submit(
                process_single_csv_file,
                fname,
                data_dir,
                chunk_size,
                previous_hashes,
            ): fname
            for fname in target_files
        }

        for future in as_completed(future_to_file):
            fname = future_to_file[future]
            try:
                res = future.result()
                if res["status"] != "SUCCESS":
                    errors.append(f"File {fname}: status {res['status']}")
                    continue

                all_hashes.update(res["hashes"])
                st = res["stats"]
                overall_stats["total_chunks"] += st.get("chunks", 0)
                overall_stats["added"] += st.get("added", 0)
                overall_stats["updated"] += st.get("updated", 0)
                overall_stats["unchanged"] += st.get("unchanged", 0)

                chunk_audit_metadata.append({
                    "filename": fname,
                    "label": res.get("label", "Relationship" if res.get("is_relationship_file") else "Unknown"),
                    "total_rows": st.get("total", 0),
                    "chunks": st.get("chunks", 0),
                    "added": st.get("added", 0),
                    "updated": st.get("updated", 0),
                    "unchanged": st.get("unchanged", 0),
                })

                if res.get("is_relationship_file"):
                    all_relationships.extend(res["relationships"])
                    overall_stats["total_relationships"] += len(res["relationships"])
                else:
                    label = res["label"]
                    if label not in records_by_type:
                        records_by_type[label] = []
                    records_by_type[label].extend(res["records"])
                    overall_stats["total_canonical_entities"] += len(res["records"])

            except Exception as e:
                logger.error(f"Error processing {fname}: {e}")
                errors.append(f"Error processing {fname}: {str(e)}")

    return {
        "records_by_type": records_by_type,
        "relationships": all_relationships,
        "hashes": all_hashes,
        "chunk_metadata": chunk_audit_metadata,
        "stats": overall_stats,
        "errors": errors,
    }


def extract_canonical_records() -> Dict[str, List[CanonicalRecord]]:
    """
    Backwards-compatible extraction returning {label: [CanonicalRecord, ...]}.
    Can be used directly by sync pipelines or existing scripts.
    """
    result = extract_all_data_parallel()
    typed_records: Dict[str, List[CanonicalRecord]] = {}
    for label, dict_list in result["records_by_type"].items():
        typed_records[label] = [CanonicalRecord.from_dict(d) for d in dict_list]
    return typed_records
