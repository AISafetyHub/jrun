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


# --- remote jobs and --json ---------------------------------------------------------

import pytest  # noqa: E402

from tests.remote_fakes import URL, FakeRemote, registry  # noqa: E402


@pytest.fixture
def fake_remote():
    fake = FakeRemote()
    with patch("jrun.commands.status.RemoteRegistry", return_value=registry(fake)):
        yield fake


def _tracker(project_dir, jobs, searches=None):
    (project_dir / ".jrun" / "jobs.json").write_text(
        json.dumps({"searches": searches or {}, "jobs": jobs}))


class TestRemoteStatus:
    @patch("jrun.commands.status.PlatformClient")
    def test_remote_jobs_refreshed_from_service_not_platform(
            self, mock_platform_cls, runner, project_dir, monkeypatch, fake_remote):
        monkeypatch.chdir(project_dir)
        _tracker(project_dir, {"r-1": {"name": "pub", "search_name": None, "status": "Submitted",
                                       "remote": URL, "submitted_at": "2026-11-27T00:00:00"}})
        fake_remote.rows = [{"job_id": "r-1", "name": "pub", "status": "Running",
                             "platform": PLATFORM_CONTEXT}]
        client = MagicMock()
        mock_platform_cls.return_value = client
        result = runner.invoke(cli, ["job", "status"])
        assert result.exit_code == 0, result.output
        assert "Running" in result.output
        client.get_job_info.assert_not_called()

    @patch("jrun.commands.status.PlatformClient")
    def test_unreachable_service_warns(self, mock_platform_cls, runner, project_dir,
                                       monkeypatch, fake_remote):
        from tests.remote_fakes import unreachable
        monkeypatch.chdir(project_dir)
        _tracker(project_dir, {"r-1": {"name": "pub", "status": "Running", "remote": URL,
                                       "submitted_at": "x"}})
        fake_remote.error = unreachable()
        result = runner.invoke(cli, ["job", "status"])
        assert result.exit_code == 0
        assert f"Warning: remote service {URL}" in result.output


class TestStatusJson:
    @patch("jrun.commands.status.PlatformClient")
    def test_json_lists_refreshed_jobs(self, mock_platform_cls, runner, project_dir,
                                       monkeypatch, fake_remote):
        monkeypatch.chdir(project_dir)
        _tracker(project_dir, {
            "loc": {"name": "a", "search_name": "s", "status": "Running", "params": {"x": 1},
                    "submitted_at": "t"},
            "queued-1": {"name": "b", "search_name": "s", "status": "Queued", "remote": URL,
                         "submitted_at": "t"}},
            {"s": {"job_ids": ["loc", "queued-1"], "config_file": "c.yaml",
                   "submitted_at": "t"}})
        fake_remote.rows = [{"job_id": "real-b", "name": "b", "search_name": "s",
                             "status": "Pending"}]
        client = MagicMock()
        client.get_job_status.return_value = "Failed"
        client.get_job_info.return_value = {"status": "Failed"}
        mock_platform_cls.return_value = client

        result = runner.invoke(cli, ["job", "status", "s", "--json"])

        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout)
        rows = {r["job_id"]: r for r in data["jobs"]}
        assert rows["loc"]["status"] == "Failed"
        assert rows["loc"]["params"] == {"x": 1}
        # the remote queued job started on the service: re-keyed
        assert rows["real-b"]["status"] == "Pending" and rows["real-b"]["remote"] == URL
        assert data["searches"]["s"]["job_ids"] == ["loc", "real-b"]

    @patch("jrun.commands.status.PlatformClient")
    def test_json_status_filter_and_unknown_name(self, mock_platform_cls, runner, project_dir,
                                                 monkeypatch, fake_remote):
        monkeypatch.chdir(project_dir)
        _tracker(project_dir, {"a": {"name": "a", "status": "Failed", "submitted_at": "t",
                                     "platform": PLATFORM_CONTEXT},
                               "b": {"name": "b", "status": "Stopped", "submitted_at": "t",
                                     "platform": PLATFORM_CONTEXT}})
        mock_platform_cls.return_value = MagicMock()
        data = json.loads(runner.invoke(cli, ["job", "status", "-s", "Failed", "--json"]).stdout)
        assert [r["job_id"] for r in data["jobs"]] == ["a"]
        data = json.loads(runner.invoke(cli, ["job", "status", "nope", "--json"]).stdout)
        assert data == {"jobs": [], "searches": {}}
