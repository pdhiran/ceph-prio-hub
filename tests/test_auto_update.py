"""Mock workflows: git-pull, .py exit, .reload_trigger, IssueStateDB.reload, update_index.sh."""

from __future__ import annotations

import importlib.util
import subprocess
import threading
import time
from datetime import date, timedelta
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


def _seed_state_and_tracking(
    state_dir: Path, tracking_path: Path, subject: str, status: str
) -> str:
    """Write issues + tracking as a separate process would, then return issue_id."""
    other_state = IssueStateDB(state_dir)
    issue = ConsolidatedIssue()
    issue._data["subject"] = subject
    other_state._issues[issue.issue_id] = issue
    other_state.save()
    other_tracking = TrackingDB(tracking_path)
    other_tracking.set("IBMCEPH-1", {"qa_status": status})
    other_tracking.save()
    return issue.issue_id


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

    def test_non_python_pull_reloads_real_state_and_tracking(self, tmp_path):
        state_dir = tmp_path / "state"
        tracking_path = tmp_path / "tracking.json"
        mcp_state = IssueStateDB(state_dir)
        mcp_tracking = TrackingDB(tracking_path)
        _seed_state_and_tracking(state_dir, tracking_path, "after-git-pull", "verified")

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
            _do_update(mcp_state, mcp_tracking, tmp_path)
        mock_exit.assert_not_called()
        jira.assert_called_once()
        loaded = mcp_state.get_all_issues()
        assert len(loaded) == 1
        assert loaded[0]._data["subject"] == "after-git-pull"
        assert mcp_tracking.get("IBMCEPH-1")["qa_status"] == "verified"

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

    def test_touch_trigger_reloads_real_state_and_tracking(self, tmp_path):
        state_dir = tmp_path / "state"
        tracking_path = tmp_path / "tracking.json"
        mcp_state = IssueStateDB(state_dir)
        mcp_tracking = TrackingDB(tracking_path)
        stop = threading.Event()
        with patch("ceph_prio_hub.server.auto_update.TRIGGER_POLL_SECONDS", 0.05):
            t = threading.Thread(
                target=_trigger_loop,
                args=(mcp_state, mcp_tracking, tmp_path, stop),
                daemon=True,
            )
            t.start()
            time.sleep(0.08)
            _seed_state_and_tracking(state_dir, tracking_path, "after-trigger", "reproducing")
            (tmp_path / ".reload_trigger").write_text("1")
            deadline = time.time() + 2.0
            while time.time() < deadline:
                issues = mcp_state.get_all_issues()
                if issues and issues[0]._data.get("subject") == "after-trigger":
                    break
                time.sleep(0.05)
            stop.set()
            t.join(timeout=1)
        loaded = mcp_state.get_all_issues()
        assert len(loaded) == 1
        assert loaded[0]._data["subject"] == "after-trigger"
        assert mcp_tracking.get("IBMCEPH-1")["qa_status"] == "reproducing"


