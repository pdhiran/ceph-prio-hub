"""Issue state tracker — consolidation, persistence, and incremental sync.

Maintains a persistent state database of consolidated issues from prio-list
emails. Groups emails by conversationId and case ID, tracks timelines,
and supports incremental sync (delta-only updates).
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ceph_prio_hub.config import STATE_DIR
from ceph_prio_hub.graph.models import EmailSummary
from ceph_prio_hub.parser.issue_extractor import ExtractedIssueInfo, extract_issue_info

logger = logging.getLogger(__name__)

ISSUES_FILE = STATE_DIR / "issues.json"
SYNC_METADATA_FILE = STATE_DIR / "sync_metadata.json"

_RE_SUBJECT_CLEAN = re.compile(r"^(?:(?:Re|Fwd|Fw)\s*:\s*)+", re.IGNORECASE)


def _clean_subject(subject: str) -> str:
    """Strip Re:/Fwd: prefixes for subject matching."""
    return _RE_SUBJECT_CLEAN.sub("", subject).strip()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ConsolidatedIssue:
    """A single consolidated issue from one or more email threads."""

    def __init__(self, data: dict | None = None) -> None:
        if data:
            self._data = data
        else:
            self._data = {
                "issue_id": uuid.uuid4().hex[:16],
                "case_ids": [],
                "jira_ids": [],
                "conversation_ids": [],
                "message_ids": [],
                "prio_lists": [],
                "first_seen": _now_iso(),
                "last_updated": _now_iso(),
                "subject": "",
                "component": "",
                "components": [],
                "severity": None,
                "ceph_versions": [],
                "jira_labels": [],
                "timeline": [],
                "coverage_status": "unknown",
                "reproduction_steps": "",
                "related_issues": [],
                "error_messages": [],
                "stack_traces": [],
                "health_warnings": [],
            }

    @property
    def issue_id(self) -> str:
        return self._data["issue_id"]

    @property
    def case_ids(self) -> list[str]:
        return self._data["case_ids"]

    @property
    def jira_ids(self) -> list[str]:
        return self._data["jira_ids"]

    @property
    def conversation_ids(self) -> list[str]:
        return self._data["conversation_ids"]

    @property
    def message_ids(self) -> list[str]:
        return self._data["message_ids"]

    @property
    def subject(self) -> str:
        return self._data["subject"]

    @property
    def last_updated(self) -> str:
        return self._data["last_updated"]

    def add_email(self, email: EmailSummary, extracted: ExtractedIssueInfo) -> None:
        """Add an email update to this issue's timeline."""
        if email.message_id in self.message_ids:
            return

        self._data["message_ids"].append(email.message_id)

        if email.conversation_id and email.conversation_id not in self._data["conversation_ids"]:
            self._data["conversation_ids"].append(email.conversation_id)

        for cid in extracted.case_ids:
            if cid not in self._data["case_ids"]:
                self._data["case_ids"].append(cid)

        for jid in extracted.jira_ids:
            if jid not in self._data["jira_ids"]:
                self._data["jira_ids"].append(jid)

        for prio in email.prio_lists:
            if prio not in self._data["prio_lists"]:
                self._data["prio_lists"].append(prio)

        if not self._data["subject"]:
            self._data["subject"] = _clean_subject(email.subject)

        if extracted.components:
            for comp in extracted.components:
                if comp not in self._data["components"]:
                    self._data["components"].append(comp)
            if not self._data["component"]:
                self._data["component"] = extracted.components[0]

        if extracted.severity and not self._data["severity"]:
            self._data["severity"] = extracted.severity

        for ver in extracted.ceph_versions:
            if ver not in self._data["ceph_versions"]:
                self._data["ceph_versions"].append(ver)

        for label in extracted.jira_labels:
            if label not in self._data["jira_labels"]:
                self._data["jira_labels"].append(label)

        for err in extracted.error_messages:
            if err not in self._data["error_messages"]:
                self._data["error_messages"].append(err)

        for trace in extracted.stack_traces:
            if trace not in self._data["stack_traces"]:
                self._data["stack_traces"].append(trace)

        for warning in extracted.health_warnings:
            if warning not in self._data["health_warnings"]:
                self._data["health_warnings"].append(warning)

        received = email.received_date.isoformat() if email.received_date else _now_iso()

        self._data["timeline"].append({
            "type": "email",
            "date": received,
            "sender": email.sender.address,
            "sender_name": email.sender.name,
            "summary": email.body_preview[:300] if email.body_preview else email.subject,
            "message_id": email.message_id,
        })

        self._data["last_updated"] = received
        if received < self._data["first_seen"]:
            self._data["first_seen"] = received

        self._data["timeline"].sort(key=lambda e: e.get("date", ""))

    def to_dict(self) -> dict:
        return dict(self._data)

    @classmethod
    def from_dict(cls, data: dict) -> "ConsolidatedIssue":
        return cls(data)


