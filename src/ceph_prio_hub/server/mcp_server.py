"""MCP server for the Ceph Prio-List Issue Hub.

Uses FastMCP from the ``mcp`` SDK. Exposes tools for fetching prio-list
emails, extracting issue info, managing consolidated issues, and generating
statistics.

Run with::

    python -m ceph_prio_hub.server.mcp_server                     # stdio (Cursor)
    python -m ceph_prio_hub.server.mcp_server --transport sse      # SSE
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

_env_file = Path(__file__).resolve().parents[3] / ".env"
if _env_file.exists():
    load_dotenv(_env_file)
_issue_kb_env = Path.home() / "Projects" / "ceph-issue-kb" / ".env"
if _issue_kb_env.exists():
    load_dotenv(_issue_kb_env, override=False)

from ceph_prio_hub.config import ServerConfig
from ceph_prio_hub.dashboard.generator import generate_dashboard
from ceph_prio_hub.graph.client import GraphClient, GraphAuthError
from ceph_prio_hub.jira.client import JiraClient, JiraAuthError, parse_jira_issue
from ceph_prio_hub.parser.issue_extractor import extract_issue_info
from ceph_prio_hub.tracker.state import IssueStateDB

logger = logging.getLogger(__name__)


def create_mcp_server(
    config: ServerConfig,
    graph: GraphClient | None = None,
    state_db: IssueStateDB | None = None,
) -> FastMCP:
    """Build and return a FastMCP server.

    Separated from ``main()`` so tests can create a server with mocks.
    """
    from mcp.types import Icon

    ceph_icon = Icon(
        src="data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCA0OCA0OCIgd2lkdGg9IjQ4IiBoZWlnaHQ9IjQ4Ij48Y2lyY2xlIGN4PSIyNCIgY3k9IjI0IiByPSIyMiIgZmlsbD0iI0VGNTAzQSIvPjx0ZXh0IHg9IjI0IiB5PSIzMiIgZm9udC1mYW1pbHk9IkFyaWFsLHNhbnMtc2VyaWYiIGZvbnQtc2l6ZT0iMjAiIGZvbnQtd2VpZ2h0PSJib2xkIiBmaWxsPSJ3aGl0ZSIgdGV4dC1hbmNob3I9Im1pZGRsZSI+UDwvdGV4dD48L3N2Zz4=",
        mimeType="image/svg+xml",
    )

    mcp = FastMCP(
        "Ceph Prio-List Issue Hub",
        instructions=(
            "Monitor and analyze prio-list emails (ocs-prio-list, ceph-prio-list, "
            "odf-prio-list) for customer issue tracking and test coverage analysis. "
            "Consolidates email threads, correlates with JIRA, and tracks issue "
            "lifecycle. Key tools: fetch_prio_emails, sync_issues, get_issue_timeline, "
            "extract_issue_info."
        ),
        icons=[ceph_icon],
    )

    db = state_db or IssueStateDB(config.state_dir)
    _jira_client: JiraClient | None = None

    def _get_graph() -> GraphClient:
        if graph:
            return graph
        if not config.azure.is_configured:
            raise GraphAuthError(
                "Azure AD not configured. Create ~/.ceph-prio-hub/config.json with "
                "client_id and tenant_id. See README for Azure AD setup instructions."
            )
        return GraphClient(config.azure)

    def _get_jira() -> JiraClient:
        nonlocal _jira_client
        if _jira_client is None:
            _jira_client = JiraClient()
        return _jira_client

    @mcp.tool()
    def fetch_prio_emails(
        prio_list: str = "all",
        days_back: int = 7,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Fetch recent emails from prio-lists.

        Args:
            prio_list: Which list to fetch from: "ceph", "ocs", "odf", or "all".
            days_back: How many days back to look (default 7).
            limit: Maximum number of emails to return (default 50).

        Returns:
            List of email summaries with subject, sender, date, case IDs, and body preview.
        """
        try:
            client = _get_graph()
            emails = client.fetch_prio_emails(
                prio_list=prio_list, days_back=days_back, limit=limit,
            )
            results = []
            for email in emails:
                extracted = extract_issue_info(email.subject, email.body_preview)
                results.append({
                    "message_id": email.message_id,
                    "conversation_id": email.conversation_id,
                    "subject": email.subject,
                    "sender": email.sender.address,
                    "sender_name": email.sender.name,
                    "received_date": email.received_date.isoformat() if email.received_date else None,
                    "body_preview": email.body_preview[:200],
                    "prio_lists": email.prio_lists,
                    "case_ids": extracted.case_ids,
                    "jira_ids": extracted.jira_ids,
                    "components": extracted.components,
                    "has_attachments": email.has_attachments,
                })
            return {"count": len(results), "emails": results}
        except GraphAuthError as exc:
            return {"error": str(exc)}

    @mcp.tool()
    def get_email_details(message_id: str) -> dict[str, Any]:
        """Get full email body and conversation thread for a specific message.

        Args:
            message_id: The Graph API message ID (from fetch_prio_emails results).

        Returns:
            Full email body (text), attachments list, and conversation thread.
        """
        try:
            client = _get_graph()
            detail = client.get_email_detail(message_id)
            thread = client.get_conversation_thread(detail.conversation_id)

            return {
                "message_id": detail.message_id,
                "conversation_id": detail.conversation_id,
                "subject": detail.subject,
                "sender": detail.sender.address,
                "received_date": detail.received_date.isoformat() if detail.received_date else None,
                "body_text": detail.body_text,
                "attachment_names": detail.attachment_names,
                "thread": [
                    {
                        "message_id": msg.message_id,
                        "subject": msg.subject,
                        "sender": msg.sender.address,
                        "received_date": msg.received_date.isoformat() if msg.received_date else None,
                        "body_preview": msg.body_preview[:200],
                    }
                    for msg in thread
                ],
                "thread_count": len(thread),
            }
        except GraphAuthError as exc:
            return {"error": str(exc)}

    @mcp.tool()
    def search_prio_emails(
        query: str,
        prio_list: str = "all",
        days_back: int = 30,
        limit: int = 20,
    ) -> dict[str, Any]:
        """Search prio-list emails by keyword.

        Args:
            query: Search query (searches subject, body, sender).
            prio_list: Filter to a specific list or "all".
            days_back: How far back to search (default 30 days).
            limit: Maximum results (default 20).
        """
        try:
            client = _get_graph()
            emails = client.search_emails(
                query=query, prio_list=prio_list, days_back=days_back, limit=limit,
            )
            results = []
            for email in emails:
                extracted = extract_issue_info(email.subject, email.body_preview)
                results.append({
                    "message_id": email.message_id,
                    "subject": email.subject,
                    "sender": email.sender.address,
                    "received_date": email.received_date.isoformat() if email.received_date else None,
                    "body_preview": email.body_preview[:200],
                    "case_ids": extracted.case_ids,
                    "jira_ids": extracted.jira_ids,
                    "components": extracted.components,
                })
            return {"count": len(results), "emails": results}
        except GraphAuthError as exc:
            return {"error": str(exc)}

    @mcp.tool()
    def extract_issue_info_tool(
        subject: str,
        body: str,
    ) -> dict[str, Any]:
        """Extract structured issue data from email text.

        Parses case IDs, JIRA IDs, Ceph versions, components, error messages,
        stack traces, health warnings, and severity from email content.

        Args:
            subject: Email subject line.
            body: Email body text (plain text).
        """
        extracted = extract_issue_info(subject, body)
        return extracted.to_dict()

    @mcp.tool()
    def get_prio_stats() -> dict[str, Any]:
        """Get aggregate statistics on consolidated prio-list issues.

        Returns counts by component, prio-list, severity, and sync metadata.
        """
        return db.get_stats()

    @mcp.tool()
    def sync_issues(
        days_back: int | None = None,
        limit: int = 200,
    ) -> dict[str, Any]:
        """Incremental sync — fetch new emails and consolidate into issues.

        On first run or with days_back set, does a bulk import. On subsequent
        runs (no days_back), fetches only emails since the last sync.

        Args:
            days_back: Override lookback period (None = use last sync timestamp).
            limit: Max emails to fetch per sync (default 200).

        Returns:
            Summary of new and updated issues since last sync.
        """
        try:
            client = _get_graph()

            since = None
            if days_back is None and db.last_sync:
                since = db.last_sync

            emails = client.fetch_prio_emails(
                prio_list="all",
                days_back=days_back or 7,
                limit=limit,
                since=since,
            )

            new_issues = []
            updated_issues = []

            for email in emails:
                issue, is_new = db.add_email(email)
                entry = {
                    "issue_id": issue.issue_id,
                    "subject": issue.subject,
                    "case_ids": issue.case_ids,
                }
                if is_new:
                    new_issues.append(entry)
                else:
                    if entry not in updated_issues:
                        updated_issues.append(entry)

            db.update_sync_timestamp()
            db.save()

            return {
                "synced_emails": len(emails),
                "new_issues": new_issues,
                "new_issue_count": len(new_issues),
                "updated_issues": updated_issues,
                "updated_issue_count": len(updated_issues),
                "total_issues": len(db.get_all_issues()),
            }
        except GraphAuthError as exc:
            return {"error": str(exc)}

    @mcp.tool()
    def get_issue_timeline(
        issue_id: str = "",
        case_id: str = "",
    ) -> dict[str, Any]:
        """Get the full timeline for a consolidated issue.

        All email updates and JIRA status changes in chronological order.

        Args:
            issue_id: Internal issue ID (from sync_issues results).
            case_id: Case ID or JIRA ID (alternative to issue_id).
        """
        issue = None
        if issue_id:
            issue = db.get_issue(issue_id)
        elif case_id:
            issue = db.get_issue_by_case_id(case_id)

        if not issue:
            return {"error": f"Issue not found: {issue_id or case_id}"}

        data = issue.to_dict()
        return {
            "issue_id": data["issue_id"],
            "subject": data["subject"],
            "case_ids": data["case_ids"],
            "jira_ids": data["jira_ids"],
            "component": data["component"],
            "components": data["components"],
            "severity": data["severity"],
            "ceph_versions": data["ceph_versions"],
            "jira_labels": data["jira_labels"],
            "prio_lists": data["prio_lists"],
            "first_seen": data["first_seen"],
            "last_updated": data["last_updated"],
            "coverage_status": data["coverage_status"],
            "timeline": data["timeline"],
            "error_messages": data["error_messages"],
            "stack_traces": data["stack_traces"],
            "health_warnings": data["health_warnings"],
            "related_issues": data["related_issues"],
        }

    @mcp.tool()
    def fetch_jira_issues(
        labels: str = "Ceph_L3,IBM_Customer_Issue",
        since: str = "",
        status: str = "",
        component: str = "",
        limit: int = 100,
    ) -> dict[str, Any]:
        """Fetch JIRA issues from IBMCEPH project with prio-list labels.

        This is the primary data source. Pulls issues with Ceph_L3 and/or
        IBM_Customer_Issue labels from ibm-ceph.atlassian.net.

        Args:
            labels: Comma-separated labels to filter by (default: Ceph_L3,IBM_Customer_Issue).
            since: Only issues updated since this date (YYYY-MM-DD format, e.g. "2026-01-01").
            status: Filter by JIRA status (e.g. "Open", "In Progress", "Resolved").
            component: Filter by component name (e.g. "RGW", "CephFS").
            limit: Maximum number of issues (default 100).
        """
        try:
            jira = _get_jira()
            label_list = [l.strip() for l in labels.split(",") if l.strip()]
            raw_issues = jira.fetch_prio_issues(
                labels=label_list,
                since=since or None,
                status=status or None,
                component=component or None,
                limit=limit,
            )
            results = []
            for raw in raw_issues:
                parsed = parse_jira_issue(raw)
                results.append({
                    "key": parsed["key"],
                    "url": parsed["url"],
                    "summary": parsed["summary"],
                    "status": parsed["status"],
                    "priority": parsed["priority"],
                    "assignee": parsed["assignee"],
                    "components": parsed["components"],
                    "labels": parsed["labels"],
                    "created": parsed["created"],
                    "updated": parsed["updated"],
                    "resolution": parsed["resolution"],
                    "comment_count": len(parsed["comments"]),
                })
            return {"count": len(results), "issues": results}
        except JiraAuthError as exc:
            return {"error": str(exc)}

    @mcp.tool()
    def sync_jira_issues(
        labels: str = "Ceph_L3,IBM_Customer_Issue",
        since: str = "",
        limit: int = 200,
    ) -> dict[str, Any]:
        """Sync JIRA prio-list issues into the consolidated state DB.

        Fetches issues with specified labels, parses them, and merges into
        the local state database. Supports incremental sync (uses last sync
        timestamp if 'since' is not provided).

        Args:
            labels: Comma-separated labels (default: Ceph_L3,IBM_Customer_Issue).
            since: Only sync issues updated since this date (YYYY-MM-DD).
            limit: Maximum issues to fetch (default 200).
        """
        try:
            jira = _get_jira()
            label_list = [l.strip() for l in labels.split(",") if l.strip()]

            sync_since = since or None
            if not sync_since and db.last_sync:
                sync_since = db.last_sync.strftime("%Y-%m-%d")

            raw_issues = jira.fetch_prio_issues(
                labels=label_list,
                since=sync_since,
                limit=limit,
            )

            new_issues = []
            updated_issues = []

            for raw in raw_issues:
                parsed = parse_jira_issue(raw)
                issue, is_new = db.add_jira_issue(parsed)
                entry = {
                    "issue_id": issue.issue_id,
                    "jira_key": parsed["key"],
                    "summary": parsed["summary"],
                    "status": parsed["status"],
                    "labels": parsed["labels"],
                }
                if is_new:
                    new_issues.append(entry)
                else:
                    if entry not in updated_issues:
                        updated_issues.append(entry)

            db.update_sync_timestamp()
            db.save()

            return {
                "synced_jira_issues": len(raw_issues),
                "new_issues": new_issues,
                "new_issue_count": len(new_issues),
                "updated_issues": updated_issues,
                "updated_issue_count": len(updated_issues),
                "total_issues": len(db.get_all_issues()),
            }
        except JiraAuthError as exc:
            return {"error": str(exc)}

    @mcp.tool()
    def get_jira_issue(issue_key: str) -> dict[str, Any]:
        """Get full details for a JIRA issue including comments.

        Args:
            issue_key: JIRA issue key (e.g. IBMCEPH-16204).
        """
        try:
            jira = _get_jira()
            raw = jira.fetch_issue(issue_key)
            return parse_jira_issue(raw)
        except JiraAuthError as exc:
            return {"error": str(exc)}

    @mcp.tool()
    def generate_dashboard_tool(
        output_dir: str = "",
    ) -> dict[str, Any]:
        """Generate the HTML dashboard and per-issue reports from synced data.

        Produces index.html (main dashboard) and issues/<id>.html (per-issue
        detail pages) into the output directory.

        Args:
            output_dir: Directory to write generated files (default: ~/.ceph-prio-hub/site/).
        """
        out = Path(output_dir) if output_dir else config.state_dir.parent / "site"
        try:
            index_path = generate_dashboard(db, out)
            issues_dir = out / "issues"
            issue_count = len(list(issues_dir.glob("*.html"))) if issues_dir.exists() else 0
            return {
                "index_path": str(index_path),
                "output_dir": str(out),
                "issue_reports": issue_count,
                "total_issues": len(db.get_all_issues()),
            }
        except Exception as exc:
            return {"error": str(exc)}

    @mcp.tool()
    def capabilities() -> dict[str, Any]:
        """Server capabilities: entity types, operations, and sources."""
        return {
            "schema_version": "1.0",
            "server": "ceph-prio-hub",
            "description": "Prio-list issue tracking for Ceph — JIRA + email consolidation",
            "entity_types": ["jira_issue", "email", "consolidated_issue"],
            "data_sources": {
                "jira": {
                    "url": "https://ibm-ceph.atlassian.net",
                    "project": "IBMCEPH",
                    "labels": ["Ceph_L3", "IBM_Customer_Issue"],
                    "status": "primary",
                },
                "email": {
                    "lists": ["ocs-prio-list", "ceph-prio-list", "odf-prio-list"],
                    "status": "planned",
                },
            },
            "tools": [
                "fetch_jira_issues", "sync_jira_issues", "get_jira_issue",
                "generate_dashboard",
                "fetch_prio_emails", "get_email_details", "search_prio_emails",
                "extract_issue_info", "get_prio_stats", "sync_issues",
                "get_issue_timeline", "capabilities", "health",
            ],
        }

    @mcp.tool()
    def health() -> dict[str, Any]:
        """Health check — JIRA connectivity, Azure config, state DB status."""
        stats = db.get_stats()

        jira_ok = False
        jira_msg = "not checked"
        try:
            jira = _get_jira()
            jira_health = jira.health()
            jira_ok = jira_health.get("ok", False)
            jira_msg = f"connected as {jira_health.get('user', '?')}" if jira_ok else jira_health.get("error", "failed")
        except JiraAuthError as exc:
            jira_msg = str(exc)

        overall = "healthy" if jira_ok else "degraded"

        return {
            "status": overall,
            "jira": {"ok": jira_ok, "message": jira_msg},
            "azure_configured": config.azure.is_configured,
            "total_issues": stats["total_issues"],
            "last_sync": stats.get("last_sync"),
            "sync_count": stats.get("sync_count", 0),
            "state_dir": str(config.state_dir),
        }

    return mcp


def _silence_stderr_logging() -> None:
    """Suppress all logging to stderr for stdio transport."""
    logging.disable(logging.CRITICAL)
    for handler in logging.root.handlers[:]:
        logging.root.removeHandler(handler)
    logging.root.addHandler(logging.NullHandler())


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Ceph Prio-Hub MCP server")
    parser.add_argument(
        "--transport",
        default="stdio",
        choices=["stdio", "sse"],
        help="MCP transport (default: stdio)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8080,
        help="Port for SSE transport (default: 8080)",
    )
    args = parser.parse_args(argv)

    if args.transport == "stdio":
        _silence_stderr_logging()

    config = ServerConfig.load()
    config.ensure_dirs()

    mcp = create_mcp_server(config)

    if args.transport == "sse":
        mcp.settings.port = args.port

    mcp.run(transport=args.transport)


if __name__ == "__main__":
    main()
