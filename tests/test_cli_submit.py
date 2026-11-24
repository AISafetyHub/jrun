import json
from unittest.mock import MagicMock, patch

import pytest

from jrun.cli import cli
from jrun.errors import ApiError, PlatformError
from jrun.models import RunOptions
from tests.conftest import SAMPLE_QUEUE, SPEC_CONFIG
from tests.test_placement import A100, V100, FakeAPI, shared_row

EXP = "experiment:\n  name: test-exp\n  image: img:1\n  queue_name: jiuding_airs1_h100\n"
JOB_CONFIG = EXP + "job:\n  name: test-job\n  command: echo hello\n"


@pytest.fixture
def platform():
    """Mocked PlatformClient/PlatformAPI with an existing experiment eid-123."""
    with patch("jrun.commands.submit.PlatformClient") as platform_cls, \
            patch("jrun.commands.submit.PlatformAPI") as api_cls:
        client = MagicMock()
        client.find_experiment.return_value = {"experiment_id": "eid-123"}
        client.get_experiment.return_value = {"experiment_id": "eid-123",
                                              "advance_config_infos": [{}]}
        client.run_config.return_value = "uuid-1234-5678"
        client.get_job_status.return_value = "Submitted"
        client.get_job_info.return_value = None
        platform_cls.return_value = client
        api = MagicMock()
        api.find_queue.return_value = SAMPLE_QUEUE
        api_cls.return_value = api
        client.api = api
        yield client


def _write(tmp_path, text, name="job.yaml"):
    path = tmp_path / name
    path.write_text(text)
    return str(path)


def _set_tracker(project_dir, jobs):
    (project_dir / ".jrun" / "jobs.json").write_text(json.dumps({"searches": {}, "jobs": jobs}))


class TestSubmitDryRun:
    def test_dry_run_single_job(self, runner, project_dir, tmp_path, monkeypatch):
        monkeypatch.chdir(project_dir)
        result = runner.invoke(cli, ["submit", _write(tmp_path, JOB_CONFIG), "--dry-run"])
        assert result.exit_code == 0
        assert "test-job" in result.output
        assert "Queue: jiuding_airs1_h100 (dedicated)" in result.output

    def test_dry_run_search(self, runner, project_dir, tmp_path, monkeypatch):
        monkeypatch.chdir(project_dir)
        config = _write(tmp_path, EXP + """
search:
  job_template:
    name: job-{lr}
    command: train.py --lr {lr}
  params:
    - name: lr
      values: [0.01, 0.001]
""")
        result = runner.invoke(cli, ["submit", config, "--dry-run"])
        assert result.exit_code == 0
        assert "job-0.01" in result.output
        assert "job-0.001" in result.output

    def test_dry_run_without_experiment_warns(self, runner, project_dir, tmp_path, monkeypatch):
        monkeypatch.chdir(project_dir)
        config = _write(tmp_path, "job:\n  name: j\n  command: c\n")
        result = runner.invoke(cli, ["submit", config, "--dry-run"])
        assert result.exit_code == 0
        assert "no 'experiment:' section" in result.output

    def test_dry_run_local_job_needs_no_experiment(self, runner, project_dir, tmp_path,
                                                   monkeypatch):
        monkeypatch.chdir(project_dir)
        config = _write(tmp_path, "job:\n  name: j\n  command: c\n"
                                  "  resource_config:\n    queue_name: local\n")
        result = runner.invoke(cli, ["submit", config, "--dry-run"])
        assert result.exit_code == 0
        assert "experiment" not in result.output

    @patch("jrun.commands.submit.PlatformAPI")
    def test_dry_run_does_not_touch_api(self, mock_api_cls, runner, project_dir,
                                        tmp_path, monkeypatch):
        monkeypatch.chdir(project_dir)
        result = runner.invoke(cli, ["submit", _write(tmp_path, SPEC_CONFIG), "--dry-run"])
        assert result.exit_code == 0
        assert "will be created if missing" in result.output
        mock_api_cls.assert_not_called()

    def test_dry_run_validates_public_run_options(self, runner, project_dir, tmp_path,
                                                  monkeypatch):
        monkeypatch.chdir(project_dir)
        config = _write(tmp_path, EXP + """
job:
  name: j
  command: c
  resource_config:
    queue_category: public
    queue_name: Public-Pool
""")
        result = runner.invoke(cli, ["submit", config, "--dry-run"])
        assert result.exit_code == 1
        assert "public queue requires" in result.output


