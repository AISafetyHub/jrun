import json
from unittest.mock import MagicMock, patch

from jrun.cli import cli
from tests.conftest import SAMPLE_QUEUE, SPEC_CONFIG


class TestSubmitDryRun:
    def test_dry_run_single_job(self, runner, project_dir, tmp_path, monkeypatch):
        monkeypatch.chdir(project_dir)
        config = tmp_path / "single.yaml"
        config.write_text("""
job:
  name: test-job
  command: echo hello
""")
        result = runner.invoke(cli, ["submit", str(config), "--dry-run"])
        assert result.exit_code == 0
        assert "test-job" in result.output

    def test_dry_run_search(self, runner, project_dir, tmp_path, monkeypatch):
        monkeypatch.chdir(project_dir)
        config = tmp_path / "search.yaml"
        config.write_text("""
search:
  job_template:
    name: job-{lr}
    command: train.py --lr {lr}
  params:
    - name: lr
      values: [0.01, 0.001]
""")
        result = runner.invoke(cli, ["submit", str(config), "--dry-run"])
        assert result.exit_code == 0
        assert "job-0.01" in result.output
        assert "job-0.001" in result.output


class TestSubmitOverwrite:
    @patch("jrun.commands.submit.PlatformClient")
    def test_overwrite_queued_job_does_not_cancel_old_job(self, mock_platform_cls, runner, project_dir, tmp_path, monkeypatch):
        monkeypatch.chdir(project_dir)
        # Set up existing queued job in tracker
        tracker = {
            "searches": {},
            "jobs": {
                "queued-abc123": {
                    "name": "test-job",
                    "search_name": None,
                    "params": {},
                    "status": "Queued",
                    "submitted_at": "2024-01-01T00:00:00",
                },
            },
        }
        (project_dir / ".jrun" / "jobs.json").write_text(json.dumps(tracker))

        # Create config for the same job
        config = tmp_path / "job.yaml"
        config.write_text("""
job:
  name: test-job
  command: echo hello
""")

        mock_client = MagicMock()
        mock_client.count_experiment_configs.return_value = 0
        mock_client.modify_configs.return_value = ["test-job"]
        mock_client.run_config.return_value = "abc-456"
        mock_platform_cls.return_value = mock_client

        # Submit with overwrite
        result = runner.invoke(cli, ["submit", str(config), "-o", "-s", "Queued"], input="y\n")

        # Should not call job_stop or job_cancel for queued- prefixed jobs
        mock_client.job_stop.assert_not_called()
        mock_client.job_cancel.assert_not_called()
        mock_client.modify_configs.assert_called_once()
        mock_client.run_config.assert_called_once_with("eid-123", "test-job")

    @patch("jrun.commands.submit.PlatformClient")
    def test_overwrite_refreshes_status_before_deciding(self, mock_platform_cls, runner, project_dir, tmp_path, monkeypatch):
        monkeypatch.chdir(project_dir)
        # Set up existing job with stale "Running" status
        tracker = {
            "searches": {},
            "jobs": {
                "real-uuid-123": {
                    "name": "test-job",
                    "search_name": None,
                    "params": {},
                    "status": "Running",
                    "submitted_at": "2024-01-01T00:00:00",
                },
            },
        }
        (project_dir / ".jrun" / "jobs.json").write_text(json.dumps(tracker))

        config = tmp_path / "job.yaml"
        config.write_text("""
job:
  name: test-job
  command: echo hello
""")

        mock_client = MagicMock()
        # Platform reports the job has actually succeeded
        mock_client.count_experiment_configs.return_value = 0
        mock_client.get_job_status.return_value = "Succeed"
        mock_platform_cls.return_value = mock_client

        # Try to overwrite with -s Running filter
        result = runner.invoke(cli, ["submit", str(config), "-o", "-s", "Running"])

        # Should have refreshed status and found it's Succeed, so skip overwrite
        mock_client.get_job_status.assert_called_once_with("real-uuid-123")
        assert "Skipped" in result.output or "status not in" in result.output.lower()