class TestStartAutoUpdate:
    def test_no_remote_still_starts_trigger(self, tmp_path):
        (tmp_path / ".git").mkdir()
        with patch("ceph_prio_hub.server.auto_update._has_remote", return_value=False):
            start_auto_update(MagicMock(), MagicMock(), tmp_path, update_interval_hours=0)
        time.sleep(0.05)
        names = [t.name for t in threading.enumerate()]
        assert "prio-hub-reload-trigger" in names
        assert "prio-hub-auto-update" not in names

    def test_remote_starts_git_pull_and_trigger(self, tmp_path):
        (tmp_path / ".git").mkdir()
        started = threading.Event()
        release = threading.Event()

        def mark_pull(*_a, **_k):
            started.set()
            release.wait(timeout=2)

        with (
            patch("ceph_prio_hub.server.auto_update._has_remote", return_value=True),
            patch("ceph_prio_hub.server.auto_update._do_update", side_effect=mark_pull),
        ):
            start_auto_update(MagicMock(), MagicMock(), tmp_path, update_interval_hours=0)
            assert started.wait(timeout=1)
            names = [t.name for t in threading.enumerate()]
            release.set()
        assert "prio-hub-auto-update" in names
        assert "prio-hub-reload-trigger" in names
        assert "prio-hub-periodic-update" not in names


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

    def test_last_index_update_writes_yesterday_of_run_date(self, tmp_path):
        """Stub sync.py so this does not hit JIRA; only --reset is a live wrapper run."""
        src = (REPO / "update_index.sh").read_text()
        assert 'python3 scripts/sync.py --since "$SINCE" --verbose' in src
        assert 'date -v-1d +%Y-%m-%d > "$LAST_RUN_FILE"' in src
        script = tmp_path / "update_index.sh"
        script.write_text(
            src.replace('python3 scripts/sync.py --since "$SINCE" --verbose', "true")
        )
        script.chmod(0o755)
        subprocess.run([str(script)], cwd=tmp_path, check=True, capture_output=True, text=True)
        expected = subprocess.check_output(
            ["bash", "-c", 'date -v-1d +%Y-%m-%d 2>/dev/null || date -d "1 day ago" +%Y-%m-%d'],
            text=True,
        ).strip()
        assert (tmp_path / ".last_index_update").read_text().strip() == expected
        assert expected == (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")
        assert (tmp_path / ".reload_trigger").exists()

    def test_updating_md_documents_canonical_command(self):
        text = (REPO / "UPDATING.md").read_text()
        assert "./update_index.sh" in text
        assert "IssueStateDB.reload" in text
        assert "~/.ceph-prio-hub/state/" in text
        assert "tracking.json" in text
        assert "--no-auto-update" in text
        assert "trigger watcher" in text
        assert "yesterday of the run date" in text
        assert "1-day overlap" in text

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
        assert "git clone https://github.com/pdhiran/ceph-prio-hub.git" in readme
        assert "cd ceph-prio-hub" in readme
        assert "--no-auto-update" in readme
        assert "updated >=" in readme
        assert "1-day overlap" in readme


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


class TestSinceDelta:
    def test_sync_jira_issues_rejects_invalid_since_without_jira(self, tmp_path):
        from ceph_prio_hub.config import ServerConfig
        from ceph_prio_hub.server.mcp_server import create_mcp_server

        cfg = ServerConfig(state_dir=tmp_path / "state")
        mcp = create_mcp_server(
            cfg,
            state_db=IssueStateDB(cfg.state_dir),
            tracking_db=TrackingDB(tmp_path / "tracking.json"),
        )
        with patch("ceph_prio_hub.server.mcp_server.JiraClient") as jira_cls:
            out = _call_tool(mcp, "sync_jira_issues", since="not-a-date")
        assert "Invalid since date" in out["error"]
        assert "YYYY-MM-DD" in out["error"]
        jira_cls.assert_not_called()

    def test_fetch_jira_issues_rejects_invalid_since_without_jira(self, tmp_path):
        from ceph_prio_hub.config import ServerConfig
        from ceph_prio_hub.server.mcp_server import create_mcp_server

        cfg = ServerConfig(state_dir=tmp_path / "state")
        mcp = create_mcp_server(
            cfg,
            state_db=IssueStateDB(cfg.state_dir),
            tracking_db=TrackingDB(tmp_path / "tracking.json"),
        )
        with patch("ceph_prio_hub.server.mcp_server.JiraClient") as jira_cls:
            out = _call_tool(mcp, "fetch_jira_issues", since="not-a-date")
        assert "Invalid since date" in out["error"]
        jira_cls.assert_not_called()

    def test_sync_jira_issues_passes_iso_since(self, tmp_path):
        from ceph_prio_hub.config import ServerConfig
        from ceph_prio_hub.server.mcp_server import create_mcp_server

        cfg = ServerConfig(state_dir=tmp_path / "state")
        mcp = create_mcp_server(
            cfg,
            state_db=IssueStateDB(cfg.state_dir),
            tracking_db=TrackingDB(tmp_path / "tracking.json"),
        )
        with patch("ceph_prio_hub.server.mcp_server.JiraClient") as jira_cls:
            jira_cls.return_value.fetch_prio_issues.return_value = []
            out = _call_tool(mcp, "sync_jira_issues", since="2026-08-01")
        assert "error" not in out
        jira_cls.return_value.fetch_prio_issues.assert_called_once()
        assert jira_cls.return_value.fetch_prio_issues.call_args.kwargs["since"] == "2026-08-01"

    def test_fetch_prio_emails_rejects_invalid_since_before_graph(self, tmp_path):
        from ceph_prio_hub.config import ServerConfig
        from ceph_prio_hub.server.mcp_server import create_mcp_server

        cfg = ServerConfig(state_dir=tmp_path / "state")
        mcp = create_mcp_server(
            cfg,
            state_db=IssueStateDB(cfg.state_dir),
            tracking_db=TrackingDB(tmp_path / "tracking.json"),
        )
        with patch("ceph_prio_hub.server.mcp_server.GraphClient") as graph_cls:
            out = _call_tool(mcp, "fetch_prio_emails", since="not-a-date")
        assert "Invalid since date" in out["error"]
        graph_cls.assert_not_called()

    def test_sync_py_rejects_invalid_since(self):
        r = subprocess.run(
            ["python3", str(REPO / "scripts" / "sync.py"), "--since", "not-a-date"],
            cwd=REPO, capture_output=True, text=True,
        )
        assert r.returncode != 0
        assert "YYYY-MM-DD" in r.stderr

    def test_sync_py_main_invalid_since_never_constructs_jira_client(self):
        spec = importlib.util.spec_from_file_location(
            "prio_hub_sync_script", REPO / "scripts" / "sync.py"
        )
        mod = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(mod)
        with (
            patch.object(mod, "JiraClient") as jira_cls,
            pytest.raises(SystemExit) as exc,
        ):
            mod.main(["--since", "not-a-date"])
        assert exc.value.code == 2
        jira_cls.assert_not_called()

    def test_sync_py_since_jql_is_updated_gte(self, tmp_path, monkeypatch):
        from ceph_prio_hub.config import ServerConfig
        from ceph_prio_hub.jira.client import JiraClient

        spec = importlib.util.spec_from_file_location(
            "prio_hub_sync_script_jql", REPO / "scripts" / "sync.py"
        )
        mod = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(mod)

        cfg = ServerConfig(state_dir=tmp_path / "state")
        captured: dict[str, str] = {}

        def fake_paginate(self, jql, limit=None):
            captured["jql"] = jql
            return []

        monkeypatch.setenv("JIRA_USERNAME", "mock-user")
        monkeypatch.setenv("JIRA_API_TOKEN", "mock-token")
        with (
            patch.object(mod.ServerConfig, "load", return_value=cfg),
            patch.object(JiraClient, "_paginate_jql", fake_paginate),
            patch.object(JiraClient, "_get") as mock_get,
        ):
            rc = mod.main(["--since", "2026-08-01"])
        assert rc == 0
        mock_get.assert_not_called()
        assert "updated >= \"2026-08-01\"" in captured["jql"]
        assert captured["jql"].startswith('project = "IBMCEPH"')

    def test_jira_client_since_clause_is_updated_gte(self):
        from ceph_prio_hub.jira.client import JiraClient

        captured: dict[str, str] = {}

        def fake_paginate(self, jql, limit=None):
            captured["jql"] = jql
            return []

        client = JiraClient(username="u", api_token="t")
        with (
            patch.object(JiraClient, "_paginate_jql", fake_paginate),
            patch.object(JiraClient, "_get") as mock_get,
        ):
            client.fetch_prio_issues(since="2026-08-01", limit=10)
        mock_get.assert_not_called()
        assert ' AND updated >= "2026-08-01"' in captured["jql"]
        assert "updated > " not in captured["jql"].replace("updated >=", "")

    def test_publish_invalid_since_never_constructs_jira_client(self):
        from ceph_prio_hub.dashboard.publish import main as publish_main

        with (
            patch("ceph_prio_hub.dashboard.publish.JiraClient") as jira_cls,
            pytest.raises(SystemExit) as exc,
        ):
            publish_main(["--since", "not-a-date"])
        assert exc.value.code == 2
        jira_cls.assert_not_called()


class TestSseCli:
    def test_help_documents_host_and_port_8080(self):
        r = subprocess.run(
            ["python3", "-m", "ceph_prio_hub.server.mcp_server", "--help"],
            cwd=REPO, capture_output=True, text=True,
        )
        assert r.returncode == 0
        assert "--host" in r.stdout
        assert "0.0.0.0" in r.stdout
        assert "8080" in r.stdout
        assert "--no-auto-update" in r.stdout
        assert ".reload_trigger" in r.stdout
        assert "restart" in r.stdout.lower()
        compact = " ".join(r.stdout.split()).lower()
        assert "disables both" in compact

    def test_sse_main_applies_host_and_default_port(self, tmp_path):
        from ceph_prio_hub.config import ServerConfig
        from ceph_prio_hub.server.mcp_server import main

        cfg = ServerConfig(state_dir=tmp_path / "state")
        captured: dict = {}

        def fake_run(self, transport="stdio", mount_path=None):
            captured["host"] = self.settings.host
            captured["port"] = self.settings.port
            captured["transport"] = transport

        with (
            patch("ceph_prio_hub.server.mcp_server.ServerConfig.load", return_value=cfg),
            patch("ceph_prio_hub.server.mcp_server.FastMCP.run", fake_run),
            patch("ceph_prio_hub.server.auto_update.start_auto_update") as mock_start,
        ):
            main([
                "--transport", "sse", "--host", "127.0.0.1", "--port", "9999",
                "--no-auto-update",
            ])
        mock_start.assert_not_called()
        assert captured["host"] == "127.0.0.1"
        assert captured["port"] == 9999
        assert captured["transport"] == "sse"

    def test_sse_defaults_host_0_0_0_0_port_8080(self, tmp_path):
        from ceph_prio_hub.config import ServerConfig
        from ceph_prio_hub.server.mcp_server import main

        cfg = ServerConfig(state_dir=tmp_path / "state")
        captured: dict = {}

        def fake_run(self, transport="stdio", mount_path=None):
            captured["host"] = self.settings.host
            captured["port"] = self.settings.port

        with (
            patch("ceph_prio_hub.server.mcp_server.ServerConfig.load", return_value=cfg),
            patch("ceph_prio_hub.server.mcp_server.FastMCP.run", fake_run),
            patch("ceph_prio_hub.server.auto_update.start_auto_update"),
        ):
            main(["--transport", "sse", "--no-auto-update"])
        assert captured["host"] == "0.0.0.0"
        assert captured["port"] == 8080

    def test_readme_sse_command_is_accepted_by_argparse(self):
        readme = (REPO / "README.md").read_text()
        assert "--transport sse --host 0.0.0.0 --port 8080" in readme
        r = subprocess.run(
            [
                "python3", "-m", "ceph_prio_hub.server.mcp_server",
                "--transport", "sse", "--host", "0.0.0.0", "--port", "8080", "--help",
            ],
            cwd=REPO, capture_output=True, text=True,
        )
        assert r.returncode == 0
        assert "unrecognized arguments" not in r.stderr


class TestNoAutoUpdateKillsBoth:
    def _run_main(self, tmp_path, argv):
        from ceph_prio_hub.config import ServerConfig
        from ceph_prio_hub.server.mcp_server import main

        cfg = ServerConfig(state_dir=tmp_path / "state")
        with (
            patch("ceph_prio_hub.server.mcp_server.ServerConfig.load", return_value=cfg),
            patch("ceph_prio_hub.server.mcp_server.FastMCP.run"),
            patch("ceph_prio_hub.server.auto_update.start_auto_update") as mock_start,
        ):
            main(argv)
        return mock_start

    def test_default_auto_update_starts_with_both_dbs(self, tmp_path):
        mock_start = self._run_main(tmp_path, ["--transport", "sse"])
        mock_start.assert_called_once()
        state_db, tracking_db, repo_root = mock_start.call_args[0]
        assert isinstance(state_db, IssueStateDB)
        assert isinstance(tracking_db, TrackingDB)
        assert repo_root == REPO
        assert mock_start.call_args.kwargs["update_interval_hours"] == 1

    def test_no_auto_update_does_not_start_git_or_trigger(self, tmp_path):
        mock_start = self._run_main(
            tmp_path,
            ["--transport", "sse", "--no-auto-update", "--update-interval", "1"],
        )
        mock_start.assert_not_called()
        names = [t.name for t in threading.enumerate()]
        assert "prio-hub-auto-update" not in names
        assert "prio-hub-reload-trigger" not in names
        assert "prio-hub-periodic-update" not in names

    def test_start_auto_update_is_only_under_auto_update_guard(self):
        text = (REPO / "src/ceph_prio_hub/server/mcp_server.py").read_text()
        guard = text.split("if args.auto_update:")[1].split("if args.transport")[0]
        assert "start_auto_update" in guard
        before = text.split("if args.auto_update:")[0]
        assert "start_auto_update(" not in before


class TestCapabilitiesAndHealth:
    def test_capabilities_tools_match_registry(self, tmp_path):
        from ceph_prio_hub.config import ServerConfig
        from ceph_prio_hub.server.mcp_server import create_mcp_server

        cfg = ServerConfig(state_dir=tmp_path / "state")
        mcp = create_mcp_server(
            cfg,
            state_db=IssueStateDB(cfg.state_dir),
            tracking_db=TrackingDB(tmp_path / "tracking.json"),
        )
        names = set(mcp._tool_manager._tools.keys())
        caps = _call_tool(mcp, "capabilities")
        assert set(caps["tools"]) == names
        assert caps["data_sources"]["jira"]["status"] == "primary"
        assert caps["data_sources"]["email"]["status"] == "optional"

    def test_fastmcp_instructions_are_jira_first(self, tmp_path):
        from ceph_prio_hub.config import ServerConfig
        from ceph_prio_hub.server.mcp_server import create_mcp_server

        cfg = ServerConfig(state_dir=tmp_path / "state")
        mcp = create_mcp_server(
            cfg,
            state_db=IssueStateDB(cfg.state_dir),
            tracking_db=TrackingDB(tmp_path / "tracking.json"),
        )
        text = mcp.instructions or ""
        assert "JIRA" in text
        assert "primary" in text.lower()
        assert "optional" in text.lower()
        assert "sync_jira_issues" in text
        assert "fetch_prio_emails" not in text

    def test_health_reports_state_and_tracking_paths(self, tmp_path):
        from ceph_prio_hub.config import ServerConfig
        from ceph_prio_hub.server.mcp_server import create_mcp_server

        cfg = ServerConfig(state_dir=tmp_path / "state")
        mcp = create_mcp_server(
            cfg,
            state_db=IssueStateDB(cfg.state_dir),
            tracking_db=TrackingDB(tmp_path / "tracking.json"),
        )
        with patch("ceph_prio_hub.server.mcp_server.JiraClient") as jira_cls:
            jira_cls.return_value.health.return_value = {"ok": True, "user": "test"}
            h = _call_tool(mcp, "health")
        assert h["state_dir"] == str(cfg.state_dir)
        assert h["tracking_file"] == str(cfg.tracking_file)


class TestStateDoesNotWriteTracking:
    def test_save_does_not_create_sibling_tracking_json(self, tmp_path):
        db = IssueStateDB(tmp_path / "state")
        db.save()
        assert (tmp_path / "state" / "issues.json").exists()
        assert (tmp_path / "state" / "sync_metadata.json").exists()
        assert not (tmp_path / "tracking.json").exists()
        assert not (tmp_path / "state" / "tracking.json").exists()
