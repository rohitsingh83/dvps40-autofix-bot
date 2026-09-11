"""
services/incident_store.py — In-memory incident audit log & state tracking for the Control Center UI.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any, Optional


@dataclass
class IncidentRecord:
    id: str
    platform: str
    project_name: str
    environment: str
    error_type: str
    error_message: str
    created_at: float = field(default_factory=time.time)
    status: str = "RECEIVED"  # RECEIVED, PARSED, SOURCE_FETCHED, ANALYZED, REVIEWED, PR_CREATED, NOTIFIED, FAILED
    affected_files: list[str] = field(default_factory=list)
    stack_trace: str = ""
    root_cause: str = ""
    steps: list[str] = field(default_factory=list)
    file_path: str = ""
    original_snippet: str = ""
    fixed_snippet: str = ""
    explanation: str = ""
    pr_url: str = ""
    pr_branch: str = ""
    review_approved: bool = False
    review_issues: list[str] = field(default_factory=list)
    validation_checks: list[dict] = field(default_factory=list)
    telegram_message: str = ""
    error_detail: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class IncidentStore:
    """Thread-safe and async-friendly in-memory incident repository."""

    def __init__(self, max_items: int = 100):
        self._max_items = max_items
        self._items: dict[str, IncidentRecord] = {}
        self._order: list[str] = []

    def record_received(
        self,
        deployment_id: str,
        platform: str,
        project_name: str,
        environment: str,
        error_type: str = "Unknown",
        error_message: str = "",
        stack_trace: str = "",
        affected_files: list[str] | None = None,
    ) -> IncidentRecord:
        record = IncidentRecord(
            id=deployment_id,
            platform=platform,
            project_name=project_name,
            environment=environment,
            error_type=error_type,
            error_message=error_message,
            stack_trace=stack_trace,
            affected_files=affected_files or [],
            status="RECEIVED",
        )
        if deployment_id in self._items:
            self._order.remove(deployment_id)
        elif len(self._order) >= self._max_items:
            oldest = self._order.pop(0)
            self._items.pop(oldest, None)

        self._items[deployment_id] = record
        self._order.append(deployment_id)
        return record

    def update_stage(self, deployment_id: str, status: str, **kwargs) -> Optional[IncidentRecord]:
        record = self._items.get(deployment_id)
        if not record:
            return None
        record.status = status
        for k, v in kwargs.items():
            if hasattr(record, k):
                setattr(record, k, v)
        return record

    def get(self, deployment_id: str) -> Optional[IncidentRecord]:
        return self._items.get(deployment_id)

    def list_all(self) -> list[dict[str, Any]]:
        # Returns newest first
        return [self._items[did].to_dict() for did in reversed(self._order)]


# Global singleton
incident_store = IncidentStore()