class TestExperimentIdFromConfig:
    @patch("jrun.commands.submit.PlatformClient")
    def test_config_experiment_id_used_over_project_default(self, mock_platform_cls, runner, project_dir, tmp_path, monkeypatch):
        monkeypatch.chdir(project_dir)
        config = tmp_path / "job.yaml"
        config.write_text("""
job:
  name: test-job
  command: echo hello
  experiment_id: "config-exp-id"
  experiment_name: "config-exp-name"
""")
        mock_client = MagicMock()
        mock_client.count_experiment_configs.return_value = 5
        mock_client.modify_configs.return_value = ["test-job"]
        mock_client.run_config.return_value = "uuid-1234-5678"
        mock_client.get_job_status.return_value = "Submitted"
        mock_platform_cls.return_value = mock_client

        result = runner.invoke(cli, ["submit", str(config)])
        assert result.exit_code == 0
        mock_client.modify_configs.assert_called_once()
        mock_client.run_config.assert_called_once_with("config-exp-id", "test-job")
        assert mock_client.modify_configs.call_args.args[1] == "config-exp-id"

    @patch("jrun.commands.submit.PlatformClient")
    def test_submit_persists_job_platform(self, mock_platform_cls, runner, project_dir, tmp_path, monkeypatch):
        monkeypatch.chdir(project_dir)
        config = tmp_path / "job.yaml"
        config.write_text("""
job:
  name: test-job
  command: echo hello
""")
        mock_client = MagicMock()
        mock_client.count_experiment_configs.return_value = 0
        mock_client.modify_configs.return_value = ["test-job"]
        mock_client.run_config.return_value = "uuid-1234-5678"
        mock_client.get_job_info.return_value = {
            "status": "Succeed",
            "projset_id": "ps1",
            "proj_id": "p1",
            "creator_id": 319832320808853520,
            "projset_name": "baai-safety",
            "proj_name": "baai-safety_research",
            "cluster_name": "dx-calc1",
            "zone_name": "dx-calc1-zonea",
        }
        mock_platform_cls.return_value = mock_client
        result = runner.invoke(cli, ["submit", str(config)])
        assert result.exit_code == 0
        stored = json.loads((project_dir / ".jrun" / "jobs.json").read_text())
        job = stored["jobs"]["uuid-1234-5678"]
        assert job["status"] == "Succeed"
        assert job["platform"]["clusterName"] == "dx-calc1"
        assert job["platform"]["projectName"] == "baai-safety_research"
        assert "platform-multi.baai.ac.cn" in result.output

    @patch("jrun.commands.submit.PlatformClient")
    def test_config_count_warning_aborts(self, mock_platform_cls, runner, project_dir, tmp_path, monkeypatch):
        monkeypatch.chdir(project_dir)
        config = tmp_path / "job.yaml"
        config.write_text("""
job:
  name: test-job
  command: echo hello
""")
        mock_client = MagicMock()
        mock_client.count_experiment_configs.return_value = 150
        mock_platform_cls.return_value = mock_client

        result = runner.invoke(cli, ["submit", str(config)], input="n\n")
        assert "Warning" in result.output
        assert "150" in result.output
        assert "Aborted" in result.output
        mock_client.modify_configs.assert_not_called()
        mock_client.run_config.assert_not_called()

    @patch("jrun.commands.submit.PlatformClient")
    def test_config_count_warning_continues(self, mock_platform_cls, runner, project_dir, tmp_path, monkeypatch):
        monkeypatch.chdir(project_dir)
        config = tmp_path / "job.yaml"
        config.write_text("""
job:
  name: test-job
  command: echo hello
""")
        mock_client = MagicMock()
        mock_client.count_experiment_configs.return_value = 100
        mock_client.modify_configs.return_value = ["test-job"]
        mock_client.run_config.return_value = "uuid-1234-5678"
        mock_client.get_job_status.return_value = "Submitted"
        mock_platform_cls.return_value = mock_client

        result = runner.invoke(cli, ["submit", str(config)], input="y\n")
        assert "Warning" in result.output
        mock_client.modify_configs.assert_called_once()
        mock_client.run_config.assert_called_once_with("eid-123", "test-job")