class IssueStateDB:
    """Persistent state database for consolidated issues."""

    def __init__(self, state_dir: Path = STATE_DIR) -> None:
        self._state_dir = state_dir
        self._issues: dict[str, ConsolidatedIssue] = {}
        self._sync_metadata: dict[str, Any] = {}
        self._load()

    def _load(self) -> None:
        self._state_dir.mkdir(parents=True, exist_ok=True)

        issues_file = self._state_dir / "issues.json"
        if issues_file.exists():
            try:
                data = json.loads(issues_file.read_text())
                for item in data.get("issues", []):
                    issue = ConsolidatedIssue.from_dict(item)
                    self._issues[issue.issue_id] = issue
            except (json.JSONDecodeError, KeyError) as exc:
                logger.warning("Failed to load issues state: %s", exc)

        meta_file = self._state_dir / "sync_metadata.json"
        if meta_file.exists():
            try:
                self._sync_metadata = json.loads(meta_file.read_text())
            except json.JSONDecodeError:
                self._sync_metadata = {}

    def save(self) -> None:
        self._state_dir.mkdir(parents=True, exist_ok=True)

        issues_data = {
            "version": "1.0",
            "updated_at": _now_iso(),
            "issue_count": len(self._issues),
            "issues": [issue.to_dict() for issue in self._issues.values()],
        }
        (self._state_dir / "issues.json").write_text(
            json.dumps(issues_data, indent=2, default=str)
        )

        (self._state_dir / "sync_metadata.json").write_text(
            json.dumps(self._sync_metadata, indent=2, default=str)
        )

    @property
    def last_sync(self) -> datetime | None:
        ts = self._sync_metadata.get("last_sync")
        if ts:
            return datetime.fromisoformat(ts)
        return None

    def update_sync_timestamp(self) -> None:
        self._sync_metadata["last_sync"] = _now_iso()
        self._sync_metadata["sync_count"] = self._sync_metadata.get("sync_count", 0) + 1

    def find_issue_for_email(
        self, email: EmailSummary, extracted: ExtractedIssueInfo
    ) -> ConsolidatedIssue | None:
        """Find an existing consolidated issue that this email belongs to.

        Matching strategy (layered, most specific wins):
        1. Case ID match
        2. Conversation ID match
        3. Subject similarity
        """
        # 1. Case ID match
        for issue in self._issues.values():
            for cid in extracted.case_ids:
                if cid in issue.case_ids:
                    return issue
            for jid in extracted.jira_ids:
                if jid in issue.jira_ids:
                    return issue

        # 2. Conversation ID match
        if email.conversation_id:
            for issue in self._issues.values():
                if email.conversation_id in issue.conversation_ids:
                    return issue

        # 3. Subject similarity
        clean_subj = _clean_subject(email.subject)
        if clean_subj:
            for issue in self._issues.values():
                if issue.subject and _clean_subject(issue.subject) == clean_subj:
                    return issue

        return None

    def add_email(self, email: EmailSummary, body_text: str = "") -> tuple[ConsolidatedIssue, bool]:
        """Add an email to the state DB.

        Returns:
            Tuple of (issue, is_new) where is_new indicates a new issue was created.
        """
        extracted = extract_issue_info(email.subject, body_text or email.body_preview)

        existing = self.find_issue_for_email(email, extracted)
        if existing:
            existing.add_email(email, extracted)
            return existing, False

        issue = ConsolidatedIssue()
        issue.add_email(email, extracted)
        self._issues[issue.issue_id] = issue
        return issue, True

    def get_issue(self, issue_id: str) -> ConsolidatedIssue | None:
        return self._issues.get(issue_id)

    def get_issue_by_case_id(self, case_id: str) -> ConsolidatedIssue | None:
        for issue in self._issues.values():
            if case_id in issue.case_ids or case_id in issue.jira_ids:
                return issue
        return None

    def get_all_issues(self) -> list[ConsolidatedIssue]:
        return list(self._issues.values())

    def get_stats(self) -> dict[str, Any]:
        issues = list(self._issues.values())
        by_component: dict[str, int] = {}
        by_prio_list: dict[str, int] = {}
        by_severity: dict[str, int] = {}

        for issue in issues:
            data = issue.to_dict()
            comp = data.get("component", "unknown") or "unknown"
            by_component[comp] = by_component.get(comp, 0) + 1

            for pl in data.get("prio_lists", []):
                by_prio_list[pl] = by_prio_list.get(pl, 0) + 1

            sev = data.get("severity", "unknown") or "unknown"
            by_severity[sev] = by_severity.get(sev, 0) + 1

        return {
            "total_issues": len(issues),
            "by_component": by_component,
            "by_prio_list": by_prio_list,
            "by_severity": by_severity,
            "last_sync": self._sync_metadata.get("last_sync"),
            "sync_count": self._sync_metadata.get("sync_count", 0),
        }

    def export_for_publishing(self) -> dict:
        """Export sanitized issues data for GitHub Pages publishing."""
        from ceph_prio_hub.sanitizer.redactor import sanitize_dict

        issues = [
            sanitize_dict(issue.to_dict())
            for issue in self._issues.values()
        ]

        return {
            "version": "1.0",
            "updated_at": _now_iso(),
            "issue_count": len(issues),
            "issues": issues,
        }
