"""Team-editable tracking data for issues.

Stored separately from the JIRA-synced state so that manual annotations
(QA status, analysis, reproduction steps, test coverage notes) are never
overwritten by automated syncs.

tracking.json schema:
{
  "issues": {
    "<issue_id_or_jira_key>": {
      "qa_status": "not_assessed|needs_analysis|reproducing|test_written|verified|wont_fix",
      "qa_assignee": "name",
      "internal_priority": "P0|P1|P2|P3",
      "analysis": "Root cause analysis text...",
      "repro_steps": "Steps to reproduce...",
      "test_coverage": "Existing coverage notes, or what test to add...",
      "hotfix_status": "requested|delivered|not_needed",
      "notes": "Free-form notes..."
    }
  }
}
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

QA_STATUSES = [
    "not_assessed",
    "needs_analysis",
    "reproducing",
    "test_written",
    "verified",
    "wont_fix",
]

QA_STATUS_LABELS = {
    "not_assessed": "Not Assessed",
    "needs_analysis": "Needs Analysis",
    "reproducing": "Reproducing",
    "test_written": "Test Written",
    "verified": "Verified",
    "wont_fix": "Won't Fix",
}

INTERNAL_PRIORITIES = ["P0", "P1", "P2", "P3", ""]

HOTFIX_STATUSES = ["requested", "delivered", "not_needed", ""]


class TrackingDB:
    """Manages the team-editable tracking data."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or Path.home() / ".ceph-prio-hub" / "tracking.json"
        self._data: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        if self._path.exists():
            try:
                raw = json.loads(self._path.read_text(encoding="utf-8"))
                self._data = raw.get("issues", {})
            except (json.JSONDecodeError, KeyError):
                logger.warning("Corrupt tracking.json, starting fresh")
                self._data = {}
        else:
            self._data = {}

    def save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"issues": self._data}
        self._path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

    def get(self, issue_id: str) -> dict[str, Any]:
        """Get tracking data for an issue. Returns defaults if not found."""
        return self._data.get(issue_id, _defaults())

    def get_by_jira_key(self, jira_key: str) -> dict[str, Any]:
        """Look up by JIRA key (stored in the data as a lookup key)."""
        if jira_key in self._data:
            return self._data[jira_key]
        return _defaults()

    def set(self, issue_id: str, fields: dict[str, Any]) -> None:
        """Update tracking fields for an issue."""
        existing = self._data.get(issue_id, _defaults())
        for key in ("qa_status", "qa_assignee", "internal_priority", "analysis",
                     "repro_steps", "test_coverage", "hotfix_status", "notes"):
            if key in fields:
                existing[key] = fields[key]
        self._data[issue_id] = existing

    def all_tracked(self) -> dict[str, dict[str, Any]]:
        return dict(self._data)

    def stats(self) -> dict[str, int]:
        """QA status distribution across all tracked issues."""
        from collections import Counter
        counts = Counter(v.get("qa_status", "not_assessed") for v in self._data.values())
        return dict(counts)


def _defaults() -> dict[str, Any]:
    return {
        "qa_status": "not_assessed",
        "qa_assignee": "",
        "internal_priority": "",
        "analysis": "",
        "repro_steps": "",
        "test_coverage": "",
        "hotfix_status": "",
        "notes": "",
    }
