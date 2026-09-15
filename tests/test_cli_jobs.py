import json
from unittest.mock import MagicMock, patch

from jrun.cli import cli


def _write_tracker(project_dir, tracker):
    (project_dir / ".jrun" / "jobs.json").write_text(json.dumps(tracker))


class TestRemove:
    @patch("jrun.commands.jobs.PlatformClient")
    def test_remove_single_job_only_refreshes_that_job(self, mock_platform_cls, runner, project_dir, monkeypatch):
        monkeypatch.chdir(project_dir)
        _write_tracker(project_dir, {
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
        })
        mock_client = MagicMock()
        mock_client.get_job_status.return_value = "Running"
        mock_platform_cls.return_value = mock_client
        result = runner.invoke(cli, ["job", "remove", "target-job"])
        assert result.exit_code == 0
        mock_client.get_job_status.assert_called_once_with("job-aaa")

    @patch("jrun.commands.jobs.PlatformClient")
    def test_remove_skips_refresh_for_terminal_job(self, mock_platform_cls, runner, project_dir, monkeypatch):
        monkeypatch.chdir(project_dir)
        _write_tracker(project_dir, {
            "searches": {},
            "jobs": {
                "job-aaa": {"name": "done-job", "search_name": None,
                            "params": {}, "status": "Succeed",
                            "submitted_at": "2024-01-01T00:00:00"},
                "job-bbb": {"name": "other-active", "search_name": None,
                            "params": {}, "status": "Running",
                            "submitted_at": "2024-01-01T00:01:00"},
            },
        })
        mock_client = MagicMock()
        mock_platform_cls.return_value = mock_client
        result = runner.invoke(cli, ["job", "remove", "done-job"])
        assert result.exit_code == 0
        mock_client.get_job_status.assert_not_called()

    @patch("jrun.commands.jobs.PlatformClient")
    def test_remove_search_only_refreshes_search_jobs(self, mock_platform_cls, runner, project_dir, monkeypatch):
        monkeypatch.chdir(project_dir)
        _write_tracker(project_dir, {
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
        })
        mock_client = MagicMock()
        mock_client.get_job_status.return_value = "Running"
        mock_platform_cls.return_value = mock_client
        result = runner.invoke(cli, ["job", "remove", "my-search"])
        assert result.exit_code == 0
        mock_client.get_job_status.assert_called_once_with("job-aaa")


class TestStop:
    @patch("jrun.commands.jobs.PlatformClient")
    def test_stop_single_job_only_refreshes_that_job(self, mock_platform_cls, runner, project_dir, monkeypatch):
        monkeypatch.chdir(project_dir)
        _write_tracker(project_dir, {
            "searches": {},
            "jobs": {
                "job-aaa": {"name": "target-job", "search_name": None,
                            "params": {}, "status": "Running",
                            "submitted_at": "2024-01-01T00:00:00"},
                "job-bbb": {"name": "other-job", "search_name": None,
                            "params": {}, "status": "Running",
                            "submitted_at": "2024-01-01T00:01:00"},
            },
        })
        mock_client = MagicMock()
        mock_client.get_job_status.return_value = "Running"
        mock_platform_cls.return_value = mock_client
        result = runner.invoke(cli, ["job", "stop", "target-job"])
        assert result.exit_code == 0
        mock_client.get_job_status.assert_called_once_with("job-aaa")

    @patch("jrun.commands.jobs.PlatformClient")
    def test_stop_with_status_filter_refreshes_all(self, mock_platform_cls, runner, project_dir, monkeypatch):
        monkeypatch.chdir(project_dir)
        _write_tracker(project_dir, {
            "searches": {},
            "jobs": {
                "job-aaa": {"name": "job-1", "search_name": None,
                            "params": {}, "status": "Running",
                            "submitted_at": "2024-01-01T00:00:00"},
                "job-bbb": {"name": "job-2", "search_name": None,
                            "params": {}, "status": "Running",
                            "submitted_at": "2024-01-01T00:01:00"},
            },
        })
        mock_client = MagicMock()
        mock_client.get_job_status.return_value = "Running"
        mock_platform_cls.return_value = mock_client
        result = runner.invoke(cli, ["job", "stop", "-s", "Running"])
        assert result.exit_code == 0
        assert mock_client.get_job_status.call_count == 2
