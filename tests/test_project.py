import json
import pytest
from pathlib import Path

from jrun.project import Project


@pytest.fixture
def project(tmp_path):
    return Project.init("test-exp", experiment_id="abc-123", directory=tmp_path)


class TestProjectInit:
    def test_creates_jrun_dir(self, tmp_path):
        Project.init("my-exp", directory=tmp_path)
        assert (tmp_path / ".jrun").is_dir()
        assert (tmp_path / ".jrun" / "settings.json").is_file()

    def test_settings_content(self, tmp_path):
        p = Project.init("my-exp", experiment_id="eid-1", directory=tmp_path)
        assert p.experiment_name == "my-exp"
        assert p.experiment_id == "eid-1"

    def test_with_platform(self, tmp_path):
        platform = {"projId": "p1", "projsetId": "ps1", "userId": "u1"}
        p = Project.init("exp", experiment_id="e1", platform=platform, directory=tmp_path)
        assert p.platform_ids == platform

    def test_find_from_subdirectory(self, tmp_path):
        Project.init("exp", experiment_id="e1", directory=tmp_path)
        sub = tmp_path / "a" / "b"
        sub.mkdir(parents=True)
        p = Project(start=sub)
        assert p.experiment_name == "exp"


class TestProjectTracker:
    def test_load_empty_tracker(self, project):
        tracker = project.load_tracker()
        assert tracker == {"searches": {}, "jobs": {}}

    def test_record_single_job(self, project):
        project.record_single_job("job-id-1", "my-job")
        tracker = project.load_tracker()
        assert "job-id-1" in tracker["jobs"]
        assert tracker["jobs"]["job-id-1"]["name"] == "my-job"
        assert tracker["jobs"]["job-id-1"]["status"] == "Submitted"

    def test_record_search(self, project):
        entries = [
            {"job_id": "j1", "name": "job-1", "params": {"lr": 0.01}},
            {"job_id": "j2", "name": "job-2", "params": {"lr": 0.001}},
        ]
        project.record_search("search-1", "config.yaml", entries)
        tracker = project.load_tracker()
        assert "search-1" in tracker["searches"]
        assert tracker["searches"]["search-1"]["job_ids"] == ["j1", "j2"]
        assert tracker["jobs"]["j1"]["search_name"] == "search-1"

    def test_update_job_status(self, project):
        project.record_single_job("j1", "job")
        project.update_job_status("j1", "Running")
        tracker = project.load_tracker()
        assert tracker["jobs"]["j1"]["status"] == "Running"

    def test_find_job_by_name(self, project):
        project.record_single_job("j1", "my-job")
        result = project.find_job_by_name("my-job")
        assert result is not None
        jid, info = result
        assert jid == "j1"
        assert info["name"] == "my-job"

    def test_find_job_by_name_not_found(self, project):
        assert project.find_job_by_name("nonexistent") is None

    def test_remove_job(self, project):
        project.record_single_job("j1", "job")
        project.remove_job("j1")
        tracker = project.load_tracker()
        assert "j1" not in tracker["jobs"]

    def test_remove_search(self, project):
        entries = [{"job_id": "j1", "name": "job-1", "params": {}}]
        project.record_search("s1", "config.yaml", entries)
        removed = project.remove_search("s1")
        assert removed == ["j1"]
        tracker = project.load_tracker()
        assert "s1" not in tracker["searches"]
        assert "j1" not in tracker["jobs"]

    def test_remove_search_nonexistent(self, project):
        assert project.remove_search("nope") == []

    def test_record_pending_jobs(self, project):
        from jrun.models import Job, ResourceConfig
        jobs = [
            Job(name="pending-1", command="echo 1", params={"x": 1},
                resource_config=ResourceConfig(), envs={}),
            Job(name="pending-2", command="echo 2", params={"x": 2},
                resource_config=ResourceConfig(), envs={}),
        ]
        ids = project.record_pending_jobs("s1", "config.yaml", jobs)
        assert len(ids) == 2
        tracker = project.load_tracker()
        for qid in ids:
            assert tracker["jobs"][qid]["status"] == "Queued"


class TestBuildJobUrl:
    def test_no_platform(self, tmp_path):
        p = Project.init("exp", experiment_id="e1", directory=tmp_path)
        assert p.build_job_url("job-id") is None

    def test_with_platform(self, tmp_path):
        platform = {"projId": "p1", "projsetId": "ps1", "userId": "u1"}
        p = Project.init("exp", experiment_id="e1", platform=platform, directory=tmp_path)
        url = p.build_job_url("job-id-123")
        assert "job-id-123" in url
        assert "projId=p1" in url


class TestExtractPlatformIds:
    def test_valid(self):
        data = {
            "storage_info": [{
                "user_id": 42,
                "volumes_path": "/mnt/abc-def-123/11111111-1111-1111-1111-111111111111_22222222-2222-2222-2222-222222222222/data",
            }],
            "projset_name": "MyProjSet",
            "project_name": "MyProject",
        }
        result = Project.extract_platform_ids(data)
        assert result is not None
        assert result["projsetId"] == "11111111-1111-1111-1111-111111111111"
        assert result["projId"] == "22222222-2222-2222-2222-222222222222"
        assert result["userId"] == "42"
        assert result["projsetName"] == "MyProjSet"

    def test_no_storage(self):
        assert Project.extract_platform_ids({}) is None
        assert Project.extract_platform_ids({"storage_info": []}) is None
