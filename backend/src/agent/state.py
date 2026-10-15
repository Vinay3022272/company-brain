"""
state.py — LangGraph State definition for the Company Brain Agent.
Implements state reducers for parallel branch execution and incremental data collection.
"""
import operator
from typing import Annotated, Any, Dict, List, Optional, TypedDict


def merge_canonical_records(
    left: Optional[Dict[str, List[Dict[str, Any]]]], 
    right: Optional[Dict[str, List[Dict[str, Any]]]]
) -> Dict[str, List[Dict[str, Any]]]:
    """
    Reducer that merges canonical records grouped by entity_type,
    deduplicating/updating by entity_id.
    """
    if not left:
        return dict(right or {})
    if not right:
        return dict(left)

    result = {k: list(v) for k, v in left.items()}
    for entity_type, new_records in right.items():
        if entity_type not in result:
            result[entity_type] = list(new_records)
        else:
            existing_map = {rec["entity_id"]: idx for idx, rec in enumerate(result[entity_type])}
            for rec in new_records:
                eid = rec.get("entity_id")
                if eid in existing_map:
                    result[entity_type][existing_map[eid]] = rec
                else:
                    result[entity_type].append(rec)
                    existing_map[eid] = len(result[entity_type]) - 1
    return result


def merge_dicts(
    left: Optional[Dict[str, Any]], 
    right: Optional[Dict[str, Any]]
) -> Dict[str, Any]:
    """Reducer that merges dictionary keys safely during parallel execution."""
    merged = dict(left or {})
    if right:
        merged.update(right)
    return merged


def take_latest(left: Any, right: Any) -> Any:
    """Reducer that accepts the latest updated value."""
    return right if right is not None else left


class WikiState(TypedDict, total=False):
    """
    Complete state passed between nodes in the Company Brain LangGraph pipeline.
    All list and dict fields used in parallel branches have reducer annotations.
    """
    # Ingestion Configuration
    chunk_size: int
    data_dir: Optional[str]
    max_workers: int
    files_to_process: List[str]

    # Change Detection & Incremental Watermarks
    previous_hashes: Dict[str, str]
    current_hashes: Annotated[Dict[str, str], merge_dicts]
    changed_entities: Dict[str, List[str]]  # {"added": [...], "updated": [...], "unchanged": [...]}

    # Ingested Canonical Records and Edges
    canonical_records: Annotated[Dict[str, List[Dict[str, Any]]], merge_canonical_records]
    relationships_records: Annotated[List[Dict[str, Any]], operator.add]

    # Parallel Branch Progress
    branch_status: Annotated[Dict[str, str], merge_dicts]

    # Observability & Chunk Metadata
    chunk_metadata: Annotated[List[Dict[str, Any]], operator.add]
    stats: Annotated[Dict[str, Any], merge_dicts]
    errors: Annotated[List[str], operator.add]

    # Pipeline Stage Tracking
    stage: Annotated[str, take_latest]


def create_initial_wiki_state(
    chunk_size: int = 500,
    files_to_process: Optional[List[str]] = None,
    previous_hashes: Optional[Dict[str, str]] = None,
    max_workers: int = 4,
    data_dir: Optional[str] = None,
) -> WikiState:
    """Helper to initialize an empty WikiState with defaults."""
    return {
        "chunk_size": chunk_size,
        "data_dir": data_dir,
        "max_workers": max_workers,
        "files_to_process": files_to_process or [],
        "previous_hashes": previous_hashes or {},
        "current_hashes": {},
        "changed_entities": {"added": [], "updated": [], "unchanged": []},
        "canonical_records": {},
        "relationships_records": [],
        "branch_status": {},
        "chunk_metadata": [],
        "stats": {},
        "errors": [],
        "stage": "initialized",
    }
