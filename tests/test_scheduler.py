import os
import pytest
from unittest.mock import patch, MagicMock

from jrun.errors import ApiError
from jrun.models import ACTIVE_STATUSES, TERMINAL_STATUSES, Job, ResourceConfig, RunOptions
from jrun.project import Project
from jrun.scheduler import Scheduler


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
        project = Project.init(directory=tmp_path)
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
        project = Project.init(directory=tmp_path)
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

        scheduler.platform.run_config.assert_called_once_with(
            "eid-1", "pending-job", run_options=RunOptions(), queue_category=None,
        )
        scheduler.platform.modify_configs.assert_not_called()
        tracker = project.load_tracker()
        assert "old-id" not in tracker["jobs"]
        assert tracker["jobs"]["new-id"]["name"] == "pending-job"
        assert tracker["searches"]["search"]["job_ids"] == ["new-id"]


class TestSchedulerRunOptions:
    @patch("jrun.scheduler.time.sleep")
    def test_forwards_persisted_category_and_run_options(self, mock_sleep, tmp_path):
        project = Project.init(directory=tmp_path)
        options = RunOptions(duration_hours=2, usage_desc="u", queue_priority_mode="high")
        project.record_pending_jobs(
            "search", "config.yaml",
            [Job(name="pub", run_options=options, resource_config=ResourceConfig(
                queue_name="Public-Pool", queue_category="QUEUE_CATEGORY_PUBLIC"))],
            experiment_id="eid-1",
        )
        scheduler = Scheduler(project.jrun_dir, "search", "exp", "eid-1", 1, 0)
        scheduler.platform = MagicMock()
        scheduler._api = MagicMock()
        scheduler._api.resolve_project_context.return_value = ("p1", "ps1")
        scheduler._api.shared_queue_budgets.return_value = {}  # unlimited
        scheduler.platform.run_config.return_value = "new-id"
        scheduler.platform.get_job_info.return_value = {"status": "Succeed"}
        scheduler._log = MagicMock()

        scheduler.run_loop()

        scheduler.platform.run_config.assert_called_once_with(
            "eid-1", "pub", run_options=options, queue_category="QUEUE_CATEGORY_PUBLIC",
        )

    @patch("jrun.scheduler.time.sleep")
    def test_invalid_job_data_is_skipped_not_fatal(self, mock_sleep, tmp_path):
        project = Project.init(directory=tmp_path)
        ids = project.record_pending_jobs("search", "c.yaml", [{"name": "old"}])
        with project._locked_tracker() as tracker:
            tracker["jobs"][ids[0]]["job_data"]["resource_config"] = {"worker": {"replicas": 2}}
        scheduler = Scheduler(project.jrun_dir, "search", "exp", "eid-1", 1, 0)
        scheduler.platform = MagicMock()
        scheduler._log = MagicMock()
        # A job that can never be submitted keeps the loop alive; stop after one pass.
        mock_sleep.side_effect = [None, KeyboardInterrupt]
        with pytest.raises(KeyboardInterrupt):
            scheduler.run_loop()
        scheduler.platform.run_config.assert_not_called()
        assert any("invalid job_data" in str(c) for c in scheduler._log.call_args_list)

    def test_pid_management_needs_only_dir_and_name(self, tmp_path):
        scheduler = Scheduler(tmp_path, "s")
        assert scheduler.is_running() is None
        assert scheduler.stop() is False


class TestSchedulerSlots:
    def _scheduler(self, project, parallel=0):
        scheduler = Scheduler(project.jrun_dir, "search", "exp", "eid-1", parallel, 0)
        scheduler.platform = MagicMock()
        scheduler.platform.get_job_info.return_value = {"status": "Running"}
        scheduler._log = MagicMock()
        return scheduler

    @staticmethod
    def _shared(name, category="public"):
        return Job(name=name, resource_config=ResourceConfig(
            queue_name="Public-Pool", queue_category=category, project_set_name="ps"))

    @patch("jrun.scheduler.time.sleep")
    def test_zero_parallel_trials_means_unlimited(self, mock_sleep, tmp_path):
        project = Project.init(directory=tmp_path)
        project.record_pending_jobs("search", "c.yaml", [Job(name="a"), Job(name="b")],
                                    experiment_id="eid-1")
        scheduler = self._scheduler(project)
        scheduler.platform.run_config.side_effect = ["id-a", "id-b"]
        scheduler.platform.get_job_info.return_value = {"status": "Succeed"}
        scheduler.run_loop()
        assert scheduler.platform.run_config.call_count == 2

    @patch("jrun.scheduler.PlatformAPI")
    @patch("jrun.scheduler.time.sleep")
    def test_shared_jobs_wait_for_submit_slots(self, mock_sleep, api_cls, tmp_path):
        project = Project.init(directory=tmp_path)
        project.record_pending_jobs(
            "search", "c.yaml",
            [self._shared("p1"), self._shared("p2"), self._shared("i1", "idle"),
             Job(name="d1")],
            experiment_id="eid-1")
        api = api_cls.return_value
        api.resolve_project_context.return_value = (None, "ps-1")
        api.shared_queue_budgets.return_value = {"QUEUE_CATEGORY_PUBLIC": 1,
                                                 "QUEUE_CATEGORY_IDLE": 0}
        scheduler = self._scheduler(project)
        scheduler.platform.run_config.side_effect = ["id-p1", "id-d1"]
        mock_sleep.side_effect = [None, None, KeyboardInterrupt]  # one poll
        with pytest.raises(KeyboardInterrupt):
            scheduler.run_loop()
        # One public slot: p1 runs, p2 waits; idle has none; dedicated d1 always runs.
        assert [c.args[1] for c in scheduler.platform.run_config.call_args_list] == ["p1", "d1"]
        api.shared_queue_budgets.assert_called_once_with(proj_id=None, projset_id="ps-1")
        statuses = {info["name"]: info["status"]
                    for info in project.load_tracker()["jobs"].values()}
        assert statuses["p2"] == "Queued" and statuses["i1"] == "Queued"

    @patch("jrun.scheduler.PlatformAPI")
    @patch("jrun.scheduler.time.sleep")
    def test_budget_read_failure_falls_back_to_trying(self, mock_sleep, api_cls, tmp_path):
        project = Project.init(directory=tmp_path)
        project.record_pending_jobs("search", "c.yaml", [self._shared("p1")],
                                    experiment_id="eid-1")
        api_cls.return_value.resolve_project_context.side_effect = ApiError("/x", 500, "down")
        scheduler = self._scheduler(project)
        scheduler.platform.run_config.return_value = "id-p1"
        scheduler.platform.get_job_info.return_value = {"status": "Succeed"}
        scheduler.run_loop()
        scheduler.platform.run_config.assert_called_once()
        assert any("trying anyway" in str(c) for c in scheduler._log.call_args_list)


class TestAirsctlContext:
    def test_context_persisted_and_applied(self, tmp_path, monkeypatch):
        import json
        jrun_dir = tmp_path / ".jrun"
        jrun_dir.mkdir()
        scheduler = Scheduler(jrun_dir, "s", "exp", "eid", 0, 5, airsctl_context=("p1", "ps1"))
        assert scheduler.platform.context == ("p1", "ps1")
        with patch("subprocess.Popen") as popen:
            popen.return_value.pid = 123
            scheduler.start([])
        args = json.loads(scheduler._args_file.read_text())
        assert args["airsctl_context"] == ["p1", "ps1"]

    def test_no_context_by_default(self, scheduler):
        assert scheduler.airsctl_context is None
        assert scheduler.platform.context == (None, None)
