"""Unit tests for the tracking service layer."""

from unittest.mock import MagicMock

import pytest

from jrun.errors import PlatformError
from jrun.project import Project
from jrun.tracking import (find_tracked_job, refresh_active_statuses,
                           release_job, remove_tracked_jobs, stop_tracked_job)


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

    @pytest.mark.parametrize("status", ["Pending", "Approving"])
    def test_platform_waiting_job_stopped_on_platform(self, project, status):
        # Regression: a platform job reported as Pending/Approving was only
        # marked Stopped locally and kept running.
        platform = MagicMock()
        stop_tracked_job(project, platform, "uuid-1", {"name": "j", "status": status})
        platform.job_stop.assert_called_once_with("uuid-1")

    def test_platform_error_swallowed(self, project):
        platform = MagicMock()
        platform.job_stop.side_effect = PlatformError("job stop", 1, "err", "")
        stop_tracked_job(project, platform, "uuid-1", {"name": "j", "status": "Running"})


class TestReleaseJob:
    """Overwrite stops the old run first; failures must not be swallowed."""

    def test_terminal_job_needs_no_platform_call(self, project):
        platform = MagicMock()
        release_job(project, platform, "uuid-1", {"status": "Failed"})
        platform.job_stop.assert_not_called()

    def test_queued_job_marked_stopped_locally(self, project):
        ids = project.record_pending_jobs("s", "c.yaml", [{"name": "j"}])
        platform = MagicMock()
        release_job(project, platform, ids[0], {"status": "Queued"})
        platform.job_stop.assert_not_called()
        assert project.load_tracker()["jobs"][ids[0]]["status"] == "Stopped"

    @pytest.mark.parametrize("status", ["Pending", "Approving"])
    def test_platform_waiting_job_is_really_stopped(self, project, status):
        project.record_single_job("uuid-1", "j")
        platform = MagicMock()
        release_job(project, platform, "uuid-1", {"status": status})
        platform.job_stop.assert_called_once_with("uuid-1")

    def test_running_job_stopped_and_marked(self, project):
        project.record_single_job("uuid-1", "j")
        platform = MagicMock()
        release_job(project, platform, "uuid-1", {"status": "Running"})
        platform.job_stop.assert_called_once_with("uuid-1")
        assert project.load_tracker()["jobs"]["uuid-1"]["status"] == "Stopped"

    def test_stop_failure_raises_while_still_active(self, project):
        platform = MagicMock()
        platform.job_stop.side_effect = PlatformError("job stop", 1, "err", "")
        platform.get_job_status.return_value = "Running"
        with pytest.raises(PlatformError):
            release_job(project, platform, "uuid-1", {"status": "Running"})

    def test_stop_failure_ok_when_job_already_ended(self, project):
        project.record_single_job("uuid-1", "j")
        platform = MagicMock()
        platform.job_stop.side_effect = PlatformError("job stop", 1, "err", "")
        platform.get_job_status.return_value = "Succeed"
        release_job(project, platform, "uuid-1", {"status": "Running"})
        assert project.load_tracker()["jobs"]["uuid-1"]["status"] == "Succeed"


def _no_sleep(seconds):
    pass


