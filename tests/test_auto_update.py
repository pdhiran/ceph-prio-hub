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
            patch("ceph_prio_hub.server.auto_update._maybe_sync_jira") as jira,
        ):
            _do_update(state, tracking, tmp_path)
        mock_exit.assert_called_once_with(0)
        state.reload.assert_not_called()
        tracking.reload.assert_not_called()
        jira.assert_not_called()

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
        assert "~/.ceph-prio-hub/state/" in text
        assert "tracking.json" in text

    def test_sync_py_writes_state_dir_not_tracking(self):
        text = (REPO / "scripts" / "sync.py").read_text()
        assert "IssueStateDB(config.state_dir)" in text
        assert "TrackingDB" not in text


class TestCanonicalPaths:
    def test_state_dir_is_under_config_dir(self):
        from ceph_prio_hub.config import CONFIG_DIR, STATE_DIR, TRACKING_FILE, ServerConfig

        assert STATE_DIR == CONFIG_DIR / "state"
        assert TRACKING_FILE == CONFIG_DIR / "tracking.json"
        assert TRACKING_FILE != STATE_DIR / "tracking.json"
        cfg = ServerConfig()
        assert cfg.state_dir == STATE_DIR
        assert cfg.tracking_file == TRACKING_FILE

    def test_custom_state_dir_keeps_tracking_as_sibling(self, tmp_path):
        from ceph_prio_hub.config import ServerConfig

        cfg = ServerConfig(state_dir=tmp_path / "state")
        assert cfg.tracking_file == tmp_path / "tracking.json"

    def test_tracking_db_default_is_tracking_file_not_state_dir(self):
        from ceph_prio_hub.config import STATE_DIR, TRACKING_FILE

        db = TrackingDB()
        assert db._path == TRACKING_FILE
        assert db._path != STATE_DIR / "tracking.json"

    def test_readme_state_dir_matches_constant(self):
        from ceph_prio_hub.config import STATE_DIR

        readme = (REPO / "README.md").read_text()
        assert "~/.ceph-prio-hub/state/" in readme
        assert STATE_DIR.name == "state"
        assert "tracking.json" in readme


class TestCrossProcessReload:
    def test_reload_sees_other_process_write_to_same_state_dir(self, tmp_path):
        mcp_db = IssueStateDB(tmp_path)
        other = IssueStateDB(tmp_path)
        issue = ConsolidatedIssue()
        issue._data["subject"] = "from-sync-py"
        other._issues[issue.issue_id] = issue
        other.save()

        mcp_db.reload()
        loaded = mcp_db.get_all_issues()
        assert len(loaded) == 1
        assert loaded[0]._data["subject"] == "from-sync-py"

    def test_reload_uses_instance_dir_not_a_global_path(self, tmp_path):
        decoy = tmp_path / "decoy"
        real = tmp_path / "real"
        decoy.mkdir()
        real.mkdir()

        decoy_db = IssueStateDB(decoy)
        decoy_issue = ConsolidatedIssue()
        decoy_issue._data["subject"] = "DECOY"
        decoy_db._issues[decoy_issue.issue_id] = decoy_issue
        decoy_db.save()

        real_db = IssueStateDB(real)
        real_issue = ConsolidatedIssue()
        real_issue._data["subject"] = "REAL"
        real_db._issues[real_issue.issue_id] = real_issue
        real_db.save()

        real_db._issues = {}
        real_db.reload()
        subjects = [i._data["subject"] for i in real_db.get_all_issues()]
        assert subjects == ["REAL"]