class TestSubmitWithSpec:
    @patch("jrun.commands.submit.PlatformAPI")
    @patch("jrun.commands.submit.PlatformClient")
    def test_submit_auto_creates_experiment(self, mock_platform_cls, mock_api_cls,
                                            runner, project_dir, tmp_path, monkeypatch):
        monkeypatch.chdir(project_dir)
        config = tmp_path / "job.yaml"
        config.write_text(SPEC_CONFIG)

        mock_platform = MagicMock()
        mock_platform.experiment_list.return_value = MagicMock(returncode=1, stdout="", stderr="")
        mock_platform.count_experiment_configs.return_value = 0
        mock_platform.modify_configs.return_value = ["spec-job"]
        mock_platform.run_config.return_value = "uuid-1234"
        mock_platform.get_job_status.return_value = "Submitted"
        mock_platform_cls.return_value = mock_platform
        mock_api = MagicMock()
        mock_api.find_queue.return_value = SAMPLE_QUEUE
        mock_api.create_experiment.return_value = "created-eid"
        mock_api_cls.return_value = mock_api

        result = runner.invoke(cli, ["submit", str(config), "-y"])

        assert result.exit_code == 0, result.output
        mock_api.create_experiment.assert_called_once()
        kwargs = mock_platform.modify_configs.call_args[1]
        assert kwargs["spec"].name == "spec-exp"
        assert kwargs["queue_locations"]["jiuding_airs1_h100"]["queue_id"] == "qid-1"
        # created experiment ID is what the config is modified/run under
        assert mock_platform.modify_configs.call_args[0][1] == "created-eid"
        mock_platform.run_config.assert_called_once_with("created-eid", "spec-job")

    @patch("jrun.commands.submit.PlatformAPI")
    @patch("jrun.commands.submit.PlatformClient")
    def test_submit_uses_existing_experiment(self, mock_platform_cls, mock_api_cls,
                                             runner, project_dir, tmp_path, monkeypatch):
        monkeypatch.chdir(project_dir)
        config = tmp_path / "job.yaml"
        config.write_text(SPEC_CONFIG)

        mock_platform = MagicMock()
        mock_platform.experiment_list.return_value = MagicMock(
            returncode=0, stdout=json.dumps({"experiment_id": "existing-eid"}), stderr=""
        )
        mock_platform.count_experiment_configs.return_value = 0
        mock_platform.modify_configs.return_value = ["spec-job"]
        mock_platform.run_config.return_value = "uuid-1234"
        mock_platform.get_job_status.return_value = "Submitted"
        mock_platform_cls.return_value = mock_platform
        mock_api = MagicMock()
        mock_api.find_queue.return_value = SAMPLE_QUEUE
        mock_api_cls.return_value = mock_api

        result = runner.invoke(cli, ["submit", str(config), "-y"])

        assert result.exit_code == 0, result.output
        mock_api.create_experiment.assert_not_called()
        assert mock_platform.modify_configs.call_args[0][1] == "existing-eid"
        mock_platform.run_config.assert_called_once_with("existing-eid", "spec-job")

    @patch("jrun.commands.submit.PlatformAPI")
    def test_dry_run_does_not_touch_api(self, mock_api_cls, runner, project_dir,
                                        tmp_path, monkeypatch):
        monkeypatch.chdir(project_dir)
        config = tmp_path / "job.yaml"
        config.write_text(SPEC_CONFIG)
        result = runner.invoke(cli, ["submit", str(config), "--dry-run"])
        assert result.exit_code == 0
        assert "will be created if missing" in result.output
        mock_api_cls.assert_not_called()

    def test_no_experiment_anywhere_errors(self, runner, tmp_path, monkeypatch):
        # Project with empty settings (new-style init) and no experiment in config
        jrun_dir = tmp_path / ".jrun"
        jrun_dir.mkdir()
        (jrun_dir / "settings.json").write_text("{}")
        monkeypatch.chdir(tmp_path)
        config = tmp_path / "job.yaml"
        config.write_text("job:\n  name: j\n  command: echo hi\n")
        result = runner.invoke(cli, ["submit", str(config)])
        assert result.exit_code == 1
        assert "no experiment" in result.output


class TestSubmitBatchFlow:
    @patch("jrun.commands.submit.PlatformClient")
    def test_grid_uses_one_modify_and_one_run_per_job(
        self, mock_platform_cls, runner, project_dir, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(project_dir)
        config = tmp_path / "search.yaml"
        config.write_text("""
search:
  name: batch
  job_template:
    name: trial-{x}
    command: echo {x}
  params:
    - name: x
      values: [one, two, three]
""")
        platform = MagicMock()
        platform.count_experiment_configs.return_value = 0
        platform.modify_configs.return_value = ["trial-one", "trial-two", "trial-three"]
        platform.run_config.side_effect = ["id-one", "id-two", "id-three"]
        mock_platform_cls.return_value = platform

        result = runner.invoke(cli, ["submit", str(config)])

        assert result.exit_code == 0, result.output
        platform.modify_configs.assert_called_once()
        assert platform.run_config.call_args_list == [
            (("eid-123", "trial-one"),),
            (("eid-123", "trial-two"),),
            (("eid-123", "trial-three"),),
        ]
        platform.submit_job.assert_not_called()

    @patch("jrun.commands.submit.PlatformClient")
    def test_modify_failure_does_not_run_or_change_tracker(
        self, mock_platform_cls, runner, project_dir, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(project_dir)
        config = tmp_path / "job.yaml"
        config.write_text("""
job:
  name: failing-modify
  command: echo hi
""")
        platform = MagicMock()
        platform.count_experiment_configs.return_value = 0
        from jrun.errors import PlatformError
        platform.modify_configs.side_effect = PlatformError(
            "experiment modify", 1, "modify failed", ""
        )
        mock_platform_cls.return_value = platform

        result = runner.invoke(cli, ["submit", str(config)])

        assert result.exit_code == 1
        assert "modify failed" in result.output
        platform.run_config.assert_not_called()
        tracker = json.loads((project_dir / ".jrun" / "jobs.json").read_text())
        assert tracker == {"searches": {}, "jobs": {}}
