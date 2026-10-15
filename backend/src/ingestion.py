"""
ingestion.py — Production-grade chunked ingestion, validation, and change detection engine.

Features:
1. Batch tracking & durable checkpoint manifests (sync_manifest.json).
2. Duplicate & conflict detection with quarantine review queue.
3. Delta classification: ADDED, UPDATED, UNCHANGED, DELETED, with old vs new diffs.
4. Unresolved foreign key tracking (parent missing detection).
5. Safe chunked CSV streaming with deterministic hashing.
"""
import csv
import hashlib
import json
import logging
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional, Set, Tuple

from .config import DATA_DIR, logger
from .schema import FK_TO_RELATIONSHIP, SCHEMA_CONFIG


MANIFEST_PATH = DATA_DIR / "sync_manifest.json"


@dataclass
class CanonicalRecord:
    """Canonical entity record shared by graph store and wiki builder."""
    entity_type: str
    entity_id: str
    display_name: str
    properties: Dict[str, Any]
    foreign_keys: Dict[str, Tuple[str, str, str]]  # fk_col -> (target_label, target_pk, target_val)
    source_file: str
    row_hash: str
    chunk_index: int = 0
    change_type: str = "added"  # "added" | "updated" | "unchanged" | "deleted" | "quarantined"
    previous_hash: Optional[str] = None
    changed_fields: List[str] = field(default_factory=list)
    previous_values: Dict[str, Any] = field(default_factory=dict)
    unresolved_fks: Dict[str, Tuple[str, str, str]] = field(default_factory=dict)
    is_tombstone: bool = False

    def to_dict(self) -> Dict[str, Any]:
        """Serialize record to dictionary for LangGraph state."""
        return {
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "display_name": self.display_name,
            "properties": self.properties,
            "foreign_keys": {
                k: list(v) if isinstance(v, (tuple, list)) else v
                for k, v in self.foreign_keys.items()
            },
            "source_file": self.source_file,
            "row_hash": self.row_hash,
            "chunk_index": self.chunk_index,
            "change_type": self.change_type,
            "previous_hash": self.previous_hash,
            "changed_fields": self.changed_fields,
            "previous_values": self.previous_values,
            "unresolved_fks": {
                k: list(v) if isinstance(v, (tuple, list)) else v
                for k, v in self.unresolved_fks.items()
            },
            "is_tombstone": self.is_tombstone,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CanonicalRecord":
        """Deserialize dictionary back to CanonicalRecord."""
        fks: Dict[str, Tuple[str, str, str]] = {}
        for k, v in data.get("foreign_keys", {}).items():
            if isinstance(v, list) and len(v) == 3:
                fks[k] = (v[0], v[1], v[2])
            elif isinstance(v, tuple):
                fks[k] = v

        unres_fks: Dict[str, Tuple[str, str, str]] = {}
        for k, v in data.get("unresolved_fks", {}).items():
            if isinstance(v, list) and len(v) == 3:
                unres_fks[k] = (v[0], v[1], v[2])
            elif isinstance(v, tuple):
                unres_fks[k] = v

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
            previous_hash=data.get("previous_hash"),
            changed_fields=data.get("changed_fields", []),
            previous_values=data.get("previous_values", {}),
            unresolved_fks=unres_fks,
            is_tombstone=data.get("is_tombstone", False),
        )


