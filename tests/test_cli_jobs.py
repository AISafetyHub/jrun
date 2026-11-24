import json
from unittest.mock import MagicMock, patch

from jrun.cli import cli


def _write_tracker(project_dir, tracker):
    (project_dir / ".jrun" / "jobs.json").write_text(json.dumps(tracker))


class TestRemove:
    @patch("jrun.commands.jobs.PlatformClient")
    def test_remove_running_job_stops_it_before_cancel(self, mock_platform_cls, runner,
                                                       project_dir, monkeypatch):
        # Regression: remove used to only `job cancel` (which airsctl refuses
        # for jobs that have not ended), so running jobs kept running while
        # they vanished from the tracker.
        monkeypatch.chdir(project_dir)
        monkeypatch.setattr("jrun.tracking.time.sleep", lambda s: None)
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
        # refresh -> Running, first poll after stop -> Stopping, then Stopped
        mock_client.get_job_status.side_effect = ["Running", "Stopping", "Stopped"]
        mock_platform_cls.return_value = mock_client
        result = runner.invoke(cli, ["job", "remove", "target-job"])
        assert result.exit_code == 0, result.output
        mock_client.job_stop.assert_called_once_with("job-aaa")
        mock_client.job_cancel.assert_called_once_with("job-aaa")
        assert all(c.args == ("job-aaa",) for c in mock_client.get_job_status.call_args_list)
        tracker = json.loads((project_dir / ".jrun" / "jobs.json").read_text())
        assert set(tracker["jobs"]) == {"job-bbb"}

    @patch("jrun.commands.jobs.PlatformClient")
    def test_remove_keeps_job_that_does_not_stop(self, mock_platform_cls, runner,
                                                 project_dir, monkeypatch):
        monkeypatch.chdir(project_dir)
        monkeypatch.setattr("jrun.tracking.time.sleep", lambda s: None)
        _write_tracker(project_dir, {
            "searches": {},
            "jobs": {"job-aaa": {"name": "stuck", "search_name": None, "params": {},
                                 "status": "Running", "experiment_id": "e1",
                                 "submitted_at": "2024-01-01T00:00:00"}},
        })
        mock_client = MagicMock()
        mock_client.get_job_status.return_value = "Running"
        mock_platform_cls.return_value = mock_client
        result = runner.invoke(cli, ["job", "remove", "stuck"])
        assert result.exit_code == 1
        assert "still Running" in result.output
        mock_client.job_cancel.assert_not_called()
        mock_client.remove_configs.assert_not_called()
        tracker = json.loads((project_dir / ".jrun" / "jobs.json").read_text())
        assert set(tracker["jobs"]) == {"job-aaa"}

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
        mock_client.job_stop.assert_not_called()
        mock_client.job_cancel.assert_called_once_with("job-aaa")

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
        monkeypatch.setattr("jrun.tracking.time.sleep", lambda s: None)
        mock_client = MagicMock()
        mock_client.get_job_status.side_effect = ["Running", "Cancelled"]
        mock_platform_cls.return_value = mock_client
        result = runner.invoke(cli, ["job", "remove", "my-search"])
        assert result.exit_code == 0, result.output
        # Only the search's active job is refreshed, stopped and polled.
        assert {c.args[0] for c in mock_client.get_job_status.call_args_list} == {"job-aaa"}
        mock_client.job_stop.assert_called_once_with("job-aaa")
        tracker = json.loads((project_dir / ".jrun" / "jobs.json").read_text())
        assert set(tracker["jobs"]) == {"job-ccc"}
        assert tracker["searches"] == {}

    @patch("jrun.commands.jobs.PlatformClient")
    def test_remove_search_kept_while_a_job_is_still_active(self, mock_platform_cls, runner,
                                                            project_dir, monkeypatch):
        monkeypatch.chdir(project_dir)
        monkeypatch.setattr("jrun.tracking.time.sleep", lambda s: None)
        _write_tracker(project_dir, {
            "searches": {"s": {"config_file": "s.yaml", "submitted_at": "t",
                               "experiment_id": "e1", "job_ids": ["u1", "u2"]}},
            "jobs": {
                "u1": {"name": "a", "search_name": "s", "status": "Running"},
                "u2": {"name": "b", "search_name": "s", "status": "Failed"},
            },
        })
        mock_client = MagicMock()
        mock_client.get_job_status.return_value = "Running"
        mock_platform_cls.return_value = mock_client
        result = runner.invoke(cli, ["job", "remove", "s"])
        assert result.exit_code == 0, result.output
        assert "kept for 1 still-active" in result.output
        mock_client.job_cancel.assert_called_once_with("u2")
        mock_client.remove_configs.assert_called_once_with("e1", {"b"})
        tracker = json.loads((project_dir / ".jrun" / "jobs.json").read_text())
        assert set(tracker["jobs"]) == {"u1"}
        assert tracker["searches"]["s"]["job_ids"] == ["u1"]


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


