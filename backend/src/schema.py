"""
schema.py — Dynamic schema inspection, inference, and referential validation.
"""
import csv
import json
import hashlib
import logging
from pathlib import Path
from typing import Any, Dict, List, Set, Tuple

from .config import DATA_DIR, logger

# Explicit schema configuration derived from CSV dataset
SCHEMA_CONFIG: Dict[str, Dict[str, Any]] = {
    "departments.csv": {
        "label": "Department",
        "pk": "department_id",
        "display_name": "department_name",
        "wiki_folder": "departments",
        "fk": {
            "head_employee_id": ("Employee", "employee_id"),
        },
        "properties": ["department_id", "department_name", "mission", "head_employee_id", "created_at"],
    },
    "teams.csv": {
        "label": "Team",
        "pk": "team_id",
        "display_name": "team_name",
        "wiki_folder": "teams",
        "fk": {
            "department_id": ("Department", "department_id"),
            "manager_employee_id": ("Employee", "employee_id"),
        },
        "properties": ["team_id", "team_name", "department_id", "manager_employee_id", "mission", "primary_tools", "created_at"],
    },
    "employees.csv": {
        "label": "Employee",
        "pk": "employee_id",
        "display_name": "full_name",
        "wiki_folder": "employees",
        "fk": {
            "department_id": ("Department", "department_id"),
            "team_id": ("Team", "team_id"),
            "manager_id": ("Employee", "employee_id"),
        },
        "properties": [
            "employee_id", "full_name", "job_title", "department_id", "team_id",
            "manager_id", "work_email", "location", "skills", "expertise_level",
            "responsibilities", "joined_date", "employment_status", "created_at"
        ],
    },
    "projects.csv": {
        "label": "Project",
        "pk": "project_id",
        "display_name": "project_name",
        "wiki_folder": "projects",
        "fk": {
            "owner_employee_id": ("Employee", "employee_id"),
            "sponsor_employee_id": ("Employee", "employee_id"),
            "primary_team_id": ("Team", "team_id"),
        },
        "properties": [
            "project_id", "project_name", "description", "business_goal", "status",
            "priority", "owner_employee_id", "sponsor_employee_id", "primary_team_id",
            "start_date", "target_date", "success_metrics", "created_at"
        ],
    },
    "work_items.csv": {
        "label": "WorkItem",
        "pk": "work_item_id",
        "display_name": "title",
        "wiki_folder": "work_items",
        "fk": {
            "project_id": ("Project", "project_id"),
            "parent_work_item_id": ("WorkItem", "work_item_id"),
            "assignee_employee_id": ("Employee", "employee_id"),
            "reporter_employee_id": ("Employee", "employee_id"),
            "owning_team_id": ("Team", "team_id"),
            "depends_on_work_item_id": ("WorkItem", "work_item_id"),
        },
        "properties": [
            "work_item_id", "project_id", "parent_work_item_id", "work_type",
            "title", "description", "status", "priority", "assignee_employee_id",
            "reporter_employee_id", "owning_team_id", "depends_on_work_item_id",
            "created_at", "updated_at", "acceptance_criteria"
        ],
    },
    "tickets.csv": {
        "label": "Ticket",
        "pk": "ticket_id",
        "display_name": "title",
        "wiki_folder": "tickets",
        "fk": {
            "reporter_employee_id": ("Employee", "employee_id"),
            "assignee_employee_id": ("Employee", "employee_id"),
            "resolved_by_employee_id": ("Employee", "employee_id"),
            "project_id": ("Project", "project_id"),
            "work_item_id": ("WorkItem", "work_item_id"),
            "source_system_id": ("SourceSystem", "source_system_id"),
        },
        "properties": [
            "ticket_id", "title", "description", "ticket_type", "status",
            "priority", "severity", "reporter_employee_id", "assignee_employee_id",
            "resolved_by_employee_id", "project_id", "work_item_id",
            "affected_system", "root_cause", "resolution_summary", "customer_impact",
            "created_at", "resolved_at", "source_system", "tags", "source_system_id"
        ],
    },
    "incidents.csv": {
        "label": "Incident",
        "pk": "incident_id",
        "display_name": "title",
        "wiki_folder": "incidents",
        "fk": {
            "incident_commander_employee_id": ("Employee", "employee_id"),
            "related_ticket_id": ("Ticket", "ticket_id"),
            "project_id": ("Project", "project_id"),
            "postmortem_document_id": ("Document", "document_id"),
        },
        "properties": [
            "incident_id", "title", "severity", "status", "summary", "affected_system",
            "incident_commander_employee_id", "related_ticket_id", "project_id",
            "root_cause", "mitigation", "follow_up_action", "started_at", "resolved_at",
            "postmortem_document_id"
        ],
    },
    "decisions.csv": {
        "label": "Decision",
        "pk": "decision_id",
        "display_name": "title",
        "wiki_folder": "decisions",
        "fk": {
            "decision_owner_employee_id": ("Employee", "employee_id"),
            "approver_employee_id": ("Employee", "employee_id"),
            "project_id": ("Project", "project_id"),
            "related_ticket_id": ("Ticket", "ticket_id"),
            "source_document_id": ("Document", "document_id"),
            "supersedes_decision_id": ("Decision", "decision_id"),
        },
        "properties": [
            "decision_id", "title", "decision_statement", "context", "rationale",
            "alternatives_considered", "consequences", "status", "decision_owner_employee_id",
            "approver_employee_id", "project_id", "related_ticket_id", "effective_date",
            "review_date", "source_document_id", "supersedes_decision_id"
        ],
    },
    "documents.csv": {
        "label": "Document",
        "pk": "document_id",
        "display_name": "title",
        "wiki_folder": "documents",
        "fk": {
            "author_employee_id": ("Employee", "employee_id"),
            "owner_employee_id": ("Employee", "employee_id"),
            "project_id": ("Project", "project_id"),
            "supersedes_document_id": ("Document", "document_id"),
            "source_system_id": ("SourceSystem", "source_system_id"),
        },
        "properties": [
            "document_id", "title", "document_type", "summary", "body",
            "author_employee_id", "owner_employee_id", "project_id",
            "visibility", "status", "version", "created_at", "updated_at",
            "effective_from", "effective_to", "supersedes_document_id",
            "source_uri", "source_system_id"
        ],
    },
    "comments.csv": {
        "label": "Comment",
        "pk": "comment_id",
        "display_name": "body",
        "wiki_folder": "comments",
        "fk": {
            "author_employee_id": ("Employee", "employee_id"),
            "ticket_id": ("Ticket", "ticket_id"),
            "project_id": ("Project", "project_id"),
        },
        "properties": [
            "comment_id", "author_employee_id", "comment_type", "body",
            "ticket_id", "project_id", "created_at", "visibility", "mentions_employee_ids"
        ],
    },
    "knowledge_entries.csv": {
        "label": "KnowledgeEntry",
        "pk": "knowledge_id",
        "display_name": "title",
        "wiki_folder": "knowledge",
        "fk": {
            "owner_employee_id": ("Employee", "employee_id"),
            "team_id": ("Team", "team_id"),
            "project_id": ("Project", "project_id"),
            "source_ticket_id": ("Ticket", "ticket_id"),
            "source_document_id": ("Document", "document_id"),
            "supersedes_knowledge_id": ("KnowledgeEntry", "knowledge_id"),
        },
        "properties": [
            "knowledge_id", "title", "knowledge_type", "content", "owner_employee_id",
            "team_id", "project_id", "source_ticket_id", "source_document_id",
            "confidence", "review_status", "last_reviewed_at", "created_at",
            "valid_from", "valid_to", "supersedes_knowledge_id"
        ],
    },
    "team_memberships.csv": {
        "label": "TeamMembership",
        "pk": "membership_id",
        "display_name": "membership_id",
        "wiki_folder": None,
        "is_junction": True,
        "fk": {
            "employee_id": ("Employee", "employee_id"),
            "team_id": ("Team", "team_id"),
        },
        "properties": ["membership_id", "employee_id", "team_id", "role", "valid_from", "valid_to", "is_primary"],
    },
    "project_contributions.csv": {
        "label": "ProjectContribution",
        "pk": "contribution_id",
        "display_name": "contribution_id",
        "wiki_folder": None,
        "is_junction": True,
        "fk": {
            "project_id": ("Project", "project_id"),
            "employee_id": ("Employee", "employee_id"),
        },
        "properties": ["contribution_id", "project_id", "employee_id", "role", "contribution_summary", "start_date", "end_date"],
    },
    "source_systems.csv": {
        "label": "SourceSystem",
        "pk": "source_system_id",
        "display_name": "name",
        "wiki_folder": "source_systems",
        "fk": {},
        "properties": ["source_system_id", "name", "source_type", "trust_level"],
    },
}

