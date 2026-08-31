"""Background auto-updater for prio-hub.

- ``git pull --ff-only`` of this repo: ``.py`` changes exit the MCP
  subprocess so Cursor respawns it; anything else hot-reloads DBs.
- ``.reload_trigger`` watcher: ``./update_index.sh`` (a separate process)
  writes ``~/.ceph-prio-hub/`` then touches the trigger; this thread
  re-reads state from disk. Cursor does not restart.
- Optional periodic JIRA delta if ``JIRA_USERNAME`` / ``JIRA_API_TOKEN``
  are set.
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ceph_prio_hub.tracker.state import IssueStateDB
    from ceph_prio_hub.tracker.tracking import TrackingDB

logger = logging.getLogger(__name__)

_periodic_stop: threading.Event | None = None

TRIGGER_NAME = ".reload_trigger"
TRIGGER_POLL_SECONDS = 5.0


def _find_repo_root(start: Path) -> Path | None:
    current = start.resolve()
    for parent in [current, *current.parents]:
        if (parent / ".git").exists():
            return parent
    return None


def _has_remote(repo_dir: Path) -> bool:
    try:
        result = subprocess.run(
            ["git", "remote"],
            cwd=repo_dir, capture_output=True, text=True, timeout=10,
        )
        return bool(result.stdout.strip())
    except Exception:
        return False


def _detect_default_branch(repo_dir: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
            capture_output=True, text=True, cwd=str(repo_dir), timeout=10,
        )
        if result.returncode == 0:
            return result.stdout.strip().replace("origin/", "")
    except Exception:
        pass
    return "main"


def _get_head_sha(repo_dir: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_dir, capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return None


def _changed_files(repo_dir: Path, old_sha: str, new_sha: str) -> list[str]:
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", old_sha, new_sha],
            cwd=repo_dir, capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            return [f for f in result.stdout.strip().splitlines() if f]
    except Exception:
        pass
    return []


def _git_pull(repo_dir: Path) -> tuple[bool, str]:
    branch = _detect_default_branch(repo_dir)
    try:
        result = subprocess.run(
            ["git", "pull", "--ff-only", "origin", branch],
            cwd=repo_dir, capture_output=True, text=True, timeout=120,
        )
        output = result.stdout.strip()
        if result.returncode != 0:
            stderr = result.stderr.strip()
            return False, f"git pull failed: {stderr or output}"
        if "Already up to date" in output:
            return False, "Already up to date"
        return True, output
    except subprocess.TimeoutExpired:
        return False, "git pull timed out"
    except Exception as exc:
        return False, f"git pull error: {exc}"


def _reload_dbs(state_db: IssueStateDB, tracking_db: TrackingDB | None) -> None:
    state_db.reload()
    if tracking_db is not None:
        tracking_db.reload()


def _maybe_sync_jira(state_db: IssueStateDB) -> None:
    if not os.environ.get("JIRA_USERNAME") or not os.environ.get("JIRA_API_TOKEN"):
        return
    try:
        from ceph_prio_hub.jira.client import JiraClient, parse_jira_issue

        jira = JiraClient()
        since = state_db.last_sync.strftime("%Y-%m-%d") if state_db.last_sync else None
        raw = jira.fetch_prio_issues(since=since, limit=200)
        for item in raw:
            state_db.add_jira_issue(parse_jira_issue(item))
        state_db.update_sync_timestamp()
        state_db.save()
        logger.info("Periodic JIRA delta: fetched %d issues since %s", len(raw), since)
    except Exception as exc:
        logger.warning("Periodic JIRA sync failed: %s", exc)


def _do_update(
    state_db: IssueStateDB,
    tracking_db: TrackingDB | None,
    repo_root: Path,
) -> None:
    try:
        old_sha = _get_head_sha(repo_root)
        changed, message = _git_pull(repo_root)
        if not changed:
            if "failed" in message.lower() or "error" in message.lower() or "timed out" in message.lower():
                logger.warning("Auto-update: %s", message)
            else:
                logger.info("Repository is up to date")
            _maybe_sync_jira(state_db)
            return

        new_sha = _get_head_sha(repo_root)
        files = _changed_files(repo_root, old_sha, new_sha) if old_sha and new_sha else []
        if any(f.endswith(".py") for f in files):
            logger.info("Code changes detected, restarting MCP process (Cursor respawns it)")
            os._exit(0)
            return

        logger.info("Prio-hub repo updated, hot-reloading state (no Cursor restart)")
        _reload_dbs(state_db, tracking_db)
        _maybe_sync_jira(state_db)
    except Exception as exc:
        logger.warning("Auto-update failed, continuing with existing data: %s", exc)


def _periodic_loop(
    state_db: IssueStateDB,
    tracking_db: TrackingDB | None,
    repo_root: Path,
    interval_seconds: float,
    stop_event: threading.Event,
) -> None:
    while not stop_event.wait(timeout=interval_seconds):
        _do_update(state_db, tracking_db, repo_root)


def _trigger_mtime(repo_root: Path) -> float:
    try:
        return (repo_root / TRIGGER_NAME).stat().st_mtime
    except OSError:
        return 0.0


def _trigger_loop(
    state_db: IssueStateDB,
    tracking_db: TrackingDB | None,
    repo_root: Path,
    stop_event: threading.Event,
) -> None:
    last = _trigger_mtime(repo_root)
    while not stop_event.wait(timeout=TRIGGER_POLL_SECONDS):
        now = _trigger_mtime(repo_root)
        if now > last + 0.01:
            last = now
            logger.info("Reload trigger detected, hot-reloading prio-hub state")
            try:
                _reload_dbs(state_db, tracking_db)
            except Exception as exc:
                logger.warning("Trigger reload failed: %s", exc)


def start_auto_update(
    state_db: IssueStateDB,
    tracking_db: TrackingDB | None,
    repo_root: Path | None,
    *,
    update_interval_hours: float = 1,
) -> None:
    """Watch ``.reload_trigger`` and optionally git-pull this repo."""
    global _periodic_stop  # noqa: PLW0603

    if _periodic_stop is not None and not _periodic_stop.is_set():
        logger.warning("Auto-update already running; skipping duplicate start")
        return

    if repo_root is None:
        repo_root = _find_repo_root(Path(__file__))
    if repo_root is None:
        logger.debug("Auto-update skipped: not a git repository")
        return

    stop_event = threading.Event()
    _periodic_stop = stop_event

    if _has_remote(repo_root):
        threading.Thread(
            target=_do_update,
            args=(state_db, tracking_db, repo_root),
            daemon=True,
            name="prio-hub-auto-update",
        ).start()
        if update_interval_hours > 0:
            threading.Thread(
                target=_periodic_loop,
                args=(state_db, tracking_db, repo_root, update_interval_hours * 3600, stop_event),
                daemon=True,
                name="prio-hub-periodic-update",
            ).start()
    else:
        logger.debug("No git remote — skip pull, still watching .reload_trigger")

    threading.Thread(
        target=_trigger_loop,
        args=(state_db, tracking_db, repo_root, stop_event),
        daemon=True,
        name="prio-hub-reload-trigger",
    ).start()


def stop_auto_update() -> None:
    global _periodic_stop  # noqa: PLW0603
    if _periodic_stop is not None:
        _periodic_stop.set()
        _periodic_stop = None
