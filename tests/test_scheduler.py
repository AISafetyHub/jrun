import os
import signal
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from jrun.models import Job
from jrun.project import Project
from jrun.scheduler import Scheduler, TERMINAL_STATUSES, ACTIVE_STATUSES


@pytest.fixture
def scheduler(tmp_path):
    jrun_dir = tmp_path / ".jrun"
    jrun_dir.mkdir()
    return Scheduler(
        jrun_dir=jrun_dir,
        search_name="test-search",
        exp_name="exp",
        exp_id="eid-1",
        parallel_trials=2,
        poll_interval=5,
    )


class TestSchedulerPaths:
    def test_scheduler_dir(self, scheduler):
        assert scheduler._scheduler_dir == scheduler.jrun_dir / "scheduler"

    def test_pid_file(self, scheduler):
        assert scheduler._pid_file.name == "test-search.pid"

    def test_log_file(self, scheduler):
        assert scheduler._log_file.name == "test-search.log"

    def test_args_file(self, scheduler):
        assert scheduler._args_file.name == "test-search.args.json"


class TestIsRunning:
    def test_no_pid_file(self, scheduler):
        assert scheduler.is_running() is None

    def test_pid_file_with_dead_process(self, scheduler):
        scheduler._scheduler_dir.mkdir(exist_ok=True)
        scheduler._pid_file.write_text("99999999")
        assert scheduler.is_running() is None
        assert not scheduler._pid_file.exists()

    def test_pid_file_with_live_process(self, scheduler):
        scheduler._scheduler_dir.mkdir(exist_ok=True)
        scheduler._pid_file.write_text(str(os.getpid()))
        assert scheduler.is_running() == os.getpid()

    def test_pid_file_invalid_content(self, scheduler):
        scheduler._scheduler_dir.mkdir(exist_ok=True)
        scheduler._pid_file.write_text("not-a-number")
        assert scheduler.is_running() is None


class TestStop:
    def test_stop_no_process(self, scheduler):
        assert scheduler.stop() is False

    @patch("os.kill")
    def test_stop_running_process(self, mock_kill, scheduler):
        scheduler._scheduler_dir.mkdir(exist_ok=True)
        scheduler._pid_file.write_text(str(os.getpid()))
        mock_kill.side_effect = [None, None]  # first for is_running check, second for SIGTERM
        result = scheduler.stop()
        assert result is True
        assert not scheduler._pid_file.exists()


class TestConstants:
    def test_terminal_statuses(self):
        assert "Failed" in TERMINAL_STATUSES
        assert "Succeed" in TERMINAL_STATUSES
        assert "Completed" in TERMINAL_STATUSES
        assert "Stopped" in TERMINAL_STATUSES
        assert "Cancelled" in TERMINAL_STATUSES

    def test_active_statuses(self):
        assert "Running" in ACTIVE_STATUSES
        assert "Submitted" in ACTIVE_STATUSES
        assert "Queued" in ACTIVE_STATUSES


class TestSchedulerSync:
    def test_run_loop_persists_job_platform(self, tmp_path):
        project = Project.init("exp", experiment_id="eid-1", directory=tmp_path)
        entries = [
            {"job_id": "j1", "name": "job-1", "params": {}},
        ]
        project.record_search(
            "test-search", "search.yaml", entries,
            experiment_id="eid-1", experiment_name="exp",
        )
        project.update_job_status("j1", "Running")
        scheduler = Scheduler(
            jrun_dir=project.jrun_dir,
            search_name="test-search",
            exp_name="exp",
            exp_id="eid-1",
            parallel_trials=1,
            poll_interval=0,
        )
        scheduler.platform = MagicMock()
        scheduler.platform.get_job_info.return_value = {
            "status": "Succeed",
            "projset_id": "ps1",
            "proj_id": "p1",
            "creator_id": 319832320808853520,
            "projset_name": "baai-safety",
            "proj_name": "baai-safety_research",
            "cluster_name": "dx-calc1",
            "zone_name": "dx-calc1-zonea",
        }
        scheduler._log = MagicMock()
        scheduler.run_loop()

        tracker = project.load_tracker()
        job = tracker["jobs"]["j1"]
        assert job["status"] == "Succeed"
        assert job["platform"]["clusterName"] == "dx-calc1"
        assert job["platform"]["zoneName"] == "dx-calc1-zonea"
        assert job["experiment_id"] == "eid-1"
        scheduler.platform.get_job_info.assert_called_once_with("j1")

    @patch("jrun.scheduler.time.sleep")
    def test_pending_job_only_runs_existing_config_and_replaces_local_id(
        self, mock_sleep, tmp_path
    ):
        project = Project.init("exp", experiment_id="eid-1", directory=tmp_path)
        project.record_single_job("old-id", "pending-job")
        project.record_pending_jobs(
            "search", "config.yaml",
            [Job(name="pending-job", command="echo hi")],
            experiment_id="eid-1", experiment_name="exp",
            replacement_ids={"pending-job": "old-id"},
        )
        scheduler = Scheduler(
            jrun_dir=project.jrun_dir,
            search_name="search",
            exp_name="exp",
            exp_id="eid-1",
            parallel_trials=1,
            poll_interval=0,
        )
        scheduler.platform = MagicMock()
        scheduler.platform.run_config.return_value = "new-id"
        scheduler.platform.get_job_info.return_value = {"status": "Succeed"}
        scheduler._log = MagicMock()

        scheduler.run_loop()

        scheduler.platform.run_config.assert_called_once_with("eid-1", "pending-job")
        scheduler.platform.submit_job.assert_not_called()
        scheduler.platform.modify_configs.assert_not_called()
        tracker = project.load_tracker()
        assert "old-id" not in tracker["jobs"]
        assert tracker["jobs"]["new-id"]["name"] == "pending-job"
        assert tracker["searches"]["search"]["job_ids"] == ["new-id"]
