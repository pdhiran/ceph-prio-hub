"""Email-to-JIRA correlation logic.

Links prio-list email issues to JIRA trackers by extracting case/tracker IDs
from email content and merging JIRA timeline data into the consolidated issue.

Note: This module provides the correlation data structures and merge logic.
The actual JIRA lookups happen at the agent/automation layer via the
ceph-issue-kb MCP tools (get_issue, find_similar_issue).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ceph_prio_hub.tracker.state import ConsolidatedIssue


def merge_jira_data(issue: ConsolidatedIssue, jira_data: dict[str, Any]) -> None:
    """Merge JIRA issue data into a consolidated issue.

    Called by the automation layer after fetching JIRA details via
    ceph-issue-kb.get_issue. Updates labels, timeline, and status.

    Args:
        issue: The consolidated issue to update.
        jira_data: Response from ceph-issue-kb.get_issue containing
                   fields like status, assignee, labels, comments, etc.
    """
    data = issue._data

    jira_labels = jira_data.get("labels", [])
    for label in jira_labels:
        if label not in data["jira_labels"]:
            data["jira_labels"].append(label)

    jira_id = jira_data.get("source_id", "") or jira_data.get("entity_id", "")
    if jira_id and jira_id not in data["jira_ids"]:
        data["jira_ids"].append(jira_id)

    jira_component = jira_data.get("component", "")
    if jira_component and not data["component"]:
        data["component"] = jira_component

    jira_version = jira_data.get("version", "") or jira_data.get("fix_version", "")
    if jira_version and jira_version not in data["ceph_versions"]:
        data["ceph_versions"].append(jira_version)

    jira_status = jira_data.get("status", "")
    if jira_status:
        status_entry = {
            "type": "jira_status",
            "date": jira_data.get("updated", "") or jira_data.get("created", ""),
            "status": jira_status,
            "source_id": jira_id,
        }
        existing_statuses = [
            e for e in data["timeline"]
            if e.get("type") == "jira_status" and e.get("source_id") == jira_id
        ]
        if not existing_statuses or existing_statuses[-1].get("status") != jira_status:
            data["timeline"].append(status_entry)

    for comment in jira_data.get("comments", []):
        comment_entry = {
            "type": "jira_comment",
            "date": comment.get("created", ""),
            "author": comment.get("author", ""),
            "summary": (comment.get("body", "")[:300] if comment.get("body") else ""),
            "source_id": jira_id,
        }
        existing_comments = [
            e for e in data["timeline"]
            if e.get("type") == "jira_comment"
            and e.get("source_id") == jira_id
            and e.get("date") == comment_entry["date"]
        ]
        if not existing_comments:
            data["timeline"].append(comment_entry)

    data["timeline"].sort(key=lambda e: e.get("date", ""))

    updated = jira_data.get("updated", "")
    if updated and updated > data.get("last_updated", ""):
        data["last_updated"] = updated


def build_correlation_summary(issue: ConsolidatedIssue) -> dict[str, Any]:
    """Build a summary of the issue's correlation status.

    Useful for the dashboard to show which issues need JIRA lookup.
    """
    data = issue.to_dict()
    has_case_ids = bool(data.get("case_ids"))
    has_jira_ids = bool(data.get("jira_ids"))
    has_jira_timeline = any(
        e.get("type", "").startswith("jira_")
        for e in data.get("timeline", [])
    )

    return {
        "issue_id": data["issue_id"],
        "has_case_ids": has_case_ids,
        "has_jira_ids": has_jira_ids,
        "has_jira_timeline": has_jira_timeline,
        "case_ids": data.get("case_ids", []),
        "jira_ids": data.get("jira_ids", []),
        "needs_jira_lookup": has_case_ids and not has_jira_timeline,
    }
