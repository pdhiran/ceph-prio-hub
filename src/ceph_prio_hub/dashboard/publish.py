"""Sync JIRA, generate dashboard, and publish to GitHub Pages.

Usage:
    python -m ceph_prio_hub.dashboard.publish          # sync + generate + push
    python -m ceph_prio_hub.dashboard.publish --no-push # sync + generate only
    python -m ceph_prio_hub.dashboard.publish --no-sync # generate + push (skip JIRA fetch)
"""

from __future__ import annotations

import argparse
import logging
import shutil
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv

_issue_kb_env = Path.home() / "Projects" / "ceph-issue-kb" / ".env"
if _issue_kb_env.exists():
    load_dotenv(_issue_kb_env, override=False)

from ceph_prio_hub.config import ServerConfig
from ceph_prio_hub.dashboard.generator import generate_dashboard
from ceph_prio_hub.jira.client import JiraClient, parse_jira_issue
from ceph_prio_hub.tracker.state import IssueStateDB

logger = logging.getLogger(__name__)
REPO_ROOT = Path(__file__).resolve().parents[3]
DOCS_DIR = REPO_ROOT / "docs"


def sync_jira(db: IssueStateDB, since: str | None = None, limit: int = 500) -> dict:
    """Fetch and merge JIRA prio-list issues."""
    client = JiraClient()
    sync_since = since or (db.last_sync.strftime("%Y-%m-%d") if db.last_sync else None)

    raw_issues = client.fetch_prio_issues(since=sync_since, limit=limit)
    new_count = 0
    update_count = 0

    for raw in raw_issues:
        parsed = parse_jira_issue(raw)
        _, is_new = db.add_jira_issue(parsed)
        if is_new:
            new_count += 1
        else:
            update_count += 1

    db.update_sync_timestamp()
    db.save()
    client.close()

    return {
        "fetched": len(raw_issues),
        "new": new_count,
        "updated": update_count,
        "total": len(db.get_all_issues()),
    }


def publish_to_docs(site_dir: Path, docs_dir: Path) -> None:
    """Copy generated site to the repo's docs/ directory."""
    if docs_dir.exists():
        shutil.rmtree(docs_dir)
    shutil.copytree(site_dir, docs_dir)
    logger.info("Copied site to %s", docs_dir)


def git_commit_and_push(docs_dir: Path, message: str) -> bool:
    """Stage docs/, commit, and push."""
    repo = docs_dir.parent
    try:
        subprocess.run(["git", "add", "docs/"], cwd=repo, check=True, capture_output=True)
        result = subprocess.run(
            ["git", "status", "--porcelain", "docs/"],
            cwd=repo, capture_output=True, text=True,
        )
        if not result.stdout.strip():
            logger.info("No changes in docs/ — skipping commit")
            return False

        subprocess.run(
            ["git", "commit", "-m", message],
            cwd=repo, check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "push", "origin", "main"],
            cwd=repo, check=True, capture_output=True,
        )
        logger.info("Pushed to origin/main")
        return True
    except subprocess.CalledProcessError as exc:
        logger.error("Git operation failed: %s", exc.stderr.decode() if exc.stderr else exc)
        return False


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync, generate, and publish dashboard")
    parser.add_argument("--no-sync", action="store_true", help="Skip JIRA sync")
    parser.add_argument("--no-push", action="store_true", help="Skip git commit/push")
    parser.add_argument("--since", default="", help="Sync issues since date (YYYY-MM-DD)")
    parser.add_argument("--limit", type=int, default=500, help="Max issues to fetch")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    config = ServerConfig.load()
    config.ensure_dirs()
    db = IssueStateDB(config.state_dir)
    site_dir = config.state_dir.parent / "site"

    if not args.no_sync:
        logger.info("Syncing JIRA issues...")
        result = sync_jira(db, since=args.since or None, limit=args.limit)
        logger.info(
            "Sync complete: %d fetched, %d new, %d updated, %d total",
            result["fetched"], result["new"], result["updated"], result["total"],
        )
    else:
        logger.info("Skipping JIRA sync (--no-sync)")

    logger.info("Generating dashboard...")
    index = generate_dashboard(db, site_dir)
    logger.info("Dashboard generated: %s", index)

    logger.info("Copying to docs/...")
    publish_to_docs(site_dir, DOCS_DIR)

    if not args.no_push:
        from datetime import datetime
        ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M")
        total = len(db.get_all_issues())
        msg = f"Update dashboard: {total} issues ({ts} UTC)"
        logger.info("Committing and pushing...")
        if git_commit_and_push(DOCS_DIR, msg):
            logger.info("Published to GitHub Pages")
        else:
            logger.info("No changes to publish (or push failed)")
    else:
        logger.info("Skipping git push (--no-push)")


if __name__ == "__main__":
    main()
