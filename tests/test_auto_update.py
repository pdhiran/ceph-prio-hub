"""Mock workflows: git-pull, .py exit, .reload_trigger, IssueStateDB.reload, update_index.sh."""

from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ceph_prio_hub.server.auto_update import (
    _do_update,
    _reload_dbs,
    _trigger_loop,
    start_auto_update,
    stop_auto_update,
)
from ceph_prio_hub.tracker.state import ConsolidatedIssue, IssueStateDB
from ceph_prio_hub.tracker.tracking import TrackingDB

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _cleanup_auto_update():
    yield
    stop_auto_update()


class TestStateReload:
    def test_reload_picks_up_disk_write(self, tmp_path):
        db = IssueStateDB(tmp_path)
        issue = ConsolidatedIssue()
        issue._data["subject"] = "before-reload"
        db._issues[issue.issue_id] = issue
        db.save()

        db._issues = {}
        db.reload()
        loaded = db.get_all_issues()
        assert len(loaded) == 1
        assert loaded[0].issue_id == issue.issue_id
        assert loaded[0]._data["subject"] == "before-reload"

    def test_tracking_reload(self, tmp_path):
        path = tmp_path / "tracking.json"
        db = TrackingDB(path)
        db.set("IBMCEPH-1", {"qa_status": "needs_analysis"})
        db.save()
        db._data = {}
        db.reload()
        assert db.get("IBMCEPH-1")["qa_status"] == "needs_analysis"


class TestDoUpdate:
    def test_python_change_exits_process(self, tmp_path):
        state = MagicMock()
        tracking = MagicMock()
        with (
            patch("ceph_prio_hub.server.auto_update._git_pull", return_value=(True, "Updated")),
            patch("ceph_prio_hub.server.auto_update._get_head_sha", side_effect=["aaa", "bbb"]),
            patch(
                "ceph_prio_hub.server.auto_update._changed_files",
                return_value=["src/ceph_prio_hub/server/mcp_server.py"],
            ),
            patch("ceph_prio_hub.server.auto_update.os._exit") as mock_exit,
            patch("ceph_prio_hub.server.auto_update._maybe_sync_jira"),
        ):
            _do_update(state, tracking, tmp_path)
        mock_exit.assert_called_once_with(0)
        state.reload.assert_not_called()

    def test_non_python_pull_hot_reloads(self, tmp_path):
        state = MagicMock()
        tracking = MagicMock()
        with (
            patch("ceph_prio_hub.server.auto_update._git_pull", return_value=(True, "Updated")),
            patch("ceph_prio_hub.server.auto_update._get_head_sha", side_effect=["aaa", "bbb"]),
            patch(
                "ceph_prio_hub.server.auto_update._changed_files",
                return_value=["UPDATING.md"],
            ),
            patch("ceph_prio_hub.server.auto_update.os._exit") as mock_exit,
            patch("ceph_prio_hub.server.auto_update._maybe_sync_jira") as jira,
        ):
            _do_update(state, tracking, tmp_path)
        mock_exit.assert_not_called()
        state.reload.assert_called_once()
        tracking.reload.assert_called_once()
        jira.assert_called_once()

    def test_already_up_to_date_still_tries_jira(self, tmp_path):
        state = MagicMock()
        with (
            patch(
                "ceph_prio_hub.server.auto_update._git_pull",
                return_value=(False, "Already up to date"),
            ),
            patch("ceph_prio_hub.server.auto_update._maybe_sync_jira") as jira,
        ):
            _do_update(state, None, tmp_path)
        jira.assert_called_once()
        state.reload.assert_not_called()


class TestTriggerLoop:
    def test_touch_trigger_reloads_without_git(self, tmp_path):
        state = MagicMock()
        tracking = MagicMock()
        stop = threading.Event()
        with patch("ceph_prio_hub.server.auto_update.TRIGGER_POLL_SECONDS", 0.05):
            t = threading.Thread(
                target=_trigger_loop,
                args=(state, tracking, tmp_path, stop),
                daemon=True,
            )
            t.start()
            time.sleep(0.08)
            (tmp_path / ".reload_trigger").write_text("1")
            deadline = time.time() + 2.0
            while time.time() < deadline and not state.reload.called:
                time.sleep(0.05)
            stop.set()
            t.join(timeout=1)
        state.reload.assert_called()
        tracking.reload.assert_called()


class TestStartAutoUpdate:
    def test_no_remote_still_starts_trigger(self, tmp_path):
        (tmp_path / ".git").mkdir()
        with patch("ceph_prio_hub.server.auto_update._has_remote", return_value=False):
            start_auto_update(MagicMock(), MagicMock(), tmp_path, update_interval_hours=0)
        time.sleep(0.05)
        names = [t.name for t in threading.enumerate()]
        assert "prio-hub-reload-trigger" in names


class TestReloadDbs:
    def test_tracking_none_is_ok(self):
        state = MagicMock()
        _reload_dbs(state, None)
        state.reload.assert_called_once()


class TestUpdateIndexScript:
    def test_script_touches_reload_trigger(self):
        text = (REPO / "update_index.sh").read_text()
        assert "touch .reload_trigger" in text
        assert "scripts/sync.py" in text

    def test_wrapper_execs_update_index(self):
        assert "update_index.sh" in (REPO / "update_kb.sh").read_text()

    def test_reset_clears_tracker(self, tmp_path):
        script = tmp_path / "update_index.sh"
        script.write_text((REPO / "update_index.sh").read_text())
        script.chmod(0o755)
        (tmp_path / ".last_index_update").write_text("2026-01-01\n")
        subprocess.run([str(script), "--reset"], cwd=tmp_path, check=True)
        assert not (tmp_path / ".last_index_update").exists()

    def test_updating_md_documents_canonical_command(self):
        text = (REPO / "UPDATING.md").read_text()
        assert "./update_index.sh" in text
        assert "IssueStateDB.reload" in text