class TestRemoveTrackedJobs:
    def test_cancels_removes_configs_per_experiment_and_untracks(self, project):
        for jid, name, exp in (("u1", "a", "e1"), ("u2", "b", "e2"), ("u3", "c", None)):
            project.record_single_job(jid, name, experiment_id=exp)
            project.update_job_status(jid, "Succeed")
        tracker = project.load_tracker()
        platform = MagicMock()

        result = remove_tracked_jobs(project, platform, tracker, ["u1", "u2", "u3"],
                                     default_exp_id="e-default", sleep=_no_sleep)

        assert result.removed == ["u1", "u2", "u3"] and result.still_active == {}
        platform.job_stop.assert_not_called()  # already ended
        assert platform.job_cancel.call_count == 3
        platform.remove_configs.assert_any_call("e1", {"a"})
        platform.remove_configs.assert_any_call("e2", {"b"})
        platform.remove_configs.assert_any_call("e-default", {"c"})
        assert project.load_tracker()["jobs"] == {}

    @pytest.mark.parametrize("status", ["Running", "Pending", "Approving", "Submitted"])
    def test_active_job_is_stopped_and_waited_before_cancel(self, project, status):
        project.record_single_job("u1", "a", experiment_id="e1")
        project.update_job_status("u1", status)
        platform = MagicMock()
        platform.get_job_status.side_effect = ["Stopping", "Stopping", "Stopped"]
        calls = []
        platform.job_stop.side_effect = lambda jid: calls.append("stop")
        platform.job_cancel.side_effect = lambda jid: calls.append("cancel")
        sleeps = []

        result = remove_tracked_jobs(project, platform, project.load_tracker(), ["u1"],
                                     poll_seconds=5, sleep=sleeps.append)

        assert calls == ["stop", "cancel"]
        assert sleeps == [5, 5]
        assert result.removed == ["u1"]
        platform.remove_configs.assert_called_once_with("e1", {"a"})
        assert project.load_tracker()["jobs"] == {}

    def test_job_still_active_after_wait_stays_tracked(self, project):
        project.record_single_job("u1", "a", experiment_id="e1")
        project.update_job_status("u1", "Running")
        platform = MagicMock()
        platform.get_job_status.return_value = "Running"

        result = remove_tracked_jobs(project, platform, project.load_tracker(), ["u1"],
                                     wait_seconds=10, poll_seconds=5, sleep=_no_sleep)

        assert result.removed == [] and result.still_active == {"u1": "Running"}
        platform.job_cancel.assert_not_called()
        platform.remove_configs.assert_not_called()
        assert "u1" in project.load_tracker()["jobs"]

    def test_stop_error_on_job_that_already_ended_is_fine(self, project):
        project.record_single_job("u1", "a", experiment_id="e1")
        project.update_job_status("u1", "Running")  # stale cached status
        platform = MagicMock()
        platform.job_stop.side_effect = PlatformError("job stop", 1, "x", "")
        platform.get_job_status.return_value = "Failed"

        result = remove_tracked_jobs(project, platform, project.load_tracker(), ["u1"],
                                     sleep=_no_sleep)

        assert result.removed == ["u1"]
        platform.job_cancel.assert_called_once_with("u1")

    def test_queued_job_skips_platform(self, project):
        ids = project.record_pending_jobs("s", "c.yaml", [{"name": "j"}], experiment_id="e1")
        tracker = project.load_tracker()
        platform = MagicMock()
        remove_tracked_jobs(project, platform, tracker, ids, sleep=_no_sleep)
        platform.job_stop.assert_not_called()
        platform.job_cancel.assert_not_called()
        platform.remove_configs.assert_not_called()
        assert ids[0] not in project.load_tracker()["jobs"]

    def test_archive_errors_are_best_effort(self, project):
        project.record_single_job("u1", "a", experiment_id="e1")
        project.update_job_status("u1", "Failed")
        platform = MagicMock()
        platform.job_cancel.side_effect = PlatformError("job cancel", 1, "x", "")
        platform.remove_configs.side_effect = PlatformError("experiment modify", 1, "x", "")
        remove_tracked_jobs(project, platform, project.load_tracker(), ["u1"], sleep=_no_sleep)
        assert project.load_tracker()["jobs"] == {}


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

        assert refresh_active_statuses(project, platform, tracker) == 1

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


# --- remote jobs ------------------------------------------------------------------

from jrun.tracking import sync_remote_jobs  # noqa: E402
from tests.remote_fakes import URL, FakeRemote, registry, unreachable  # noqa: E402


def _remote_project(project, jobs, searches=None):
    import json
    (project.jrun_dir / "jobs.json").write_text(json.dumps(
        {"searches": searches or {}, "jobs": jobs}))
    return project.load_tracker()


