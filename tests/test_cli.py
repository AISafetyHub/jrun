import json
import pytest
from pathlib import Path
from click.testing import CliRunner
from unittest.mock import patch, MagicMock

from jrun.cli import cli


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def project_dir(tmp_path):
    jrun_dir = tmp_path / ".jrun"
    jrun_dir.mkdir()
    settings = {"experiment_name": "test-exp", "experiment_id": "eid-123"}
    (jrun_dir / "settings.json").write_text(json.dumps(settings))
    (jrun_dir / "jobs.json").write_text(json.dumps({"searches": {}, "jobs": {}}))
    return tmp_path


class TestTemplate:
    def test_template_stdout(self, runner):
        result = runner.invoke(cli, ["template"])
        assert result.exit_code == 0
        assert "search:" in result.output or "job:" in result.output

    def test_template_to_file(self, runner, tmp_path):
        out_file = str(tmp_path / "out.yaml")
        result = runner.invoke(cli, ["template", "-f", out_file])
        assert result.exit_code == 0
        assert Path(out_file).exists()


class TestInit:
    def test_init_creates_jrun_dir(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(cli, ["init", "-N", "my-exp"])
        assert result.exit_code == 0
        assert (tmp_path / ".jrun" / "settings.json").exists()

    @patch("jrun.cli.PlatformClient")
    def test_init_with_experiment_id(self, mock_platform_cls, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        mock_client = MagicMock()
        mock_client.experiment_list.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps({
                "experiment_name": "my-exp",
                "storage_info": [{
                    "user_id": 1,
                    "volumes_path": "/mnt/x/11111111-1111-1111-1111-111111111111_22222222-2222-2222-2222-222222222222/d",
                }],
            }),
            stderr="",
        )
        mock_platform_cls.return_value = mock_client
        result = runner.invoke(cli, ["init", "-N", "my-exp", "-e", "eid-1"])
        assert result.exit_code == 0

    @patch("jrun.cli.PlatformClient")
    def test_init_uses_canonical_name_for_experiment_id(
        self, mock_platform_cls, runner, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        mock_client = MagicMock()
        mock_client.experiment_list.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps({
                "experiment_id": "eid-1",
                "experiment_name": "canonical-exp",
            }),
            stderr="",
        )
        mock_platform_cls.return_value = mock_client

        result = runner.invoke(
            cli, ["init", "-N", "stale-exp", "-e", "eid-1"]
        )

        assert result.exit_code == 0
        settings = json.loads((tmp_path / ".jrun" / "settings.json").read_text())
        assert settings["experiment_name"] == "canonical-exp"
        assert "belongs to 'canonical-exp', not 'stale-exp'" in result.output


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
    @patch("jrun.cli.PlatformClient")
    def test_overwrite_queued_job_skips_platform_calls(self, mock_platform_cls, runner, project_dir, tmp_path, monkeypatch):
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
        mock_client.experiment_modify.return_value = MagicMock(returncode=0)
        mock_client.job_run.return_value = MagicMock(returncode=0, stdout="Job abc-456 submitted")
        mock_platform_cls.return_value = mock_client

        # Submit with overwrite
        result = runner.invoke(cli, ["submit", str(config), "-o", "-s", "Queued"], input="y\n")

        # Should not call job_stop or job_cancel for queued- prefixed jobs
        mock_client.job_stop.assert_not_called()
        mock_client.job_cancel.assert_not_called()

    @patch("jrun.cli.PlatformClient")
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


class TestRemove:
    @patch("jrun.cli.PlatformClient")
    def test_remove_single_job_only_refreshes_that_job(self, mock_platform_cls, runner, project_dir, monkeypatch):
        monkeypatch.chdir(project_dir)
        tracker = {
            "searches": {},
            "jobs": {
                "job-aaa": {"name": "target-job", "search_name": None,
                            "params": {}, "status": "Running",
                            "submitted_at": "2024-01-01T00:00:00"},
                "job-bbb": {"name": "other-job", "search_name": None,
                            "params": {}, "status": "Running",
                            "submitted_at": "2024-01-01T00:01:00"},
                "job-ccc": {"name": "third-job", "search_name": None,
                            "params": {}, "status": "Running",
                            "submitted_at": "2024-01-01T00:02:00"},
            },
        }
        (project_dir / ".jrun" / "jobs.json").write_text(json.dumps(tracker))
        mock_client = MagicMock()
        mock_client.get_job_status.return_value = "Running"
        mock_platform_cls.return_value = mock_client
        result = runner.invoke(cli, ["remove", "target-job"])
        assert result.exit_code == 0
        mock_client.get_job_status.assert_called_once_with("job-aaa")

    @patch("jrun.cli.PlatformClient")
    def test_remove_skips_refresh_for_terminal_job(self, mock_platform_cls, runner, project_dir, monkeypatch):
        monkeypatch.chdir(project_dir)
        tracker = {
            "searches": {},
            "jobs": {
                "job-aaa": {"name": "done-job", "search_name": None,
                            "params": {}, "status": "Succeed",
                            "submitted_at": "2024-01-01T00:00:00"},
                "job-bbb": {"name": "other-active", "search_name": None,
                            "params": {}, "status": "Running",
                            "submitted_at": "2024-01-01T00:01:00"},
            },
        }
        (project_dir / ".jrun" / "jobs.json").write_text(json.dumps(tracker))
        mock_client = MagicMock()
        mock_platform_cls.return_value = mock_client
        result = runner.invoke(cli, ["remove", "done-job"])
        assert result.exit_code == 0
        mock_client.get_job_status.assert_not_called()

    @patch("jrun.cli.PlatformClient")
    def test_remove_search_only_refreshes_search_jobs(self, mock_platform_cls, runner, project_dir, monkeypatch):
        monkeypatch.chdir(project_dir)
        tracker = {
            "searches": {
                "my-search": {
                    "config_file": "s.yaml",
                    "submitted_at": "2024-01-01T00:00:00",
                    "job_ids": ["job-aaa", "job-bbb"],
                }
            },
            "jobs": {
                "job-aaa": {"name": "search-job-1", "search_name": "my-search",
                            "params": {}, "status": "Running",
                            "submitted_at": "2024-01-01T00:00:00"},
                "job-bbb": {"name": "search-job-2", "search_name": "my-search",
                            "params": {}, "status": "Succeed",
                            "submitted_at": "2024-01-01T00:01:00"},
                "job-ccc": {"name": "unrelated-job", "search_name": None,
                            "params": {}, "status": "Running",
                            "submitted_at": "2024-01-01T00:02:00"},
            },
        }
        (project_dir / ".jrun" / "jobs.json").write_text(json.dumps(tracker))
        mock_client = MagicMock()
        mock_client.get_job_status.return_value = "Running"
        mock_platform_cls.return_value = mock_client
        result = runner.invoke(cli, ["remove", "my-search"])
        assert result.exit_code == 0
        mock_client.get_job_status.assert_called_once_with("job-aaa")


class TestStop:
    @patch("jrun.cli.PlatformClient")
    def test_stop_single_job_only_refreshes_that_job(self, mock_platform_cls, runner, project_dir, monkeypatch):
        monkeypatch.chdir(project_dir)
        tracker = {
            "searches": {},
            "jobs": {
                "job-aaa": {"name": "target-job", "search_name": None,
                            "params": {}, "status": "Running",
                            "submitted_at": "2024-01-01T00:00:00"},
                "job-bbb": {"name": "other-job", "search_name": None,
                            "params": {}, "status": "Running",
                            "submitted_at": "2024-01-01T00:01:00"},
            },
        }
        (project_dir / ".jrun" / "jobs.json").write_text(json.dumps(tracker))
        mock_client = MagicMock()
        mock_client.get_job_status.return_value = "Running"
        mock_platform_cls.return_value = mock_client
        result = runner.invoke(cli, ["stop", "target-job"])
        assert result.exit_code == 0
        mock_client.get_job_status.assert_called_once_with("job-aaa")

    @patch("jrun.cli.PlatformClient")
    def test_stop_with_status_filter_refreshes_all(self, mock_platform_cls, runner, project_dir, monkeypatch):
        monkeypatch.chdir(project_dir)
        tracker = {
            "searches": {},
            "jobs": {
                "job-aaa": {"name": "job-1", "search_name": None,
                            "params": {}, "status": "Running",
                            "submitted_at": "2024-01-01T00:00:00"},
                "job-bbb": {"name": "job-2", "search_name": None,
                            "params": {}, "status": "Running",
                            "submitted_at": "2024-01-01T00:01:00"},
            },
        }
        (project_dir / ".jrun" / "jobs.json").write_text(json.dumps(tracker))
        mock_client = MagicMock()
        mock_client.get_job_status.return_value = "Running"
        mock_platform_cls.return_value = mock_client
        result = runner.invoke(cli, ["stop", "-s", "Running"])
        assert result.exit_code == 0
        assert mock_client.get_job_status.call_count == 2


class TestStatus:
    def test_status_empty(self, runner, project_dir, monkeypatch):
        monkeypatch.chdir(project_dir)
        result = runner.invoke(cli, ["status"])
        assert result.exit_code == 0

    def test_status_with_jobs(self, runner, project_dir, monkeypatch):
        monkeypatch.chdir(project_dir)
        tracker = {
            "searches": {},
            "jobs": {
                "j1": {"name": "my-job", "search_name": None,
                       "params": {}, "status": "Running",
                       "submitted_at": "2024-01-01T00:00:00"},
            },
        }
        (project_dir / ".jrun" / "jobs.json").write_text(json.dumps(tracker))
        result = runner.invoke(cli, ["status"])
        assert result.exit_code == 0
        assert "my-job" in result.output

    @patch("jrun.cli.PlatformClient")
    def test_status_skips_refresh_for_terminal_jobs(self, mock_platform_cls, runner, project_dir, monkeypatch):
        monkeypatch.chdir(project_dir)
        platform = {
            "projId": "p1",
            "projsetId": "ps1",
            "userId": "u1",
            "projsetName": "baai-safety",
            "projectName": "baai-safety_research",
            "clusterName": "dx-calc1",
            "zoneName": "dx-calc1-zonea",
        }
        tracker = {
            "searches": {
                "my-search": {
                    "config_file": "search.yaml",
                    "submitted_at": "2024-01-01T00:00:00",
                    "job_ids": ["j1", "j2", "j3"],
                }
            },
            "jobs": {
                "j1": {"name": "done-job", "search_name": "my-search",
                       "params": {}, "status": "Succeed",
                       "submitted_at": "2024-01-01T00:00:00",
                       "platform": platform},
                "j2": {"name": "failed-job", "search_name": "my-search",
                       "params": {}, "status": "Failed",
                       "submitted_at": "2024-01-01T00:01:00",
                       "platform": platform},
                "j3": {"name": "active-job", "search_name": "my-search",
                       "params": {}, "status": "Running",
                       "submitted_at": "2024-01-01T00:02:00"},
            },
        }
        (project_dir / ".jrun" / "jobs.json").write_text(json.dumps(tracker))
        mock_client = MagicMock()
        mock_client.get_job_info.return_value = {"status": "Running"}
        mock_platform_cls.return_value = mock_client
        result = runner.invoke(cli, ["status"])
        assert result.exit_code == 0
        # Only the active job (j3) should trigger a platform call
        assert mock_client.get_job_info.call_count == 1
        mock_client.get_job_info.assert_called_once_with("j3")

    @patch("jrun.cli.PlatformClient")
    def test_status_backfills_terminal_job_platform(self, mock_platform_cls, runner, project_dir, monkeypatch):
        monkeypatch.chdir(project_dir)
        tracker = {
            "searches": {},
            "jobs": {
                "j1": {
                    "name": "done-job",
                    "search_name": None,
                    "params": {},
                    "status": "Succeed",
                    "submitted_at": "2026-07-29T14:25:56",
                    "experiment_name": "test",
                }
            },
        }
        (project_dir / ".jrun" / "jobs.json").write_text(json.dumps(tracker))
        mock_client = MagicMock()
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
        result = runner.invoke(cli, ["status"])
        assert result.exit_code == 0
        stored = json.loads((project_dir / ".jrun" / "jobs.json").read_text())
        context = stored["jobs"]["j1"]["platform"]
        assert stored["jobs"]["j1"]["status"] == "Succeed"
        assert context == {
            "projsetId": "ps1",
            "projId": "p1",
            "userId": "319832320808853520",
            "projsetName": "baai-safety",
            "projectName": "baai-safety_research",
            "clusterName": "dx-calc1",
            "zoneName": "dx-calc1-zonea",
        }
        assert "platform-multi.baai.ac.cn" in result.output


class TestExperimentIdFromConfig:
    @patch("jrun.cli.PlatformClient")
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
        mock_client.submit_job.return_value = "uuid-1234-5678"
        mock_client.get_job_status.return_value = "Submitted"
        mock_platform_cls.return_value = mock_client

        result = runner.invoke(cli, ["submit", str(config)])
        assert result.exit_code == 0
        mock_client.submit_job.assert_called_once()
        call_args = mock_client.submit_job.call_args
        assert call_args[0][1] == "config-exp-name"
        assert call_args[0][2] == "config-exp-id"

    @patch("jrun.cli.PlatformClient")
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
        mock_client.submit_job.return_value = "uuid-1234-5678"
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

    @patch("jrun.cli.PlatformClient")
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
        mock_client.submit_job.assert_not_called()

    @patch("jrun.cli.PlatformClient")
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
        mock_client.submit_job.return_value = "uuid-1234-5678"
        mock_client.get_job_status.return_value = "Submitted"
        mock_platform_cls.return_value = mock_client

        result = runner.invoke(cli, ["submit", str(config)], input="y\n")
        assert "Warning" in result.output
        mock_client.submit_job.assert_called_once()
