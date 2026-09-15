"""Unit tests for the tracking service layer."""

from unittest.mock import MagicMock

import pytest

from jrun.errors import PlatformError
from jrun.project import Project
from jrun.tracking import (find_tracked_job, refresh_active_statuses,
                           remove_one_job, stop_tracked_job)


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return Project.init()


class TestFindTrackedJob:
    TRACKER = {
        "jobs": {
            "uuid-1": {"name": "job-a"},
            "uuid-2": {"name": "job-b"},
        }
    }

    def test_by_id(self):
        assert find_tracked_job(self.TRACKER, "uuid-1")[1]["name"] == "job-a"

    def test_by_name(self):
        assert find_tracked_job(self.TRACKER, "job-b")[0] == "uuid-2"

    def test_not_found(self):
        assert find_tracked_job(self.TRACKER, "nope") is None


class TestStopTrackedJob:
    def test_queued_job_marked_stopped_without_platform(self, project):
        platform = MagicMock()
        info = {"name": "j", "status": "Queued"}
        project.record_pending_jobs("s", "c.yaml", [{"name": "j"}])
        stop_tracked_job(project, platform, "queued-x", info)
        platform.job_stop.assert_not_called()

    def test_running_job_stopped_on_platform(self, project):
        platform = MagicMock()
        stop_tracked_job(project, platform, "uuid-1", {"name": "j", "status": "Running"})
        platform.job_stop.assert_called_once_with("uuid-1")

    def test_platform_error_swallowed(self, project):
        platform = MagicMock()
        platform.job_stop.side_effect = PlatformError("job stop", 1, "err", "")
        stop_tracked_job(project, platform, "uuid-1", {"name": "j", "status": "Running"})


class TestRemoveOneJob:
    def test_cancels_and_removes_config(self):
        platform = MagicMock()
        remove_one_job(platform, "uuid-1", "job-a", "eid-1")
        platform.job_cancel.assert_called_once_with("uuid-1")
        platform.remove_configs.assert_called_once_with("eid-1", {"job-a"})

    def test_queued_job_skips_cancel(self):
        platform = MagicMock()
        remove_one_job(platform, "queued-x", "job-a", "eid-1")
        platform.job_cancel.assert_not_called()

    def test_no_experiment_skips_config_removal(self):
        platform = MagicMock()
        remove_one_job(platform, "uuid-1", "job-a", None)
        platform.remove_configs.assert_not_called()


class TestRefreshActiveStatuses:
    def test_refreshes_only_non_terminal(self, project):
        tracker = {
            "jobs": {
                "j1": {"name": "a", "status": "Running"},
                "j2": {"name": "b", "status": "Succeed"},
                "queued-x": {"name": "c", "status": "Queued"},
            }
        }
        platform = MagicMock()
        platform.get_job_status.return_value = "Failed"

        refresh_active_statuses(project, platform, tracker)

        platform.get_job_status.assert_called_once_with("j1")
        assert tracker["jobs"]["j1"]["status"] == "Failed"
        assert tracker["jobs"]["j2"]["status"] == "Succeed"

    def test_job_ids_scope(self, project):
        tracker = {
            "jobs": {
                "j1": {"name": "a", "status": "Running"},
                "j2": {"name": "b", "status": "Running"},
            }
        }
        platform = MagicMock()
        platform.get_job_status.return_value = "Running"

        refresh_active_statuses(project, platform, tracker, job_ids=["j2"])

        platform.get_job_status.assert_called_once_with("j2")
