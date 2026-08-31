#!/usr/bin/env python3
"""CLI delta sync for ceph-prio-hub.

Same ``--since YYYY-MM-DD`` contract as ceph-issue-kb ``index_issues.py``
(JIRA ``updated >=`` that date).

Usage:
    python scripts/sync.py
    python scripts/sync.py --since 2026-08-01
    python scripts/sync.py --since 2026-08-01 --emails
    python scripts/sync.py --jira-only --limit 500
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dotenv import load_dotenv

_root = Path(__file__).resolve().parents[1]
load_dotenv(_root / ".env")
_issue_kb_env = Path.home() / "Projects" / "ceph-issue-kb" / ".env"
if _issue_kb_env.exists():
    load_dotenv(_issue_kb_env, override=False)

from ceph_prio_hub.config import ServerConfig
from ceph_prio_hub.jira.client import JiraAuthError, JiraClient, parse_jira_issue
from ceph_prio_hub.tracker.state import IssueStateDB

logger = logging.getLogger("ceph_prio_hub.sync")


def _parse_since(value: str) -> str:
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"Invalid date {value!r}; expected YYYY-MM-DD"
        ) from exc
    return value


def _sync_jira(db: IssueStateDB, since: str | None, limit: int, labels: list[str]) -> dict:
    jira = JiraClient()
    sync_since = since
    if not sync_since and db.last_sync:
        sync_since = db.last_sync.strftime("%Y-%m-%d")

    raw_issues = jira.fetch_prio_issues(
        labels=labels,
        since=sync_since,
        limit=limit,
    )

    new_count = 0
    updated_count = 0
    for raw in raw_issues:
        parsed = parse_jira_issue(raw)
        _, is_new = db.add_jira_issue(parsed)
        if is_new:
            new_count += 1
        else:
            updated_count += 1

    db.update_sync_timestamp()
    db.save()
    return {
        "source": "jira",
        "since": sync_since,
        "fetched": len(raw_issues),
        "new": new_count,
        "updated": updated_count,
        "total": len(db.get_all_issues()),
    }


def _sync_emails(db: IssueStateDB, since: str | None, limit: int) -> dict:
    from ceph_prio_hub.graph.client import GraphAuthError, GraphClient

    config = ServerConfig.load()
    if not config.azure.is_configured:
        return {"source": "email", "error": "Azure AD not configured (~/.ceph-prio-hub/config.json)"}

    client = GraphClient(config.azure)
    since_dt = None
    if since:
        since_dt = datetime.strptime(since, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    elif db.last_sync:
        since_dt = db.last_sync

    try:
        emails = client.fetch_prio_emails(
            prio_list="all",
            days_back=7,
            limit=limit,
            since=since_dt,
        )
    except GraphAuthError as exc:
        return {"source": "email", "error": str(exc)}

    new_count = 0
    updated_count = 0
    for email in emails:
        _, is_new = db.add_email(email)
        if is_new:
            new_count += 1
        else:
            updated_count += 1

    db.update_sync_timestamp()
    db.save()
    return {
        "source": "email",
        "since": since or (since_dt.strftime("%Y-%m-%d") if since_dt else None),
        "fetched": len(emails),
        "new": new_count,
        "updated": updated_count,
        "total": len(db.get_all_issues()),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Delta-sync prio-hub from JIRA (and optionally email) since a date.",
    )
    parser.add_argument(
        "--since",
        metavar="YYYY-MM-DD",
        type=_parse_since,
        default=None,
        help="Only fetch records updated since this ISO date (default: last sync timestamp)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=200,
        help="Max records per source (default: 200)",
    )
    parser.add_argument(
        "--labels",
        default="Ceph_L3,IBM_Customer_Issue",
        help="Comma-separated JIRA labels (default: Ceph_L3,IBM_Customer_Issue)",
    )
    parser.add_argument(
        "--emails",
        action="store_true",
        help="Also sync prio-list emails via Microsoft Graph",
    )
    parser.add_argument(
        "--jira-only",
        action="store_true",
        help="Sync JIRA only (default behaviour; kept for explicitness)",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(message)s",
        datefmt="%H:%M:%S",
    )

    config = ServerConfig.load()
    config.ensure_dirs()
    db = IssueStateDB(config.state_dir)
    labels = [p.strip() for p in args.labels.split(",") if p.strip()]

    print(f"=== Ceph Prio-Hub delta sync ===")
    print(f"since: {args.since or '(last sync / connector default)'}")
    print()

    try:
        jira_stats = _sync_jira(db, args.since, args.limit, labels)
    except JiraAuthError as exc:
        logger.error("JIRA sync failed: %s", exc)
        return 1

    print(
        f"JIRA: fetched {jira_stats['fetched']}, "
        f"new {jira_stats['new']}, updated {jira_stats['updated']}, "
        f"total {jira_stats['total']}"
    )

    if args.emails:
        email_stats = _sync_emails(db, args.since, args.limit)
        if email_stats.get("error"):
            logger.error("Email sync failed: %s", email_stats["error"])
            return 1
        print(
            f"Email: fetched {email_stats['fetched']}, "
            f"new {email_stats['new']}, updated {email_stats['updated']}, "
            f"total {email_stats['total']}"
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
