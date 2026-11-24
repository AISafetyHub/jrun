import pytest
from urllib.parse import parse_qs, urlparse

from jrun.project import Project, build_job_url


@pytest.fixture
def project(tmp_path):
    return Project.init(directory=tmp_path)


class TestProjectInit:
    def test_creates_jrun_dir(self, tmp_path):
        Project.init(directory=tmp_path)
        assert (tmp_path / ".jrun").is_dir()
        assert (tmp_path / ".jrun" / "settings.json").is_file()

    def test_settings_is_only_a_marker(self, tmp_path):
        Project.init(directory=tmp_path)
        assert (tmp_path / ".jrun" / "settings.json").read_text() == "{}\n"
        assert not hasattr(Project, "experiment_id")
        assert not hasattr(Project, "platform_ids")

    def test_init_keeps_existing_settings(self, tmp_path):
        Project.init(directory=tmp_path)
        (tmp_path / ".jrun" / "settings.json").write_text('{"experiment_id": "old"}')
        Project.init(directory=tmp_path)
        assert "old" in (tmp_path / ".jrun" / "settings.json").read_text()

    def test_missing_project_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="jrun init"):
            Project(start=tmp_path)

    def test_find_from_subdirectory(self, tmp_path):
        Project.init(directory=tmp_path)
        sub = tmp_path / "a" / "b"
        sub.mkdir(parents=True)
        p = Project(start=sub)
        assert p.jrun_dir == (tmp_path / ".jrun").resolve()


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
        platform = {"clusterName": "dx-calc1", "zoneName": "dx-calc1-zonea"}
        project.update_job_status("j1", "Running", platform)
        tracker = project.load_tracker()
        assert tracker["jobs"]["j1"]["status"] == "Running"
        assert tracker["jobs"]["j1"]["platform"] == platform

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
    def test_no_platform(self):
        assert build_job_url("job-id", None) is None
        assert build_job_url("job-id", {"projId": "p"}) is None

    def test_with_platform(self):
        job_platform = {
            "projId": "e31a8bd7-5c5e-47d5-a070-4ab934bff3c9",
            "projsetId": "a7d74c82-9265-41a6-a5c8-9828efde24ae",
            "userId": "319832320808853520",
            "projsetName": "baai-safety",
            "projectName": "baai-safety_research",
            "clusterName": "dx-calc1",
            "zoneName": "dx-calc1-zonea",
        }
        url = build_job_url("job-id-123", job_platform, "test")
        parsed = urlparse(url)
        assert parsed.netloc == "platform-multi.baai.ac.cn"
        assert parse_qs(parsed.query) == {
            "clusterName": ["dx-calc1"],
            "id": ["job-id-123"],
            "name": ["test"],
            "projId": ["e31a8bd7-5c5e-47d5-a070-4ab934bff3c9"],
            "projsetId": ["a7d74c82-9265-41a6-a5c8-9828efde24ae"],
            "userId": ["319832320808853520"],
        }
        assert parse_qs(parsed.fragment) == {
            "projsetName": ["baai-safety"],
            "projectName": ["baai-safety_research"],
            "clusterName": ["dx-calc1"],
            "zoneName": ["dx-calc1-zonea"],
        }
        assert "activeName" not in parse_qs(parsed.fragment)


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

    def test_new_platform_fields(self):
        data = {
            "projset_id": "ps-new",
            "proj_id": "p-new",
            "creator_id": 319832320808853520,
            "projset_name": "baai-safety",
            "proj_name": "baai-safety_research",
            "cluster_name": "dx-calc1",
            "zone_name": "dx-calc1-zonea",
        }
        assert Project.extract_platform_ids(data) == {
            "projsetId": "ps-new",
            "projId": "p-new",
            "userId": "319832320808853520",
            "projsetName": "baai-safety",
            "projectName": "baai-safety_research",
            "clusterName": "dx-calc1",
            "zoneName": "dx-calc1-zonea",
        }

    def test_no_storage(self):
        assert Project.extract_platform_ids({}) is None
        assert Project.extract_platform_ids({"storage_info": []}) is None


class TestTrackerExperimentId:
    def test_record_search_stores_experiment_id(self, tmp_path):
        p = Project.init(directory=tmp_path)
        entries = [{"job_id": "j1", "name": "job1", "params": {}}]
        p.record_search("s1", "config.yaml", entries,
                        experiment_id="custom-eid", experiment_name="custom-exp")
        tracker = p.load_tracker()
        assert tracker["searches"]["s1"]["experiment_id"] == "custom-eid"
        assert tracker["searches"]["s1"]["experiment_name"] == "custom-exp"
        assert tracker["jobs"]["j1"]["experiment_id"] == "custom-eid"
        # job rows carry the name too, so job links can be built per job
        assert tracker["jobs"]["j1"]["experiment_name"] == "custom-exp"

    def test_record_search_without_experiment_id(self, tmp_path):
        p = Project.init(directory=tmp_path)
        entries = [{"job_id": "j1", "name": "job1", "params": {}}]
        p.record_search("s1", "config.yaml", entries)
        tracker = p.load_tracker()
        assert "experiment_id" not in tracker["searches"]["s1"]
        assert "experiment_id" not in tracker["jobs"]["j1"]

    def test_record_single_job_stores_experiment_id(self, tmp_path):
        p = Project.init(directory=tmp_path)
        p.record_single_job("j1", "job1",
                            experiment_id="custom-eid", experiment_name="custom-exp")
        tracker = p.load_tracker()
        assert tracker["jobs"]["j1"]["experiment_id"] == "custom-eid"
        assert tracker["jobs"]["j1"]["experiment_name"] == "custom-exp"

    def test_record_pending_jobs_stores_experiment_id(self, tmp_path):
        p = Project.init(directory=tmp_path)
        jobs = [{"name": "pending1", "command": "echo", "params": {}, "resource_config": {}, "envs": {}}]
        ids = p.record_pending_jobs("s1", "config.yaml", jobs,
                                    experiment_id="custom-eid", experiment_name="custom-exp")
        tracker = p.load_tracker()
        assert tracker["searches"]["s1"]["experiment_id"] == "custom-eid"
        assert tracker["jobs"][ids[0]]["experiment_id"] == "custom-eid"

    def test_record_pending_jobs_keeps_replacement_until_scheduler_success(self, project):
        project.record_single_job("old-id", "pending")
        ids = project.record_pending_jobs(
            "s1", "config.yaml", [{"name": "pending", "command": "echo"}],
            replacement_ids={"pending": "old-id"},
        )
        tracker = project.load_tracker()
        assert tracker["jobs"]["old-id"]["name"] == "pending"
        assert tracker["jobs"][ids[0]]["replace_job_id"] == "old-id"
