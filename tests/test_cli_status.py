import json
from unittest.mock import MagicMock, patch

from jrun.cli import cli


PLATFORM_CONTEXT = {
    "projId": "p1",
    "projsetId": "ps1",
    "userId": "u1",
    "projsetName": "baai-safety",
    "projectName": "baai-safety_research",
    "clusterName": "dx-calc1",
    "zoneName": "dx-calc1-zonea",
}


class TestStatus:
    def test_status_empty(self, runner, project_dir, monkeypatch):
        monkeypatch.chdir(project_dir)
        result = runner.invoke(cli, ["job", "status"])
        assert result.exit_code == 0

    def test_top_level_status_removed(self, runner):
        result = runner.invoke(cli, ["status"])
        assert result.exit_code != 0

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
        result = runner.invoke(cli, ["job", "status"])
        assert result.exit_code == 0
        assert "my-job" in result.output

    @patch("jrun.commands.status.PlatformClient")
    def test_status_skips_refresh_for_terminal_jobs(self, mock_platform_cls, runner, project_dir, monkeypatch):
        monkeypatch.chdir(project_dir)
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
                       "platform": PLATFORM_CONTEXT},
                "j2": {"name": "failed-job", "search_name": "my-search",
                       "params": {}, "status": "Failed",
                       "submitted_at": "2024-01-01T00:01:00",
                       "platform": PLATFORM_CONTEXT},
                "j3": {"name": "active-job", "search_name": "my-search",
                       "params": {}, "status": "Running",
                       "submitted_at": "2024-01-01T00:02:00"},
            },
        }
        (project_dir / ".jrun" / "jobs.json").write_text(json.dumps(tracker))
        mock_client = MagicMock()
        mock_client.get_job_info.return_value = {"status": "Running"}
        mock_platform_cls.return_value = mock_client
        result = runner.invoke(cli, ["job", "status"])
        assert result.exit_code == 0
        # Only the active job (j3) should trigger a platform call
        assert mock_client.get_job_info.call_count == 1
        mock_client.get_job_info.assert_called_once_with("j3")

    @patch("jrun.commands.status.PlatformClient")
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
        result = runner.invoke(cli, ["job", "status"])
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