class TestSubmitErrors:
    """Config problems must be reported as `Error:` lines, never tracebacks."""

    @pytest.mark.parametrize("text,message", [
        ("job:\n  name: a\n  command: echo $JRUN_TEST_UNDEFINED\n", "undefined variable"),
        ("job:\n  name: a\n  resource_config:\n    queue_id: x\n", "IDs/display names were removed"),
        ("job:\n  name: a\n  resource_config:\n    worker: {replicas: 2}\n", "worker was removed"),
        ("job:\n  name: a\n  experiment_id: e\n", "no longer supported"),
        ("job: [broken\n", "invalid YAML"),
        ("experiment:\n  name: e\n", "neither 'search' nor 'job'"),
    ])
    def test_config_errors_are_clean(self, runner, project_dir, tmp_path, monkeypatch,
                                     text, message):
        monkeypatch.chdir(project_dir)
        monkeypatch.delenv("JRUN_TEST_UNDEFINED", raising=False)
        result = runner.invoke(cli, ["submit", _write(tmp_path, text), "--dry-run"])
        assert result.exit_code == 1
        assert "Error: " in result.output and message in result.output
        assert isinstance(result.exception, SystemExit)  # not an uncaught traceback

    def test_missing_project_is_clean(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(cli, ["submit", _write(tmp_path, JOB_CONFIG), "--dry-run"])
        assert result.exit_code == 1
        assert "jrun init" in result.output

    def test_no_experiment_block_errors(self, runner, project_dir, tmp_path, monkeypatch,
                                        platform):
        monkeypatch.chdir(project_dir)
        result = runner.invoke(cli, ["submit", _write(tmp_path, "job:\n  name: j\n  command: c\n")])
        assert result.exit_code == 1
        assert "no 'experiment:' section" in result.output
        platform.modify_configs.assert_not_called()


class TestLocalRun:
    def test_local_job_runs_without_platform(self, runner, project_dir, tmp_path, monkeypatch):
        monkeypatch.chdir(project_dir)
        config = _write(tmp_path, "job:\n  name: j\n  command: exit 4\n"
                                  "  resource_config:\n    queue_name: local\n")
        with patch("jrun.commands.submit.PlatformClient") as platform_cls:
            result = runner.invoke(cli, ["submit", config])
        assert result.exit_code == 4
        platform_cls.assert_not_called()


class TestSubmitSingle:
    def test_submit_records_and_shows_links(self, runner, project_dir, tmp_path,
                                            monkeypatch, platform):
        monkeypatch.chdir(project_dir)
        platform.get_job_info.return_value = {
            "status": "Succeed", "projset_id": "ps1", "proj_id": "p1",
            "creator_id": 319832320808853520, "projset_name": "baai-safety",
            "proj_name": "baai-safety_research", "cluster_name": "dx-calc1",
            "zone_name": "dx-calc1-zonea",
        }
        result = runner.invoke(cli, ["submit", _write(tmp_path, JOB_CONFIG)])
        assert result.exit_code == 0, result.output
        assert platform.modify_configs.call_args.args[1] == "eid-123"
        platform.run_config.assert_called_once_with(
            "eid-123", "test-job", run_options=RunOptions(),
            queue_category=None,
        )
        stored = json.loads((project_dir / ".jrun" / "jobs.json").read_text())
        job = stored["jobs"]["uuid-1234-5678"]
        assert job["status"] == "Succeed"
        assert job["experiment_name"] == "test-exp"
        assert job["platform"]["clusterName"] == "dx-calc1"
        assert "platform-multi.baai.ac.cn" in result.output

    def test_overwrite_stops_running_old_job(self, runner, project_dir, tmp_path,
                                             monkeypatch, platform):
        monkeypatch.chdir(project_dir)
        _set_tracker(project_dir, {"old-uuid": {"name": "test-job", "status": "Running"}})
        platform.get_job_status.return_value = "Running"
        result = runner.invoke(cli, ["submit", _write(tmp_path, JOB_CONFIG), "-o"], input="y\n")
        assert result.exit_code == 0, result.output
        platform.job_stop.assert_called_once_with("old-uuid")
        stored = json.loads((project_dir / ".jrun" / "jobs.json").read_text())
        assert list(stored["jobs"]) == ["uuid-1234-5678"]

    def test_overwrite_stop_failure_aborts(self, runner, project_dir, tmp_path,
                                           monkeypatch, platform):
        monkeypatch.chdir(project_dir)
        _set_tracker(project_dir, {"old-uuid": {"name": "test-job", "status": "Running"}})
        platform.get_job_status.return_value = "Running"
        platform.job_stop.side_effect = PlatformError("job stop", 1, "denied", "")
        result = runner.invoke(cli, ["submit", _write(tmp_path, JOB_CONFIG), "-o"], input="y\n")
        assert result.exit_code == 1
        assert "denied" in result.output
        platform.modify_configs.assert_not_called()

    def test_overwrite_queued_job_does_not_touch_platform_job(self, runner, project_dir,
                                                              tmp_path, monkeypatch, platform):
        monkeypatch.chdir(project_dir)
        _set_tracker(project_dir, {"queued-abc123": {"name": "test-job", "status": "Queued"}})
        result = runner.invoke(cli, ["submit", _write(tmp_path, JOB_CONFIG), "-o", "-s", "Queued"],
                               input="y\n")
        assert result.exit_code == 0, result.output
        platform.job_stop.assert_not_called()
        platform.job_cancel.assert_not_called()
        platform.run_config.assert_called_once()

    def test_overwrite_refreshes_status_before_deciding(self, runner, project_dir, tmp_path,
                                                        monkeypatch, platform):
        monkeypatch.chdir(project_dir)
        _set_tracker(project_dir, {"real-uuid-123": {"name": "test-job", "status": "Running"}})
        platform.get_job_status.return_value = "Succeed"
        result = runner.invoke(cli, ["submit", _write(tmp_path, JOB_CONFIG), "-o", "-s", "Running"])
        platform.get_job_status.assert_called_once_with("real-uuid-123")
        assert "Skipped" in result.output
        platform.modify_configs.assert_not_called()

    def test_config_count_warning_aborts(self, runner, project_dir, tmp_path, monkeypatch,
                                         platform):
        monkeypatch.chdir(project_dir)
        platform.get_experiment.return_value = {"experiment_id": "eid-123",
                                                "advance_config_infos": [{}] * 150}
        result = runner.invoke(cli, ["submit", _write(tmp_path, JOB_CONFIG)], input="n\n")
        assert "Warning" in result.output and "150" in result.output
        assert "Aborted" in result.output
        platform.modify_configs.assert_not_called()

    def test_config_count_warning_continues(self, runner, project_dir, tmp_path, monkeypatch,
                                            platform):
        monkeypatch.chdir(project_dir)
        platform.get_experiment.return_value = {"experiment_id": "eid-123",
                                                "advance_config_infos": [{}] * 100}
        result = runner.invoke(cli, ["submit", _write(tmp_path, JOB_CONFIG)], input="y\n")
        assert "Warning" in result.output
        platform.modify_configs.assert_called_once()


class TestSubmitWithSpec:
    def test_submit_auto_creates_experiment(self, runner, project_dir, tmp_path, monkeypatch,
                                            platform):
        monkeypatch.chdir(project_dir)
        platform.find_experiment.return_value = None
        platform.get_experiment.return_value = {"experiment_id": "created-eid",
                                                "advance_config_infos": [{}]}
        platform.api.create_experiment.return_value = "created-eid"

        result = runner.invoke(cli, ["submit", _write(tmp_path, SPEC_CONFIG), "-y"])

        assert result.exit_code == 0, result.output
        platform.api.create_experiment.assert_called_once()
        kwargs = platform.modify_configs.call_args.kwargs
        assert kwargs["spec"].name == "spec-exp"
        location_key = "QUEUE_CATEGORY_DEDICATED|||jiuding_airs1_h100||"
        assert kwargs["queue_locations"][location_key]["queue_id"] == "qid-1"
        assert platform.modify_configs.call_args.args[1] == "created-eid"
        assert platform.run_config.call_args.args == ("created-eid", "spec-job")

    def test_submit_uses_existing_experiment(self, runner, project_dir, tmp_path, monkeypatch,
                                             platform):
        monkeypatch.chdir(project_dir)
        result = runner.invoke(cli, ["submit", _write(tmp_path, SPEC_CONFIG), "-y"])
        assert result.exit_code == 0, result.output
        platform.api.create_experiment.assert_not_called()
        assert platform.modify_configs.call_args.args[1] == "eid-123"


class TestSubmitBatchFlow:
    SEARCH = EXP + """
search:
  name: batch
  job_template:
    name: trial-{x}
    command: echo {x}
  params:
    - name: x
      values: [one, two, three]
"""

    def test_grid_uses_one_modify_and_one_run_per_job(self, runner, project_dir, tmp_path,
                                                      monkeypatch, platform):
        monkeypatch.chdir(project_dir)
        platform.run_config.side_effect = ["id-one", "id-two", "id-three"]
        result = runner.invoke(cli, ["submit", _write(tmp_path, self.SEARCH)])
        assert result.exit_code == 0, result.output
        platform.modify_configs.assert_called_once()
        assert [c.args[:2] for c in platform.run_config.call_args_list] == [
            ("eid-123", "trial-one"), ("eid-123", "trial-two"), ("eid-123", "trial-three"),
        ]

    def test_grid_overwrite_stop_failure_skips_that_job(self, runner, project_dir, tmp_path,
                                                        monkeypatch, platform):
        monkeypatch.chdir(project_dir)
        _set_tracker(project_dir, {"old-one": {"name": "trial-one", "status": "Running"}})
        platform.get_job_status.return_value = "Running"
        platform.job_stop.side_effect = PlatformError("job stop", 1, "denied", "")
        platform.run_config.side_effect = ["id-two", "id-three"]

        result = runner.invoke(cli, ["submit", _write(tmp_path, self.SEARCH), "-o"], input="y\n")

        assert result.exit_code == 0, result.output
        assert "could not stop old job" in result.output
        names = [j.name for j in platform.modify_configs.call_args.args[0]]
        assert names == ["trial-two", "trial-three"]

    def test_modify_failure_does_not_run_or_change_tracker(self, runner, project_dir, tmp_path,
                                                           monkeypatch, platform):
        monkeypatch.chdir(project_dir)
        platform.modify_configs.side_effect = PlatformError(
            "experiment modify", 1, "modify failed", "")
        result = runner.invoke(cli, ["submit", _write(tmp_path, JOB_CONFIG)])
        assert result.exit_code == 1
        assert "modify failed" in result.output
        platform.run_config.assert_not_called()
        tracker = json.loads((project_dir / ".jrun" / "jobs.json").read_text())
        assert tracker == {"searches": {}, "jobs": {}}


AUTO_SEARCH = EXP + """
search:
  name: auto
  job_template:
    name: auto-{x}
    command: echo {x}
    resource_config:
      queue_name: auto
      queue_category: [public, idle]
      accelerator_model: [NVIDIA_A100-SXM4-40GB, NVIDIA_V100-PCIE-32GB]
      accelerator_count: [8, 8]
    run_options: {duration_hours: 1, usage_desc: t, queue_priority_mode: normal}
  params:
    - name: x
      values: [a, b, c]
"""


def _auto_api():
    return FakeAPI(shared=[shared_row("A", A100, 8, 0), shared_row("V", V100, 8, 0)],
                   budgets={"QUEUE_CATEGORY_PUBLIC": 2, "QUEUE_CATEGORY_IDLE": 0})


class TestAutoPlacement:
    def test_dry_run_prints_placement(self, runner, project_dir, tmp_path, monkeypatch):
        monkeypatch.chdir(project_dir)
        with patch("jrun.commands.submit.PlatformAPI", return_value=_auto_api()):
            result = runner.invoke(cli, ["submit", _write(tmp_path, AUTO_SEARCH), "--dry-run"])
        assert result.exit_code == 0, result.output
        assert "Auto placement: 2 start now, 0 queued, 1 wait" in result.output
        assert (f"auto-a: public A (北京-大兴/可用区A) {A100} x8 - free cards, starts now"
                in result.output)
        assert f"auto-b: public V (北京-大兴/可用区A) {V100} x8" in result.output
        assert "auto-c: public A" in result.output and "waits for a public/idle slot" in result.output
        assert "JRUN_ACCELERATOR_COUNT" in result.output

    def test_dry_run_without_credentials_still_previews(self, runner, project_dir, tmp_path,
                                                        monkeypatch):
        monkeypatch.chdir(project_dir)
        api = MagicMock()
        api.resolve_project_context.side_effect = ApiError("/token", None, "no credentials")
        with patch("jrun.commands.submit.PlatformAPI", return_value=api):
            result = runner.invoke(cli, ["submit", _write(tmp_path, AUTO_SEARCH), "--dry-run"])
        assert result.exit_code == 0, result.output
        assert "cannot read queue capacity" in result.output
        assert f"Queue: auto (public/idle; {A100} x8, {V100} x8)" in result.output

    def test_submit_places_only_jobs_being_submitted(self, runner, project_dir, tmp_path,
                                                     monkeypatch, platform):
        monkeypatch.chdir(project_dir)
        _set_tracker(project_dir, {"old-a": {"name": "auto-a", "status": "Succeed"}})
        platform.run_config.side_effect = ["id-b", "id-c"]
        platform.api = _auto_api()
        with patch("jrun.commands.submit.PlatformAPI", return_value=platform.api), \
                patch("jrun.submission.Scheduler"):
            result = runner.invoke(cli, ["submit", _write(tmp_path, AUTO_SEARCH)], input="s\n")
        assert result.exit_code == 0, result.output
        # auto-a is skipped, so b and c get both public slots and both pools.
        assert "Auto placement: 2 start now, 0 queued, 0 wait" in result.output
        installed = platform.modify_configs.call_args.args[0]
        assert [(j.name, j.resource_config.queue_name) for j in installed] == [
            ("auto-b", "A"), ("auto-c", "V")]
        assert installed[1].envs["JRUN_ACCELERATOR_MODEL"] == V100

    def test_single_job_without_slot_fails_cleanly(self, runner, project_dir, tmp_path,
                                                   monkeypatch, platform):
        monkeypatch.chdir(project_dir)
        config = EXP + """
job:
  name: solo
  command: c
  resource_config:
    queue_name: auto
    queue_category: idle
    accelerator_model: NVIDIA_A100-SXM4-40GB
    accelerator_count: 1
"""
        with patch("jrun.commands.submit.PlatformAPI", return_value=_auto_api()):
            result = runner.invoke(cli, ["submit", _write(tmp_path, config)])
        assert result.exit_code == 1
        assert "no public/idle submit slot" in result.output
        platform.modify_configs.assert_not_called()