# Mapping of FK column name to standard relationship edge label
FK_TO_RELATIONSHIP = {
    "head_employee_id": "LED_BY",
    "manager_employee_id": "MANAGED_BY",
    "manager_id": "REPORTS_TO",
    "department_id": "BELONGS_TO",
    "team_id": "MEMBER_OF",
    "owner_employee_id": "OWNS",
    "sponsor_employee_id": "SPONSORED_BY",
    "primary_team_id": "PRIMARILY_DELIVERED_BY",
    "project_id": "PART_OF",
    "parent_work_item_id": "CHILD_OF",
    "depends_on_work_item_id": "DEPENDS_ON",
    "assignee_employee_id": "ASSIGNED_TO",
    "reporter_employee_id": "REPORTED_BY",
    "resolved_by_employee_id": "RESOLVED",
    "work_item_id": "TRACKED_BY",
    "related_ticket_id": "TRACKED_BY",
    "incident_commander_employee_id": "COMMANDED",
    "postmortem_document_id": "DOCUMENTED_BY",
    "decision_owner_employee_id": "MADE",
    "approver_employee_id": "APPROVED_BY",
    "source_document_id": "SUPPORTED_BY",
    "supersedes_decision_id": "SUPERSEDES",
    "supersedes_document_id": "SUPERSEDES",
    "supersedes_knowledge_id": "SUPERSEDES",
    "source_ticket_id": "DERIVED_FROM",
    "source_system_id": "SOURCED_FROM",
    "author_employee_id": "AUTHORED",
    "owning_team_id": "OWNED_BY_TEAM",
}



def load_csv(filename: str) -> List[Dict[str, str]]:
    """Load a CSV file from DATA_DIR as list of dicts."""
    path = DATA_DIR / filename
    if not path.exists():
        logger.warning(f"File not found: {path}")
        return []
    with open(path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return list(reader)