def compute_row_hash(row: Dict[str, str]) -> str:
    """Deterministic SHA-256 fingerprint of a single CSV row."""
    sorted_items = sorted(f"{k}:{str(v).strip()}" for k, v in row.items())
    raw = "|".join(sorted_items).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def parse_cell_value(val: Any) -> Any:
    """Parse JSON strings (arrays/objects) or clean scalar values."""
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
    Stream rows from CSV file sequentially in memory-safe chunks.
    Yields (chunk_rows, chunk_index, is_last_chunk).
    """
    if not file_path.exists():
        logger.warning(f"File not found: {file_path}")
        return

    with open(file_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
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

        if chunk:
            yield chunk, chunk_idx, True


# ─────────────────────────────────────────────────────────────────────────────
# Durable Manifest & Checkpoint Management
# ─────────────────────────────────────────────────────────────────────────────

def load_sync_manifest(manifest_path: Optional[Path] = None) -> Dict[str, Any]:
    """Load persistent sync manifest containing past batch history, hashes, and entity states."""
    path = manifest_path or MANIFEST_PATH
    if not path.exists():
        return {
            "version": 1,
            "last_updated": datetime.now(timezone.utc).isoformat(),
            "batches": {},
            "entity_hashes": {},          # entity_id -> row_hash
            "entity_snapshots": {},       # entity_id -> properties dict
            "unresolved_foreign_keys": {},# entity_id -> {fk_col: [target_label, target_pk, target_val]}
            "quarantined_records": [],
            "processed_file_checksums": {},# filename -> md5/sha256
        }
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Error loading manifest from {path}: {e}")
        return {
            "version": 1,
            "last_updated": datetime.now(timezone.utc).isoformat(),
            "batches": {},
            "entity_hashes": {},
            "entity_snapshots": {},
            "unresolved_foreign_keys": {},
            "quarantined_records": [],
            "processed_file_checksums": {},
        }


def save_sync_manifest(manifest_data: Dict[str, Any], manifest_path: Optional[Path] = None):
    """Persist updated sync manifest to disk atomically."""
    path = manifest_path or MANIFEST_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    manifest_data["last_updated"] = datetime.now(timezone.utc).isoformat()
    
    temp_path = path.with_suffix(".tmp")
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(manifest_data, f, indent=2)
    temp_path.replace(path)


# ─────────────────────────────────────────────────────────────────────────────
# Conflict & Duplicate Detection
# ─────────────────────────────────────────────────────────────────────────────

def detect_duplicates_and_conflicts(
    rows: List[Dict[str, str]], 
    pk_col: str, 
    filename: str
) -> Tuple[List[Dict[str, str]], List[Dict[str, Any]]]:
    """
    Validate that IDs within the file chunk do not have conflicting data for the same PK.
    Returns (clean_rows, quarantined_records).
    """
    seen_ids: Dict[str, Dict[str, str]] = {}
    clean_rows: List[Dict[str, str]] = []
    quarantined: List[Dict[str, Any]] = []

    for row in rows:
        entity_id = row.get(pk_col, "").strip()
        if not entity_id:
            quarantined.append({
                "reason": "MISSING_PRIMARY_KEY",
                "filename": filename,
                "row_data": row,
                "detected_at": datetime.now(timezone.utc).isoformat(),
            })
            continue

        if entity_id in seen_ids:
            prev_row = seen_ids[entity_id]
            # Check if all fields match or if there is a conflict
            if prev_row != row:
                conflict_item = {
                    "reason": "DUPLICATE_ID_CONFLICT",
                    "entity_id": entity_id,
                    "filename": filename,
                    "first_occurrence": prev_row,
                    "conflicting_occurrence": row,
                    "detected_at": datetime.now(timezone.utc).isoformat(),
                }
                quarantined.append(conflict_item)
                logger.warning(f"Conflict quarantined for {entity_id} in {filename}")
                continue
            else:
                # Exact identical duplicate line, safely skip duplicate
                continue

        seen_ids[entity_id] = row
        clean_rows.append(row)

    return clean_rows, quarantined


# ─────────────────────────────────────────────────────────────────────────────
# Canonical Chunk Extraction with Diff & Foreign-Key Validation
# ─────────────────────────────────────────────────────────────────────────────

def extract_canonical_chunk_validated(
    filename: str,
    rows: List[Dict[str, str]],
    config: Dict[str, Any],
    chunk_index: int = 0,
    previous_hashes: Optional[Dict[str, str]] = None,
    previous_snapshots: Optional[Dict[str, Dict[str, Any]]] = None,
    known_valid_ids: Optional[Set[str]] = None,
) -> Tuple[List[CanonicalRecord], Dict[str, str], Dict[str, Dict[str, Any]], Dict[str, Any], List[Dict[str, Any]]]:
    """
    Extract CanonicalRecords from a chunk with:
    - Delta classification (added vs updated vs unchanged)
    - Field-by-field diff comparison (changed_fields, previous_values)
    - Unresolved foreign key tracking
    - In-chunk conflict quarantine
    """
    label = config["label"]
    pk_col = config["pk"]
    display_col = config.get("display_name", pk_col)
    fk_map = config.get("fk", {})
    prop_cols = config.get("properties", [])
    
    prev_hashes = previous_hashes or {}
    prev_snapshots = previous_snapshots or {}
    valid_ids = known_valid_ids or set()

    # 1. Deduplicate & quarantine in-chunk conflicts
    clean_rows, quarantined = detect_duplicates_and_conflicts(rows, pk_col, filename)

    records: List[CanonicalRecord] = []
    chunk_hashes: Dict[str, str] = {}
    chunk_snapshots: Dict[str, Dict[str, Any]] = {}
    stats = {"added": 0, "updated": 0, "unchanged": 0, "quarantined": len(quarantined)}

    for row in clean_rows:
        entity_id = row.get(pk_col, "").strip()
        row_h = compute_row_hash(row)
        chunk_hashes[entity_id] = row_h

        # Extract properties
        properties: Dict[str, Any] = {}
        for col in prop_cols:
            properties[col] = parse_cell_value(row.get(col, ""))
        chunk_snapshots[entity_id] = properties

        # Extract Foreign Keys and identify unresolved parents
        fks: Dict[str, Tuple[str, str, str]] = {}
        unresolved_fks: Dict[str, Tuple[str, str, str]] = {}

        for fk_col, (target_label, target_pk) in fk_map.items():
            val = row.get(fk_col, "").strip()
            if val:
                fk_tuple = (target_label, target_pk, val)
                fks[fk_col] = fk_tuple
                
                # If known_valid_ids is populated and target is not in it, mark unresolved
                if valid_ids and val not in valid_ids:
                    unresolved_fks[fk_col] = fk_tuple

        # Clean display name
        display_name = str(row.get(display_col, entity_id)).strip()
        if len(display_name) > 120:
            display_name = display_name[:117] + "..."

        # Delta classification
        prev_h = prev_hashes.get(entity_id)
        changed_fields = []
        prev_values = {}

        if prev_h is None:
            change_type = "added"
            stats["added"] += 1
        elif prev_h != row_h:
            change_type = "updated"
            stats["updated"] += 1
            # Compute exact field-level diff
            old_snap = prev_snapshots.get(entity_id, {})
            for k, new_v in properties.items():
                old_v = old_snap.get(k)
                if old_v != new_v:
                    changed_fields.append(k)
                    prev_values[k] = old_v
        else:
            change_type = "unchanged"
            stats["unchanged"] += 1

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
            previous_hash=prev_h,
            changed_fields=changed_fields,
            previous_values=prev_values,
            unresolved_fks=unresolved_fks,
            is_tombstone=False,
        )
        records.append(rec)

    return records, chunk_hashes, chunk_snapshots, stats, quarantined


# ─────────────────────────────────────────────────────────────────────────────
# Full Parallel File Ingestion with Batch ID & Manifest Integration
# ─────────────────────────────────────────────────────────────────────────────

def process_file_with_validation(
    filename: str,
    batch_id: str,
    data_dir: Optional[Path] = None,
    chunk_size: int = 500,
    previous_hashes: Optional[Dict[str, str]] = None,
    previous_snapshots: Optional[Dict[str, Dict[str, Any]]] = None,
    known_valid_ids: Optional[Set[str]] = None,
) -> Dict[str, Any]:
    """Process a single CSV file with full validation, chunking, and diff tracking."""
    base_dir = data_dir or DATA_DIR
    file_path = base_dir / filename

    if not file_path.exists():
        return {
            "filename": filename,
            "status": "NOT_FOUND",
            "records": [],
            "hashes": {},
            "snapshots": {},
            "quarantined": [],
            "stats": {"added": 0, "updated": 0, "unchanged": 0, "quarantined": 0, "total": 0, "chunks": 0},
        }

    # Handle relationships.csv
    if filename == "relationships.csv":
        from .ingestion import extract_relationships_file
        rels, meta = extract_relationships_file(file_path, chunk_size=chunk_size)
        return {
            "filename": filename,
            "status": "SUCCESS",
            "is_relationship_file": True,
            "relationships": rels,
            "hashes": {r["relationship_id"]: r["row_hash"] for r in rels},
            "snapshots": {},
            "quarantined": [],
            "stats": {
                "total": meta["total_relationships"],
                "chunks": meta["chunks"],
                "added": meta["total_relationships"],
                "updated": 0,
                "unchanged": 0,
                "quarantined": 0,
            },
        }

    config = SCHEMA_CONFIG.get(filename)
    if not config:
        return {
            "filename": filename,
            "status": "NO_SCHEMA_CONFIG",
            "records": [],
            "hashes": {},
            "snapshots": {},
            "quarantined": [],
            "stats": {"added": 0, "updated": 0, "unchanged": 0, "quarantined": 0, "total": 0, "chunks": 0},
        }

    all_records: List[CanonicalRecord] = []
    file_hashes: Dict[str, str] = {}
    file_snapshots: Dict[str, Dict[str, Any]] = {}
    file_quarantined: List[Dict[str, Any]] = []
    aggregated_stats = {"added": 0, "updated": 0, "unchanged": 0, "quarantined": 0, "total": 0, "chunks": 0}

    for chunk_rows, chunk_idx, _ in stream_csv_chunks(file_path, chunk_size=chunk_size):
        records, chunk_hashes, chunk_snaps, chunk_stats, quarantined = extract_canonical_chunk_validated(
            filename=filename,
            rows=chunk_rows,
            config=config,
            chunk_index=chunk_idx,
            previous_hashes=previous_hashes,
            previous_snapshots=previous_snapshots,
            known_valid_ids=known_valid_ids,
        )
        all_records.extend(records)
        file_hashes.update(chunk_hashes)
        file_snapshots.update(chunk_snaps)
        file_quarantined.extend(quarantined)

        aggregated_stats["added"] += chunk_stats["added"]
        aggregated_stats["updated"] += chunk_stats["updated"]
        aggregated_stats["unchanged"] += chunk_stats["unchanged"]
        aggregated_stats["quarantined"] += chunk_stats["quarantined"]
        aggregated_stats["total"] += len(records)
        aggregated_stats["chunks"] += 1

    return {
        "filename": filename,
        "label": config["label"],
        "status": "SUCCESS",
        "is_relationship_file": False,
        "records": [r.to_dict() for r in all_records],
        "hashes": file_hashes,
        "snapshots": file_snapshots,
        "quarantined": file_quarantined,
        "stats": aggregated_stats,
    }


def execute_incremental_ingestion_batch(
    filenames: Optional[List[str]] = None,
    data_dir: Optional[Path] = None,
    chunk_size: int = 500,
    batch_id: Optional[str] = None,
    max_workers: int = 4,
    persist_manifest: bool = True,
) -> Dict[str, Any]:
    """
    Main entrypoint for Step 1 Ingestion:
    - Generates or tracks batch_id
    - Loads existing sync_manifest checkpoints
    - Processes files concurrently with conflict detection & diff classification
    - Resolves pending foreign-keys against newly discovered entities
    - Updates and persists sync_manifest
    """
    active_batch_id = batch_id or f"batch_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{str(uuid.uuid4())[:8]}"
    manifest = load_sync_manifest()
    
    prev_hashes = manifest.get("entity_hashes", {})
    prev_snapshots = manifest.get("entity_snapshots", {})
    existing_unresolved = manifest.get("unresolved_foreign_keys", {})
    
    known_valid_ids = set(prev_hashes.keys())

    target_files = filenames or (list(SCHEMA_CONFIG.keys()) + ["relationships.csv"])
    logger.info(f"Starting Ingestion Batch [{active_batch_id}]: {len(target_files)} files (chunk_size={chunk_size})")

    records_by_type: Dict[str, List[Dict[str, Any]]] = {}
    all_relationships: List[Dict[str, Any]] = []
    new_hashes: Dict[str, str] = {}
    new_snapshots: Dict[str, Dict[str, Any]] = {}
    all_quarantined: List[Dict[str, Any]] = []
    chunk_audit_metadata: List[Dict[str, Any]] = []
    errors: List[str] = []

    overall_stats = {
        "batch_id": active_batch_id,
        "total_files": len(target_files),
        "total_canonical_entities": 0,
        "total_relationships": 0,
        "total_chunks": 0,
        "added": 0,
        "updated": 0,
        "unchanged": 0,
        "quarantined": 0,
    }

    # Parallel file processing
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_file = {
            executor.submit(
                process_file_with_validation,
                fname,
                active_batch_id,
                data_dir,
                chunk_size,
                prev_hashes,
                prev_snapshots,
                known_valid_ids,
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

                new_hashes.update(res["hashes"])
                new_snapshots.update(res.get("snapshots", {}))
                all_quarantined.extend(res.get("quarantined", []))

                st = res["stats"]
                overall_stats["total_chunks"] += st.get("chunks", 0)
                overall_stats["added"] += st.get("added", 0)
                overall_stats["updated"] += st.get("updated", 0)
                overall_stats["unchanged"] += st.get("unchanged", 0)
                overall_stats["quarantined"] += st.get("quarantined", 0)

                chunk_audit_metadata.append({
                    "filename": fname,
                    "label": res.get("label", "Relationship" if res.get("is_relationship_file") else "Unknown"),
                    "total_rows": st.get("total", 0),
                    "chunks": st.get("chunks", 0),
                    "added": st.get("added", 0),
                    "updated": st.get("updated", 0),
                    "unchanged": st.get("unchanged", 0),
                    "quarantined": st.get("quarantined", 0),
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

    # 4. Check for resolved foreign keys from existing manifest
    all_current_ids = set(new_hashes.keys()) | known_valid_ids
    now_resolved_fks: Dict[str, Any] = {}
    remaining_unresolved_fks: Dict[str, Any] = {}

    for eid, fks in existing_unresolved.items():
        pending_for_entity = {}
        for fk_col, fk_info in fks.items():
            target_val = fk_info[2]
            if target_val in all_current_ids:
                if eid not in now_resolved_fks:
                    now_resolved_fks[eid] = []
                now_resolved_fks[eid].append((fk_col, fk_info))
            else:
                pending_for_entity[fk_col] = fk_info
        if pending_for_entity:
            remaining_unresolved_fks[eid] = pending_for_entity

    # Also collect unresolved FKs from this batch
    for label, records in records_by_type.items():
        for rec in records:
            unres = rec.get("unresolved_fks", {})
            if unres:
                remaining_unresolved_fks[rec["entity_id"]] = unres

    # 5. Update and persist manifest checkpoint
    if persist_manifest:
        updated_hashes = dict(prev_hashes)
        updated_hashes.update(new_hashes)

        updated_snapshots = dict(prev_snapshots)
        updated_snapshots.update(new_snapshots)

        manifest["entity_hashes"] = updated_hashes
        manifest["entity_snapshots"] = updated_snapshots
        manifest["unresolved_foreign_keys"] = remaining_unresolved_fks
        manifest["quarantined_records"].extend(all_quarantined)
        manifest["batches"][active_batch_id] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "stats": overall_stats,
            "errors": errors,
            "target_files": target_files,
        }
        save_sync_manifest(manifest)

    logger.info(
        f"Batch [{active_batch_id}] complete: {overall_stats['total_canonical_entities']} entities, "
        f"{overall_stats['total_relationships']} edges. (Added: {overall_stats['added']}, "
        f"Updated: {overall_stats['updated']}, Unchanged: {overall_stats['unchanged']}, "
        f"Quarantined: {overall_stats['quarantined']})"
    )

    return {
        "batch_id": active_batch_id,
        "records_by_type": records_by_type,
        "relationships": all_relationships,
        "current_hashes": new_hashes,
        "snapshots": new_snapshots,
        "quarantined": all_quarantined,
        "unresolved_foreign_keys": remaining_unresolved_fks,
        "now_resolved_foreign_keys": now_resolved_fks,
        "chunk_metadata": chunk_audit_metadata,
        "stats": overall_stats,
        "errors": errors,
    }


def extract_relationships_file(
    file_path: Path,
    chunk_size: int = 1000,
    offset: int = 0
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Extract explicit relationship edges from relationships.csv in chunks."""
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


def extract_all_data_parallel(
    filenames: Optional[List[str]] = None,
    data_dir: Optional[Path] = None,
    chunk_size: int = 500,
    previous_hashes: Optional[Dict[str, str]] = None,
    max_workers: int = 4,
) -> Dict[str, Any]:
    """Compatibility bridge calling the validated batch ingestion engine."""
    return execute_incremental_ingestion_batch(
        filenames=filenames,
        data_dir=data_dir,
        chunk_size=chunk_size,
        max_workers=max_workers,
        persist_manifest=True,
    )


def extract_canonical_records() -> Dict[str, List[CanonicalRecord]]:
    """Backwards-compatible extraction returning {label: [CanonicalRecord, ...]}."""
    result = execute_incremental_ingestion_batch()
    typed_records: Dict[str, List[CanonicalRecord]] = {}
    for label, dict_list in result["records_by_type"].items():
        typed_records[label] = [CanonicalRecord.from_dict(d) for d in dict_list]
    return typed_records
