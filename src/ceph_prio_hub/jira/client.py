"""JIRA REST API client for fetching prio-list issues.

Connects to ibm-ceph.atlassian.net (IBMCEPH project) and queries for
issues with customer escalation labels (Ceph_L3, IBM_Customer_Issue).

Uses the same JIRA_USERNAME / JIRA_API_TOKEN env vars as ceph-issue-kb.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime
from typing import Any, Iterator

import requests

logger = logging.getLogger(__name__)

JIRA_BASE_URL = "https://ibm-ceph.atlassian.net"
JIRA_PROJECT = "IBMCEPH"
PAGE_SIZE = 50

PRIO_LABELS = ["Ceph_L3", "IBM_Customer_Issue"]

ISSUE_FIELDS = ",".join([
    "summary", "status", "assignee", "reporter", "priority", "labels",
    "components", "fixVersions", "versions", "created", "updated",
    "resolution", "resolutiondate", "description", "comment",
    "issuetype", "customfield_10002",
])


class JiraAuthError(Exception):
    """Raised when JIRA credentials are missing or invalid."""


class JiraClient:
    """Client for JIRA REST API v3 with cursor-based pagination."""

    def __init__(
        self,
        base_url: str = JIRA_BASE_URL,
        username: str | None = None,
        api_token: str | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._username = username or os.environ.get("JIRA_USERNAME", "")
        self._api_token = api_token or os.environ.get("JIRA_API_TOKEN", "")

        if not self._username or not self._api_token:
            raise JiraAuthError(
                "JIRA credentials not found. Set JIRA_USERNAME and JIRA_API_TOKEN "
                "environment variables (same as ceph-issue-kb)."
            )

        self._session = requests.Session()
        self._session.auth = (self._username, self._api_token)
        self._session.headers.update({
            "Content-Type": "application/json",
            "Accept": "application/json",
        })
        self._min_interval = 0.2
        self._last_request = 0.0

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last_request = time.monotonic()

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict:
        url = f"{self._base_url}{path}"
        self._throttle()
        try:
            resp = self._session.get(url, params=params, timeout=30)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            raise JiraAuthError(f"JIRA request failed: {path} -- {exc}") from exc

    @staticmethod
    def _escape_jql(value: str) -> str:
        return value.replace("\\", "\\\\").replace('"', '\\"')

    def _paginate_jql(self, jql: str, limit: int | None = None) -> Iterator[dict]:
        """Paginate through JIRA search results using cursor-based pagination."""
        yielded = 0
        max_results = min(limit, PAGE_SIZE) if limit is not None else PAGE_SIZE
        next_page_token: str | None = None

        while True:
            params: dict[str, Any] = {
                "jql": jql,
                "maxResults": max_results,
                "fields": ISSUE_FIELDS,
            }
            if next_page_token is not None:
                params["nextPageToken"] = next_page_token

            data = self._get("/rest/api/3/search/jql", params=params)
            issues = data.get("issues", [])
            if not issues:
                break

            for issue in issues:
                if limit is not None and yielded >= limit:
                    return
                yield issue
                yielded += 1

            if data.get("isLast", True):
                break
            next_page_token = data.get("nextPageToken")
            if not next_page_token:
                break

        logger.info("JIRA pagination complete: yielded %d issues", yielded)

    def fetch_prio_issues(
        self,
        labels: list[str] | None = None,
        since: str | None = None,
        status: str | None = None,
        component: str | None = None,
        limit: int = 200,
    ) -> list[dict]:
        """Fetch issues from IBMCEPH project with prio-list labels.

        Args:
            labels: Labels to filter by (default: Ceph_L3, IBM_Customer_Issue).
            since: Only issues updated since this date (YYYY-MM-DD).
            status: Filter by status (e.g. "Open", "In Progress").
            component: Filter by component name.
            limit: Maximum number of issues to return.
        """
        target_labels = labels or PRIO_LABELS
        label_clause = " OR ".join(
            f'labels = "{self._escape_jql(lbl)}"' for lbl in target_labels
        )
        jql = f'project = "{JIRA_PROJECT}" AND ({label_clause})'

        if since:
            jql += f' AND updated >= "{since}"'
        if status:
            jql += f' AND status = "{self._escape_jql(status)}"'
        if component:
            jql += f' AND component = "{self._escape_jql(component)}"'

        jql += " ORDER BY updated DESC"

        return list(self._paginate_jql(jql, limit=limit))

    def fetch_issue(self, issue_key: str) -> dict:
        """Fetch a single issue by key (e.g. IBMCEPH-16204)."""
        return self._get(
            f"/rest/api/3/issue/{issue_key}",
            params={"fields": ISSUE_FIELDS},
        )

    def fetch_issue_comments(self, issue_key: str) -> list[dict]:
        """Fetch all comments for an issue."""
        data = self._get(f"/rest/api/3/issue/{issue_key}/comment")
        return data.get("comments", [])

    def health(self) -> dict[str, Any]:
        """Check JIRA connectivity and return status."""
        try:
            data = self._get("/rest/api/3/myself")
            return {
                "ok": True,
                "user": data.get("displayName", ""),
                "email": data.get("emailAddress", ""),
                "base_url": self._base_url,
                "project": JIRA_PROJECT,
            }
        except JiraAuthError as exc:
            return {"ok": False, "error": str(exc)}

    def close(self) -> None:
        self._session.close()


def parse_jira_issue(raw: dict) -> dict[str, Any]:
    """Parse a raw JIRA issue into a normalized dict for the state DB."""
    fields = raw.get("fields", {})
    key = raw.get("key", "")

    components = [c.get("name", "") for c in fields.get("components", [])]
    labels = [lbl for lbl in fields.get("labels", [])]
    fix_versions = [v.get("name", "") for v in fields.get("fixVersions", [])]
    affected_versions = [v.get("name", "") for v in fields.get("versions", [])]

    status_obj = fields.get("status", {})
    priority_obj = fields.get("priority", {})
    assignee_obj = fields.get("assignee", {}) or {}
    reporter_obj = fields.get("reporter", {}) or {}
    resolution_obj = fields.get("resolution", {}) or {}

    description = ""
    desc_field = fields.get("description")
    if isinstance(desc_field, str):
        description = desc_field
    elif isinstance(desc_field, dict):
        description = _extract_adf_text(desc_field)

    comments = []
    comment_field = fields.get("comment", {})
    for c in comment_field.get("comments", []) if isinstance(comment_field, dict) else []:
        body = c.get("body", "")
        if isinstance(body, dict):
            body = _extract_adf_text(body)
        comments.append({
            "author": (c.get("author", {}) or {}).get("displayName", ""),
            "created": c.get("created", ""),
            "body": body[:500] if body else "",
        })

    return {
        "key": key,
        "url": f"{JIRA_BASE_URL}/browse/{key}",
        "summary": fields.get("summary", ""),
        "status": status_obj.get("name", ""),
        "status_category": status_obj.get("statusCategory", {}).get("name", ""),
        "priority": priority_obj.get("name", ""),
        "assignee": assignee_obj.get("displayName", ""),
        "assignee_email": assignee_obj.get("emailAddress", ""),
        "reporter": reporter_obj.get("displayName", ""),
        "components": components,
        "labels": labels,
        "fix_versions": fix_versions,
        "affected_versions": affected_versions,
        "resolution": resolution_obj.get("name", "") if resolution_obj else "",
        "created": fields.get("created", ""),
        "updated": fields.get("updated", ""),
        "resolved": fields.get("resolutiondate", ""),
        "description": description[:2000],
        "comments": comments,
        "issue_type": (fields.get("issuetype", {}) or {}).get("name", ""),
    }


def _extract_adf_text(adf: dict) -> str:
    """Extract plain text from Atlassian Document Format (ADF) JSON."""
    parts: list[str] = []

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("type") == "text":
                parts.append(node.get("text", ""))
            for child in node.get("content", []):
                _walk(child)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(adf)
    return " ".join(parts)
