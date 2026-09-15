"""Unit tests for the submission service layer (no click, no CliRunner)."""

import json
from unittest.mock import MagicMock

import pytest

from jrun.config import ConfigLoader
from jrun.errors import ApiError, ConfigError, PlatformError
from jrun.models import ExperimentSpec, Job, ResourceConfig, effective_spec, job_runtime
from jrun.project import Project
from jrun.submission import (classify_jobs, ensure_experiment,
                             resolve_experiment, resolve_queue_locations,
                             submit_jobs, submit_single)


SPEC = ExperimentSpec(name="spec-exp", image="img:1", queue_name="q1")
QUEUE = {
    "id": "qid-1", "name": "q1", "quotaId": "quota-1",
    "projId": "p1", "projsetId": "ps1",
    "zoneId": "zid-1", "clusterId": "cid-1",
    "quotaDetailList": [{"resourceDetail": {"acceleratorModel": "H100"}}],
    "quotaInfoList": [],
}


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return Project.init()


def _write_config(tmp_path, text):
    path = tmp_path / "cfg.yaml"
    path.write_text(text)
    return ConfigLoader(str(path))


class TestEnsureExperiment:
    def test_existing_experiment_skips_create(self):
        platform = MagicMock()
        platform.experiment_list.return_value = MagicMock(
            returncode=0, stdout=json.dumps({"experiment_id": "eid-1"}))
        api = MagicMock()

        exp_id = ensure_experiment(SPEC, platform, api, confirm=None)

        assert exp_id == "eid-1"
        # No queue lookup needed when the experiment already exists
        api.find_queue.assert_not_called()
        api.create_experiment.assert_not_called()

    def test_creates_when_missing_with_assume_yes(self):
        platform = MagicMock()
        platform.experiment_list.return_value = MagicMock(returncode=1, stdout="")
        api = MagicMock()
        api.find_queue.return_value = QUEUE
        api.create_experiment.return_value = "new-eid"

        exp_id = ensure_experiment(SPEC, platform, api, assume_yes=True, confirm=None)

        assert exp_id == "new-eid"
        kwargs = api.create_experiment.call_args[1]
        assert kwargs["name"] == "spec-exp"
        assert kwargs["image"] == "img:1"  # deprecated spec field as fallback
        assert kwargs["proj_id"] == "p1"

    def test_job_level_image_queue_win_over_spec(self):
        spec = ExperimentSpec(name="e")  # no deprecated fields
        platform = MagicMock()
        platform.experiment_list.return_value = MagicMock(returncode=1, stdout="")
        api = MagicMock()
        api.find_queue.return_value = QUEUE
        api.create_experiment.return_value = "new-eid"

        exp_id = ensure_experiment(spec, platform, api, assume_yes=True, confirm=None,
                                   image="job-img:2", image_region="PRIVATE",
                                   queue_name="q1")

        assert exp_id == "new-eid"
        api.find_queue.assert_called_once()
        assert api.find_queue.call_args[0][0] == "q1"
        kwargs = api.create_experiment.call_args[1]
        assert kwargs["image"] == "job-img:2"
        assert kwargs["image_region"] == "PRIVATE"

    def test_no_image_or_queue_anywhere_raises(self):
        spec = ExperimentSpec(name="e")
        platform = MagicMock()
        platform.experiment_list.return_value = MagicMock(returncode=1, stdout="")

        with pytest.raises(ConfigError, match="no image/queue"):
            ensure_experiment(spec, platform, MagicMock(), assume_yes=True, confirm=None)

    def test_declined_confirmation_raises(self):
        platform = MagicMock()
        platform.experiment_list.return_value = MagicMock(returncode=1, stdout="")
        api = MagicMock()
        api.find_queue.return_value = QUEUE

        with pytest.raises(ConfigError, match="Aborted"):
            ensure_experiment(SPEC, platform, api, confirm=lambda *a, **k: False)
        api.create_experiment.assert_not_called()

    def test_unknown_queue_lists_available(self):
        platform = MagicMock()
        platform.experiment_list.return_value = MagicMock(returncode=1, stdout="")
        api = MagicMock()
        api.find_queue.return_value = None
        api.list_queues.return_value = [QUEUE]

        with pytest.raises(ApiError, match="not found.*q1"):
            ensure_experiment(SPEC, platform, api, assume_yes=True, confirm=None)


