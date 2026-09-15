import json
from unittest.mock import MagicMock, patch

from jrun.cli import cli
from tests.conftest import SAMPLE_QUEUE, SPEC_CONFIG


class TestExperimentCreate:
    @patch("jrun.commands.experiment.PlatformAPI")
    @patch("jrun.commands.experiment.PlatformClient")
    def test_creates_when_missing(self, mock_platform_cls, mock_api_cls,
                                  runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        config = tmp_path / "job.yaml"
        config.write_text(SPEC_CONFIG)

        mock_platform = MagicMock()
        mock_platform.experiment_list.return_value = MagicMock(returncode=1, stdout="", stderr="")
        mock_platform_cls.return_value = mock_platform
        mock_api = MagicMock()
        mock_api.find_queue.return_value = SAMPLE_QUEUE
        mock_api.create_experiment.return_value = "new-eid"
        mock_api_cls.return_value = mock_api

        result = runner.invoke(cli, ["experiment", "create", str(config), "-y"])

        assert result.exit_code == 0
        assert "new-eid" in result.output
        kwargs = mock_api.create_experiment.call_args[1]
        assert kwargs["name"] == "spec-exp"
        assert kwargs["image"] == "harbor.x/pytorch:23.08"
        assert kwargs["proj_id"] == "p1"

    @patch("jrun.commands.experiment.PlatformAPI")
    @patch("jrun.commands.experiment.PlatformClient")
    def test_skips_create_when_exists(self, mock_platform_cls, mock_api_cls,
                                      runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        config = tmp_path / "job.yaml"
        config.write_text(SPEC_CONFIG)

        mock_platform = MagicMock()
        mock_platform.experiment_list.return_value = MagicMock(
            returncode=0, stdout=json.dumps({"experiment_id": "existing-eid"}), stderr=""
        )
        mock_platform_cls.return_value = mock_platform
        mock_api = MagicMock()
        mock_api.find_queue.return_value = SAMPLE_QUEUE
        mock_api_cls.return_value = mock_api

        result = runner.invoke(cli, ["experiment", "create", str(config), "-y"])

        assert result.exit_code == 0
        assert "existing-eid" in result.output
        mock_api.create_experiment.assert_not_called()

    @patch("jrun.commands.experiment.PlatformAPI")
    @patch("jrun.commands.experiment.PlatformClient")
    def test_unknown_queue_lists_available(self, mock_platform_cls, mock_api_cls,
                                           runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        config = tmp_path / "job.yaml"
        config.write_text(SPEC_CONFIG)
        mock_api = MagicMock()
        mock_api.find_queue.return_value = None
        mock_api.list_queues.return_value = [SAMPLE_QUEUE]
        mock_api_cls.return_value = mock_api

        result = runner.invoke(cli, ["experiment", "create", str(config), "-y"])

        assert result.exit_code == 1
        assert "not found" in result.output
        assert "jiuding_airs1_h100" in result.output  # shows actual available queue

    def test_missing_experiment_section(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        config = tmp_path / "job.yaml"
        config.write_text("job:\n  name: j\n  command: echo hi\n")
        result = runner.invoke(cli, ["experiment", "create", str(config)])
        assert result.exit_code == 1
        assert "no 'experiment' section" in result.output


class TestExperimentList:
    @patch("jrun.commands.experiment.PlatformClient")
    def test_lists_experiments(self, mock_platform_cls, runner):
        mock_platform = MagicMock()
        mock_platform.experiment_list.return_value = MagicMock(
            returncode=0, stdout="exp-a\nexp-b", stderr=""
        )
        mock_platform_cls.return_value = mock_platform

        result = runner.invoke(cli, ["experiment", "list"])

        assert result.exit_code == 0
        assert "exp-a" in result.output
        assert "exp-b" in result.output

    @patch("jrun.commands.experiment.PlatformClient")
    def test_top_level_list_removed(self, mock_platform_cls, runner):
        result = runner.invoke(cli, ["list"])
        assert result.exit_code != 0


class TestExperimentJobs:
    EXP = {"experiment_id": "eid-1", "experiment_name": "my-exp"}
    JOBS = [
        {"id": "j-1", "configName": "train-lr0.01", "status": "Running",
         "createdTime": "1788362902921", "queueName": "q1"},
        {"id": "j-2", "configName": "train-lr0.1", "status": "Failed",
         "createdTime": "1788362902920", "queueName": "q1"},
    ]

    @patch("jrun.commands.experiment.PlatformAPI")
    @patch("jrun.commands.experiment.PlatformClient")
    def test_lists_jobs(self, mock_platform_cls, mock_api_cls, runner,
                        tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        mock_platform_cls.return_value.find_experiment.return_value = self.EXP
        mock_api_cls.return_value.experiment_jobs_all.return_value = self.JOBS

        result = runner.invoke(cli, ["experiment", "jobs", "my-exp"])

        assert result.exit_code == 0
        assert "my-exp" in result.output
        assert "train-lr0.01" in result.output
        assert "train-lr0.1" in result.output
        assert "j-1" in result.output
        mock_api_cls.return_value.experiment_jobs_all.assert_called_once_with(
            "eid-1", proj_id=None, projset_id=None)

    @patch("jrun.commands.experiment.PlatformAPI")
    @patch("jrun.commands.experiment.PlatformClient")
    def test_links_when_project_has_platform_ids(self, mock_platform_cls,
                                                 mock_api_cls, runner,
                                                 tmp_path, monkeypatch):
        import json as _json
        jrun_dir = tmp_path / ".jrun"
        jrun_dir.mkdir()
        (jrun_dir / "settings.json").write_text(_json.dumps({
            "platform": {"projId": "p1", "projsetId": "ps1", "userId": "u1"},
        }))
        monkeypatch.chdir(tmp_path)
        mock_platform_cls.return_value.find_experiment.return_value = self.EXP
        mock_api_cls.return_value.experiment_jobs_all.return_value = self.JOBS

        result = runner.invoke(cli, ["experiment", "jobs", "my-exp"])

        assert result.exit_code == 0
        assert "LINK" in result.output
        # hyperlink targets the platform job-detail page with the job id
        assert "id=j-1" in result.output
        assert "projId=p1" in result.output

    @patch("jrun.commands.experiment.PlatformAPI")
    @patch("jrun.commands.experiment.PlatformClient")
    def test_status_filter(self, mock_platform_cls, mock_api_cls, runner,
                           tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        mock_platform_cls.return_value.find_experiment.return_value = self.EXP
        mock_api_cls.return_value.experiment_jobs_all.return_value = self.JOBS

        result = runner.invoke(cli, ["experiment", "jobs", "my-exp", "-s", "Running"])

        assert result.exit_code == 0
        assert "train-lr0.01" in result.output
        assert "train-lr0.1" not in result.output

    @patch("jrun.commands.experiment.PlatformAPI")
    @patch("jrun.commands.experiment.PlatformClient")
    def test_empty_result(self, mock_platform_cls, mock_api_cls, runner,
                          tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        mock_platform_cls.return_value.find_experiment.return_value = self.EXP
        mock_api_cls.return_value.experiment_jobs_all.return_value = []

        result = runner.invoke(cli, ["experiment", "jobs", "my-exp"])

        assert result.exit_code == 0
        assert "No matching jobs" in result.output

    @patch("jrun.commands.experiment.PlatformClient")
    def test_experiment_not_found(self, mock_platform_cls, runner,
                                  tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        mock_platform_cls.return_value.find_experiment.return_value = None

        result = runner.invoke(cli, ["experiment", "jobs", "missing"])

        assert result.exit_code == 1
        assert "not found" in result.output

    @patch("jrun.commands.experiment.PlatformAPI")
    @patch("jrun.commands.experiment.PlatformClient")
    def test_api_error(self, mock_platform_cls, mock_api_cls, runner,
                       tmp_path, monkeypatch):
        from jrun.errors import ApiError
        monkeypatch.chdir(tmp_path)
        mock_platform_cls.return_value.find_experiment.return_value = self.EXP
        mock_api_cls.return_value.experiment_jobs_all.side_effect = ApiError(
            "/api/v1/job/select", 403, "forbidden")

        result = runner.invoke(cli, ["experiment", "jobs", "my-exp"])

        assert result.exit_code == 1
        assert "forbidden" in result.output


class TestExperimentDelete:
    EXP = {"experiment_id": "eid-1", "experiment_name": "my-exp"}

    @patch("jrun.commands.experiment.PlatformAPI")
    @patch("jrun.commands.experiment.PlatformClient")
    def test_deletes_with_yes(self, mock_platform_cls, mock_api_cls, runner,
                              tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        mock_platform_cls.return_value.find_experiment.return_value = self.EXP

        result = runner.invoke(cli, ["experiment", "delete", "my-exp", "-y"])

        assert result.exit_code == 0
        assert "deleted" in result.output
        mock_api_cls.return_value.delete_experiment.assert_called_once_with(
            "eid-1", "my-exp", proj_id=None, projset_id=None)

    @patch("jrun.commands.experiment.PlatformAPI")
    @patch("jrun.commands.experiment.PlatformClient")
    def test_confirm_prompt_accepts(self, mock_platform_cls, mock_api_cls,
                                    runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        mock_platform_cls.return_value.find_experiment.return_value = self.EXP

        result = runner.invoke(cli, ["experiment", "delete", "my-exp"], input="y\n")

        assert result.exit_code == 0
        mock_api_cls.return_value.delete_experiment.assert_called_once()

    @patch("jrun.commands.experiment.PlatformAPI")
    @patch("jrun.commands.experiment.PlatformClient")
    def test_confirm_prompt_abort(self, mock_platform_cls, mock_api_cls,
                                  runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        mock_platform_cls.return_value.find_experiment.return_value = self.EXP

        result = runner.invoke(cli, ["experiment", "delete", "my-exp"], input="n\n")

        assert result.exit_code == 1
        mock_api_cls.return_value.delete_experiment.assert_not_called()

    @patch("jrun.commands.experiment.PlatformClient")
    def test_experiment_not_found(self, mock_platform_cls, runner,
                                  tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        mock_platform_cls.return_value.find_experiment.return_value = None

        result = runner.invoke(cli, ["experiment", "delete", "missing", "-y"])

        assert result.exit_code == 1
        assert "not found" in result.output

    @patch("jrun.commands.experiment.PlatformAPI")
    @patch("jrun.commands.experiment.PlatformClient")
    def test_api_error(self, mock_platform_cls, mock_api_cls, runner,
                       tmp_path, monkeypatch):
        from jrun.errors import ApiError
        monkeypatch.chdir(tmp_path)
        mock_platform_cls.return_value.find_experiment.return_value = self.EXP
        mock_api_cls.return_value.delete_experiment.side_effect = ApiError(
            "/api/v1/experiment/delete", 400, "bad request")

        result = runner.invoke(cli, ["experiment", "delete", "my-exp", "-y"])

        assert result.exit_code == 1
        assert "bad request" in result.output