class TestSyncRemoteJobs:
    def test_updates_status_and_platform(self, project):
        tracker = _remote_project(project, {
            "r-1": {"name": "a", "status": "Submitted", "remote": URL},
            "loc": {"name": "b", "status": "Running"}})
        fake = FakeRemote([{"job_id": "r-1", "name": "a", "status": "Running",
                            "platform": {"userId": "staff"}}])
        changed = sync_remote_jobs(project, tracker, registry(fake))
        assert changed == 1
        saved = project.load_tracker()["jobs"]["r-1"]
        assert saved["status"] == "Running"
        assert saved["platform"] == {"userId": "staff"}

    def test_queued_job_rekeyed_by_search_and_name(self, project):
        tracker = _remote_project(
            project,
            {"queued-1": {"name": "a", "search_name": "s", "status": "Queued", "remote": URL}},
            {"s": {"job_ids": ["queued-1"]}})
        fake = FakeRemote([{"job_id": "real-1", "name": "a", "search_name": "s",
                            "status": "Pending"},
                           {"job_id": "other", "name": "a", "search_name": "t",
                            "status": "Running"}])
        sync_remote_jobs(project, tracker, registry(fake))
        saved = project.load_tracker()
        assert saved["searches"]["s"]["job_ids"] == ["real-1"]
        assert saved["jobs"]["real-1"]["status"] == "Pending"
        assert "real-1" in tracker["jobs"] and "queued-1" not in tracker["jobs"]

    def test_terminal_job_with_context_not_refreshed(self, project):
        tracker = _remote_project(project, {"r-1": {
            "name": "a", "status": "Failed", "remote": URL, "platform": {"userId": "u"}}})
        fake = FakeRemote()
        fake.error = AssertionError("must not be called")
        assert sync_remote_jobs(project, tracker, registry(fake)) == 0

    def test_unreachable_service_reported_and_left_alone(self, project):
        tracker = _remote_project(project, {"r-1": {"name": "a", "status": "Running",
                                                    "remote": URL}})
        fake = FakeRemote()
        fake.error = unreachable()
        errors = []
        sync_remote_jobs(project, tracker, registry(fake),
                         on_error=lambda url, e: errors.append(url))
        assert errors == [URL]
        assert project.load_tracker()["jobs"]["r-1"]["status"] == "Running"

    def test_local_refresh_skips_remote_jobs(self, project):
        tracker = _remote_project(project, {"r-1": {"name": "a", "status": "Running",
                                                    "remote": URL}})
        platform = MagicMock()
        refresh_active_statuses(project, platform, tracker)
        platform.get_job_status.assert_not_called()


class TestRemoteLifecycle:
    def test_stop_goes_to_service(self, project):
        _remote_project(project, {"r-1": {"name": "a", "status": "Running", "remote": URL}})
        fake = FakeRemote()
        platform = MagicMock()
        ok = stop_tracked_job(project, platform, "r-1",
                              {"name": "a", "status": "Running", "remote": URL}, registry(fake))
        assert ok and fake.stopped == ["r-1"]
        platform.job_stop.assert_not_called()

    def test_stop_unreachable_is_best_effort(self, project):
        fake = FakeRemote()
        fake.error = unreachable()
        assert not stop_tracked_job(project, MagicMock(), "r-1",
                                    {"status": "Running", "remote": URL}, registry(fake))

    def test_release_raises_when_service_cannot_stop(self, project):
        fake = FakeRemote()
        fake.stop_ok = False
        with pytest.raises(PlatformError, match="remote"):
            release_job(project, MagicMock(), "r-1",
                        {"status": "Running", "remote": URL}, registry(fake))

    def test_release_unreachable_raises(self, project):
        fake = FakeRemote()
        fake.error = unreachable()
        with pytest.raises(PlatformError, match="unreachable"):
            release_job(project, MagicMock(), "r-1",
                        {"status": "Running", "remote": URL}, registry(fake))

    def test_remove_mixes_local_and_remote(self, project):
        tracker = _remote_project(project, {
            "r-1": {"name": "a", "status": "Running", "remote": URL},
            "r-2": {"name": "b", "status": "Running", "remote": URL},
            "r-3": {"name": "c", "status": "Failed", "remote": URL},
            "loc": {"name": "d", "status": "Failed", "experiment_id": "eid"}})
        fake = FakeRemote()
        fake.remove_result = {"removed": ["r-1"], "still_active": {"r-2": "Running"},
                              "missing": ["r-3"]}
        platform = MagicMock()
        result = remove_tracked_jobs(project, platform, tracker, ["r-1", "r-2", "r-3", "loc"],
                                     remotes=registry(fake), sleep=lambda s: None)
        assert sorted(result.removed) == ["loc", "r-1", "r-3"]
        assert result.still_active == {"r-2": "Running"}
        assert fake.removed == ["r-1", "r-2", "r-3"]
        platform.job_cancel.assert_called_once_with("loc")
        platform.remove_configs.assert_called_once_with("eid", {"d"})
        assert set(project.load_tracker()["jobs"]) == {"r-2"}

    def test_remove_unreachable_keeps_tracked(self, project):
        tracker = _remote_project(project, {"r-1": {"name": "a", "status": "Running",
                                                    "remote": URL}})
        fake = FakeRemote()
        fake.error = unreachable()
        result = remove_tracked_jobs(project, MagicMock(), tracker, ["r-1"],
                                     remotes=registry(fake))
        assert result.removed == []
        assert "remote error" in result.still_active["r-1"]
        assert "r-1" in project.load_tracker()["jobs"]