class TestJobRuntime:
    def test_job_level_wins_over_spec(self):
        spec = ExperimentSpec(name="e", image="spec-img", queue_name="spec-q",
                              image_region="PRIVATE")
        job = Job(name="j", command="c", image="job-img", image_region="PUBLIC",
                  resource_config=ResourceConfig(queue_name="job-q"))
        assert job_runtime(job, spec) == ("job-img", "PUBLIC", "job-q")

    def test_spec_fallback(self):
        spec = ExperimentSpec(name="e", image="spec-img", queue_name="spec-q",
                              image_region="PRIVATE")
        job = Job(name="j", command="c")
        assert job_runtime(job, spec) == ("spec-img", "PRIVATE", "spec-q")

    def test_local_queue_is_not_a_platform_queue(self):
        job = Job(name="j", command="c",
                  resource_config=ResourceConfig(queue_name="local"))
        assert job_runtime(job, None) == (None, None, None)

    def test_effective_spec_none_without_overrides(self):
        assert effective_spec(Job(name="j", command="c"), None) is None

    def test_effective_spec_carries_job_fields(self):
        job = Job(name="j", command="c", image="img:1",
                  resource_config=ResourceConfig(queue_name="q1"))
        eff = effective_spec(job, None)
        assert eff.image == "img:1"
        assert eff.queue_name == "q1"
        assert eff.image_region == "PUBLIC"

    def test_effective_spec_keeps_spec_cluster_zone(self):
        spec = ExperimentSpec(name="e", cluster_id="c-x", zone_id="z-x")
        eff = effective_spec(Job(name="j", command="c"), spec)
        assert eff.cluster_id == "c-x"
        assert eff.zone_id == "z-x"
        assert eff.image is None


class TestResolveQueueLocations:
    def test_resolves_distinct_queues_once(self):
        jobs = [
            Job(name="a", command="c", resource_config=ResourceConfig(queue_name="q1")),
            Job(name="b", command="c", resource_config=ResourceConfig(queue_name="q1")),
            Job(name="c", command="c", resource_config=ResourceConfig(queue_name="q2")),
        ]
        api = MagicMock()
        api.find_queue.side_effect = lambda name, **kw: {**QUEUE, "name": name}

        locations = resolve_queue_locations(api, jobs, None)

        assert set(locations) == {"q1", "q2"}
        assert api.find_queue.call_count == 2

    def test_spec_cluster_zone_override_queue_lookup(self):
        spec = ExperimentSpec(name="e", cluster_id="c-x", zone_id="z-x")
        jobs = [Job(name="a", command="c", resource_config=ResourceConfig(queue_name="q1"))]
        api = MagicMock()
        api.find_queue.return_value = QUEUE

        locations = resolve_queue_locations(api, jobs, spec)

        assert locations["q1"]["cluster_id"] == "c-x"
        assert locations["q1"]["zone_id"] == "z-x"

    def test_unknown_queue_lists_available(self):
        jobs = [Job(name="a", command="c", resource_config=ResourceConfig(queue_name="nope"))]
        api = MagicMock()
        api.find_queue.return_value = None
        api.list_queues.return_value = [QUEUE]

        with pytest.raises(ApiError, match="queue 'nope' not found.*q1"):
            resolve_queue_locations(api, jobs, None)