class TestRemoveSearch:
    TRACKER = {
        "searches": {"s": {"config_file": "s.yaml", "submitted_at": "t",
                           "experiment_id": "eid-s", "job_ids": ["u1", "u2", "queued-x"]}},
        "jobs": {
            "u1": {"name": "a", "search_name": "s", "status": "Failed", "experiment_id": "eid-s"},
            "u2": {"name": "b", "search_name": "s", "status": "Succeed"},
            "queued-x": {"name": "c", "search_name": "s", "status": "Queued"},
        },
    }

    @patch("jrun.commands.jobs.PlatformClient")
    def test_whole_search_removes_configs_and_tracker(self, mock_platform_cls, runner,
                                                      project_dir, monkeypatch):
        monkeypatch.chdir(project_dir)
        _write_tracker(project_dir, self.TRACKER)
        client = mock_platform_cls.return_value
        result = runner.invoke(cli, ["job", "remove", "s"])
        assert result.exit_code == 0, result.output
        assert {c.args[0] for c in client.job_cancel.call_args_list} == {"u1", "u2"}
        client.remove_configs.assert_called_once_with("eid-s", {"a", "b"})
        tracker = json.loads((project_dir / ".jrun" / "jobs.json").read_text())
        assert tracker == {"searches": {}, "jobs": {}}

    @patch("jrun.commands.jobs.PlatformClient")
    def test_status_filter_removes_only_matching(self, mock_platform_cls, runner,
                                                 project_dir, monkeypatch):
        monkeypatch.chdir(project_dir)
        _write_tracker(project_dir, self.TRACKER)
        client = mock_platform_cls.return_value
        result = runner.invoke(cli, ["job", "remove", "s", "-s", "Failed"])
        assert result.exit_code == 0, result.output
        client.job_cancel.assert_called_once_with("u1")
        client.remove_configs.assert_called_once_with("eid-s", {"a"})
        tracker = json.loads((project_dir / ".jrun" / "jobs.json").read_text())
        assert set(tracker["jobs"]) == {"u2", "queued-x"}
        assert tracker["searches"]["s"]["job_ids"] == ["u2", "queued-x"]


class TestStopFilters:
    @patch("jrun.commands.jobs.PlatformClient")
    def test_status_filter_only_stops_stoppable_jobs(self, mock_platform_cls, runner,
                                                     project_dir, monkeypatch):
        monkeypatch.chdir(project_dir)
        _write_tracker(project_dir, {"searches": {}, "jobs": {
            "u1": {"name": "a", "status": "Failed"},
            "u2": {"name": "b", "status": "Running"},
        }})
        client = mock_platform_cls.return_value
        client.get_job_status.return_value = "Running"
        result = runner.invoke(cli, ["job", "stop", "-s", "Failed", "-s", "Running"])
        assert result.exit_code == 0
        client.job_stop.assert_called_once_with("u2")
        assert "Stopped 1 jobs" in result.output

    @patch("jrun.commands.jobs.PlatformClient")
    def test_unknown_name_errors(self, mock_platform_cls, runner, project_dir, monkeypatch):
        monkeypatch.chdir(project_dir)
        result = runner.invoke(cli, ["job", "stop", "nope"])
        assert result.exit_code == 1
        assert "no job or search matching 'nope'" in result.output

    def test_no_project_is_clean_error(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(cli, ["job", "stop", "x"])
        assert result.exit_code == 1
        assert "jrun init" in result.output