class TestMaybeSyncJira:
    def test_skipped_without_credentials(self, monkeypatch):
        from ceph_prio_hub.server.auto_update import _maybe_sync_jira

        monkeypatch.delenv("JIRA_USERNAME", raising=False)
        monkeypatch.delenv("JIRA_API_TOKEN", raising=False)
        state = MagicMock()
        _maybe_sync_jira(state)
        state.save.assert_not_called()
        state.add_jira_issue.assert_not_called()

    def test_skipped_with_only_username(self, monkeypatch):
        from ceph_prio_hub.server.auto_update import _maybe_sync_jira

        monkeypatch.setenv("JIRA_USERNAME", "user@ibm.com")
        monkeypatch.delenv("JIRA_API_TOKEN", raising=False)
        state = MagicMock()
        _maybe_sync_jira(state)
        state.save.assert_not_called()

    def test_runs_when_both_env_set(self, monkeypatch):
        from ceph_prio_hub.server.auto_update import _maybe_sync_jira

        monkeypatch.setenv("JIRA_USERNAME", "user@ibm.com")
        monkeypatch.setenv("JIRA_API_TOKEN", "token")
        state = MagicMock()
        state.last_sync = None
        with patch("ceph_prio_hub.jira.client.JiraClient") as jira_cls:
            jira_cls.return_value.fetch_prio_issues.return_value = []
            _maybe_sync_jira(state)
        jira_cls.assert_called_once()
        state.save.assert_called_once()


class TestTriggerIndependentOfJira:
    def test_trigger_reloads_without_calling_jira(self, tmp_path):
        state = MagicMock()
        tracking = MagicMock()
        stop = threading.Event()
        with (
            patch("ceph_prio_hub.server.auto_update.TRIGGER_POLL_SECONDS", 0.05),
            patch("ceph_prio_hub.server.auto_update._maybe_sync_jira") as jira,
        ):
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
        jira.assert_not_called()


def _call_tool(mcp, name, **kwargs):
    return mcp._tool_manager.get_tool(name).fn(**kwargs)


class TestMcpTrackingWiring:
    def test_tools_use_injected_tracking_db_not_a_second_instance(self, tmp_path):
        from ceph_prio_hub.config import ServerConfig
        from ceph_prio_hub.server.mcp_server import create_mcp_server

        tracking = TrackingDB(tmp_path / "tracking.json")
        tracking.set("IBMCEPH-1", {"qa_status": "reproducing"})
        tracking.save()
        cfg = ServerConfig(state_dir=tmp_path / "state")
        mcp = create_mcp_server(
            cfg,
            state_db=IssueStateDB(cfg.state_dir),
            tracking_db=tracking,
        )

        got = _call_tool(mcp, "get_tracking", issue_key="IBMCEPH-1")
        assert got["qa_status"] == "reproducing"

        tracking.set("IBMCEPH-1", {"qa_status": "verified"})
        got2 = _call_tool(mcp, "get_tracking", issue_key="IBMCEPH-1")
        assert got2["qa_status"] == "verified"

    def test_fallback_tracking_uses_config_tracking_file(self, tmp_path):
        from ceph_prio_hub.config import ServerConfig
        from ceph_prio_hub.server.mcp_server import create_mcp_server

        cfg = ServerConfig(state_dir=tmp_path / "state")
        captured: list[Path] = []
        real = TrackingDB

        def spy(path=None):
            db = real(path)
            captured.append(db._path)
            return db

        with patch("ceph_prio_hub.server.mcp_server.TrackingDB", side_effect=spy):
            create_mcp_server(cfg, state_db=IssueStateDB(cfg.state_dir))
        assert captured == [cfg.tracking_file]
        assert cfg.tracking_file == tmp_path / "tracking.json"

    def test_generate_dashboard_tool_passes_injected_tracking(self, tmp_path):
        from ceph_prio_hub.config import ServerConfig
        from ceph_prio_hub.server.mcp_server import create_mcp_server

        tracking = TrackingDB(tmp_path / "tracking.json")
        cfg = ServerConfig(state_dir=tmp_path / "state")
        mcp = create_mcp_server(
            cfg,
            state_db=IssueStateDB(cfg.state_dir),
            tracking_db=tracking,
        )
        out = tmp_path / "site"
        with patch("ceph_prio_hub.server.mcp_server.generate_dashboard") as gen:
            gen.return_value = out / "index.html"
            _call_tool(mcp, "generate_dashboard_tool", output_dir=str(out))
        gen.assert_called_once()
        assert gen.call_args.kwargs["tracking"] is tracking