class TestResolveExperiment:
    def test_legacy_path_uses_config_and_project_defaults(self, project, tmp_path):
        loader = _write_config(tmp_path, """
job:
  name: j
  command: echo hi
  experiment_id: "cfg-eid"
  experiment_name: "cfg-exp"
""")
        platform = MagicMock()
        platform.count_experiment_configs.return_value = 3

        resolved = resolve_experiment(loader, project, platform, None, None,
                                      dry_run=False, assume_yes=False, confirm=None)

        assert resolved.id == "cfg-eid"
        assert resolved.name == "cfg-exp"
        assert resolved.queue_locations == {}
        assert resolved.config_count == 3

    def test_no_experiment_raises(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        Project.init()  # empty settings
        project = Project()
        loader = _write_config(tmp_path, "job:\n  name: j\n  command: echo hi\n")

        with pytest.raises(ConfigError, match="no experiment"):
            resolve_experiment(loader, project, MagicMock(), None, None,
                               dry_run=False, assume_yes=False, confirm=None)

    def test_dry_run_never_touches_platform_or_api(self, project, tmp_path):
        loader = _write_config(tmp_path, """
experiment: {name: e, image: i, queue_name: q1}
job: {name: j, command: echo hi}
""")
        platform = MagicMock()
        api = MagicMock()

        resolved = resolve_experiment(loader, project, platform, api,
                                      loader.get_experiment_spec(),
                                      dry_run=True, assume_yes=False, confirm=None)

        assert resolved.name == "e"
        platform.count_experiment_configs.assert_not_called()
        api.find_queue.assert_not_called()

    def test_deprecated_spec_fields_warn(self, project, tmp_path):
        loader = _write_config(tmp_path, """
experiment: {name: e, image: i, queue_name: q1}
job: {name: j, command: echo hi}
""")
        messages = []
        resolve_experiment(loader, project, MagicMock(), None,
                           loader.get_experiment_spec(),
                           dry_run=True, assume_yes=False, confirm=None,
                           echo=messages.append)
        assert any("deprecated" in m for m in messages)

    def test_resolves_job_level_queue_locations(self, project, tmp_path):
        loader = _write_config(tmp_path, """
job:
  name: j
  command: echo hi
  experiment_id: "cfg-eid"
  resource_config:
    queue_name: q1
""")
        jobs = loader.build_single_job()
        api = MagicMock()
        api.find_queue.return_value = QUEUE
        platform = MagicMock()

        resolved = resolve_experiment(loader, project, platform, api, None,
                                      dry_run=False, assume_yes=False,
                                      jobs=[jobs], confirm=None)

        assert set(resolved.queue_locations) == {"q1"}
        assert resolved.queue_locations["q1"]["queue_id"] == "qid-1"

    def test_queue_resolution_requires_api(self, project, tmp_path):
        loader = _write_config(tmp_path, """
job:
  name: j
  command: echo hi
  experiment_id: "cfg-eid"
  resource_config:
    queue_name: q1
""")
        jobs = loader.build_single_job()
        with pytest.raises(ConfigError, match="requires platform API"):
            resolve_experiment(loader, project, MagicMock(), None, None,
                               dry_run=False, assume_yes=False,
                               jobs=[jobs], confirm=None)


class TestClassifyJobs:
    def test_new_overwrite_skipped(self):
        jobs = [Job(name="new-job", command="echo hi"), Job(name="old-job", command="echo hi"), Job(name="skip-job", command="echo hi")]
        tracked = {
            "old-job": ("id-old", {"status": "Failed"}),
            "skip-job": ("id-skip", {"status": "Running"}),
        }

        result = classify_jobs(
            jobs, lambda name: tracked.get(name), overwrite=True,
            status_filter={"Failed"}, choose=None)

        assert [j.name for j in result.new] == ["new-job"]
        assert [(j.name, oid) for j, oid, _ in result.overwrite] == [("old-job", "id-old")]
        assert [j.name for j, _, _ in result.skipped] == ["skip-job"]

    def test_interactive_choose_callback(self):
        jobs = [Job(name="a", command="echo hi"), Job(name="b", command="echo hi")]
        tracked = {"a": ("id-a", {"status": "Failed"}),
                   "b": ("id-b", {"status": "Failed"})}
        # overwrite only job "b"
        choose = lambda job, old_id, old_status: job.name == "b"

        result = classify_jobs(jobs, tracked.get, overwrite=False,
                               status_filter=None, choose=choose)

        assert [j.name for j, _, _ in result.overwrite] == ["b"]
        assert [j.name for j, _, _ in result.skipped] == ["a"]


class TestSubmitSingle:
    def test_fresh_job_submits_and_records(self, project):
        platform = MagicMock()
        platform.run_config.return_value = "uuid-1"

        job_id = submit_single(project, platform, Job(name="j1", command="echo hi"), "exp", "eid",
                               None, {}, overwrite=False, status_filter=None,
                               confirm=None, choose=None)

        assert job_id == "uuid-1"
        assert project.find_job_by_name("j1") is not None
        platform.modify_configs.assert_called_once()
        platform.run_config.assert_called_once_with("eid", "j1")
        platform.submit_job.assert_not_called()

    def test_job_level_image_queue_passed_to_platform(self, project):
        platform = MagicMock()
        platform.run_config.return_value = "uuid-1"
        job = Job(name="j1", command="echo hi", image="img:9", image_region="PRIVATE",
                  resource_config=ResourceConfig(queue_name="q1"))
        locations = {"q1": {"queue_id": "qid-1"}}

        submit_single(project, platform, job, "exp", "eid", None, locations,
                      overwrite=False, status_filter=None, confirm=None, choose=None)

        args, kwargs = platform.modify_configs.call_args
        assert args[0] == [job]
        assert args[1] == "eid"
        assert kwargs["spec"] is None
        assert kwargs["queue_locations"] == locations

    def test_existing_job_skip_returns_none(self, project):
        project.record_single_job("old-id", "j1", experiment_id="eid",
                                  experiment_name="exp")
        platform = MagicMock()
        platform.get_job_status.return_value = "Running"

        result = submit_single(project, platform, Job(name="j1", command="echo hi"), "exp", "eid",
                               None, {}, overwrite=False, status_filter=None,
                               confirm=None, choose=lambda *a: False)

        assert result is None
        platform.modify_configs.assert_not_called()
        platform.run_config.assert_not_called()

    def test_overwrite_only_replaces_local_mapping_after_success(self, project):
        project.record_single_job("old-id", "j1", experiment_id="eid",
                                  experiment_name="exp")
        platform = MagicMock()
        platform.get_job_status.return_value = "Running"
        platform.run_config.return_value = "uuid-2"

        job_id = submit_single(project, platform, Job(name="j1", command="echo hi"), "exp", "eid",
                               None, {}, overwrite=True, status_filter=None,
                               confirm=lambda *a, **k: True, choose=None)

        assert job_id == "uuid-2"
        platform.job_stop.assert_not_called()
        platform.job_cancel.assert_not_called()
        platform.remove_configs.assert_not_called()
        platform.submit_job.assert_not_called()
        tracker = project.load_tracker()
        assert "old-id" not in tracker["jobs"]
        assert project.find_job_by_name("j1")[0] == "uuid-2"

    def test_queued_job_never_touches_platform(self, project):
        project.record_single_job("queued-abc", "j1", experiment_id="eid",
                                  experiment_name="exp")
        platform = MagicMock()
        platform.run_config.return_value = "uuid-3"

        submit_single(project, platform, Job(name="j1", command="echo hi"), "exp", "eid",
                      None, {}, overwrite=True, status_filter=None,
                      confirm=lambda *a, **k: True, choose=None)

        platform.job_stop.assert_not_called()
        platform.job_cancel.assert_not_called()
        platform.get_job_status.assert_not_called()

    def test_modify_failure_keeps_old_mapping_and_does_not_run(self, project):
        project.record_single_job("old-id", "j1")
        platform = MagicMock()
        platform.get_job_status.return_value = "Failed"
        platform.modify_configs.side_effect = PlatformError(
            "experiment modify", 1, "modify failed", ""
        )

        with pytest.raises(PlatformError, match="modify failed"):
            submit_single(
                project, platform, Job(name="j1", command="echo hi"),
                "exp", "eid", None, {}, overwrite=True,
                status_filter=None, confirm=lambda *a, **k: True, choose=None,
            )

        assert project.find_job_by_name("j1")[0] == "old-id"
        platform.run_config.assert_not_called()

    def test_run_failure_keeps_old_mapping(self, project):
        project.record_single_job("old-id", "j1")
        platform = MagicMock()
        platform.get_job_status.return_value = "Failed"
        platform.run_config.side_effect = PlatformError(
            "job run", 1, "run failed", ""
        )

        with pytest.raises(PlatformError, match="run failed"):
            submit_single(
                project, platform, Job(name="j1", command="echo hi"),
                "exp", "eid", None, {}, overwrite=True,
                status_filter=None, confirm=lambda *a, **k: True, choose=None,
            )

        assert project.find_job_by_name("j1")[0] == "old-id"


class TestSubmitJobs:
    def test_batch_modifies_once_then_runs_every_job(self, project):
        jobs = [
            Job(name="j1", command="echo 1", params={"x": 1}),
            Job(name="j2", command="echo 2", params={"x": 2}),
            Job(name="j3", command="echo 3", params={"x": 3}),
        ]
        platform = MagicMock()
        platform.run_config.side_effect = ["id-1", "id-2", "id-3"]
        snapshot = {"advance_config_infos": [{"config_name": "template"}]}

        result = submit_jobs(
            project, platform, "search", "config.yaml", jobs,
            "exp", "eid", None, {},
            {"parallel_trials": None, "poll_interval": 30},
            experiment_data=snapshot,
        )

        platform.modify_configs.assert_called_once_with(
            jobs, "eid", spec=None, queue_locations={},
            experiment_data=snapshot,
        )
        assert platform.run_config.call_args_list == [
            (("eid", "j1"),), (("eid", "j2"),), (("eid", "j3"),),
        ]
        platform.submit_job.assert_not_called()
        assert [entry["job_id"] for entry in result.job_entries] == [
            "id-1", "id-2", "id-3",
        ]

    def test_modify_failure_runs_nothing_and_writes_nothing(self, project):
        jobs = [Job(name="j1", command="echo 1")]
        platform = MagicMock()
        platform.modify_configs.side_effect = PlatformError(
            "experiment modify", 1, "bad payload", ""
        )

        with pytest.raises(PlatformError, match="bad payload"):
            submit_jobs(
                project, platform, "search", "config.yaml", jobs,
                "exp", "eid", None, {},
                {"parallel_trials": None, "poll_interval": 30},
            )

        platform.run_config.assert_not_called()
        assert project.load_tracker() == {"searches": {}, "jobs": {}}

    def test_one_run_failure_does_not_block_others_or_replace_old_id(self, project):
        project.record_single_job("old-id", "j2")
        jobs = [
            Job(name="j1", command="echo 1"),
            Job(name="j2", command="echo 2"),
            Job(name="j3", command="echo 3"),
        ]
        platform = MagicMock()
        platform.run_config.side_effect = [
            "id-1",
            PlatformError("job run", 1, "failed j2", ""),
            "id-3",
        ]
        errors = []

        result = submit_jobs(
            project, platform, "search", "config.yaml", jobs,
            "exp", "eid", None, {},
            {"parallel_trials": None, "poll_interval": 30},
            replacements={"j2": "old-id"}, err_echo=errors.append,
        )

        assert platform.run_config.call_count == 3
        assert [entry["name"] for entry in result.job_entries] == ["j1", "j3"]
        tracker = project.load_tracker()
        assert "old-id" in tracker["jobs"]
        assert any("failed j2" in message for message in errors)

    def test_successful_overwrite_never_stops_or_cancels_old_job(self, project):
        project.record_single_job("old-id", "j1")
        job = Job(name="j1", command="echo new")
        platform = MagicMock()
        platform.run_config.return_value = "new-id"

        submit_jobs(
            project, platform, "search", "config.yaml", [job],
            "exp", "eid", None, {},
            {"parallel_trials": None, "poll_interval": 30},
            replacements={"j1": "old-id"},
        )

        platform.job_stop.assert_not_called()
        platform.job_cancel.assert_not_called()
        platform.remove_configs.assert_not_called()
        tracker = project.load_tracker()
        assert "old-id" not in tracker["jobs"]
        assert tracker["jobs"]["new-id"]["name"] == "j1"
