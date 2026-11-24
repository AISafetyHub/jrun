"""Unit tests for the submission service layer (no click, no CliRunner)."""

from unittest.mock import MagicMock, patch

import pytest

from jrun.errors import ApiError, ConfigError, PlatformError
from jrun.models import ExperimentSpec, Job, ResourceConfig, RunOptions, job_target
from jrun.placement import WAIT, Assignment, Placement
from jrun.project import Project
from jrun.submission import (ResolvedExperiment, classify_jobs, ensure_experiment,
                             find_queue, resolve_experiment,
                             resolve_queue_locations, run_local, submit_jobs,
                             submit_single, validate_jobs)


SPEC = ExperimentSpec(name="spec-exp", image="img:1", queue_name="q1")
QUEUE = {
    "id": "qid-1", "name": "q1", "quotaId": "quota-1",
    "projId": "p1", "projsetId": "ps1",
    "zoneId": "zid-1", "clusterId": "cid-1",
    "quotaDetailList": [{"resourceDetail": {"acceleratorModel": "H100"}}],
    "quotaInfoList": [],
}
PUBLIC_OPTIONS = RunOptions(duration_hours=1, usage_desc="t", queue_priority_mode="normal")
SCHED = {"parallel_trials": None, "poll_interval": 30}


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return Project.init()


def _platform(job_ids=None):
    platform = MagicMock()
    platform.run_config.side_effect = list(job_ids or ["new-id"])
    return platform


def _resolved(spec=None):
    return ResolvedExperiment(spec, "eid", {}, {"experiment_id": "eid"})


# --- queues & experiments -----------------------------------------------------

class TestFindQueue:
    def test_dedicated_lookup(self):
        api = MagicMock()
        api.find_queue.return_value = QUEUE
        target = job_target(Job(name="j"), ExperimentSpec(
            name="e", queue_name="q1", project_set_name="ps", region_name="r"))
        assert find_queue(api, target) is QUEUE
        api.find_queue.assert_called_once_with(
            "q1", queue_category="QUEUE_CATEGORY_DEDICATED",
            project_set_name="ps", project_name=None, region_name="r", zone_name=None,
        )

    def test_missing_dedicated_queue_lists_available(self):
        api = MagicMock()
        api.find_queue.return_value = None
        api.list_queues.return_value = [{"name": "b"}, {"name": "a"}]
        with pytest.raises(ConfigError, match="dedicated queue 'nope' not found. Available: a, b"):
            find_queue(api, job_target(Job(name="j", resource_config=ResourceConfig(
                queue_name="nope")), None))
        api.list_shared_queues.assert_not_called()

    def test_missing_shared_queue_lists_shared(self):
        api = MagicMock()
        api.find_queue.return_value = None
        api.list_shared_queues.return_value = [{"name": "Public-Pool-A"}]
        target = job_target(Job(name="j", resource_config=ResourceConfig(
            queue_name="nope", queue_category="idle")), None)
        with pytest.raises(ConfigError, match="idle queue 'nope'.*Public-Pool-A"):
            find_queue(api, target)
        api.list_shared_queues.assert_called_once_with(queue_category="QUEUE_CATEGORY_IDLE")

    def test_ambiguous_queue_becomes_config_error(self):
        api = MagicMock()
        api.find_queue.side_effect = ValueError("queue 'q1' has multiple locations")
        with pytest.raises(ConfigError, match="multiple locations"):
            find_queue(api, job_target(Job(name="j"), SPEC))


class TestResolveQueueLocations:
    def test_resolves_distinct_targets_once(self):
        api = MagicMock()
        api.find_queue.return_value = QUEUE
        jobs = [
            Job(name="a"), Job(name="b"),
            Job(name="c", resource_config=ResourceConfig(queue_name="q2")),
            Job(name="d", resource_config=ResourceConfig(queue_name="local")),
        ]
        locations = resolve_queue_locations(api, jobs, SPEC)
        assert api.find_queue.call_count == 2
        key = job_target(jobs[0], SPEC).location_key
        assert locations[key]["queue_id"] == "qid-1"
        assert locations[key]["queue_category"] == "QUEUE_CATEGORY_DEDICATED"

    def test_shared_category_and_names_kept(self):
        api = MagicMock()
        api.find_queue.return_value = {"id": "sq", "name": "Public-Pool"}
        job = Job(name="a", resource_config=ResourceConfig(
            queue_name="Public-Pool", queue_category="public",
            region_name="北京", zone_name="A"))
        locations = resolve_queue_locations(api, [job], None)
        location = locations[job_target(job, None).location_key]
        assert location["queue_category"] == "QUEUE_CATEGORY_PUBLIC"
        assert (location["region_name"], location["zone_name"]) == ("北京", "A")
        assert api.find_queue.call_args.kwargs["queue_category"] == "QUEUE_CATEGORY_PUBLIC"

    def test_no_queue_no_lookup(self):
        api = MagicMock()
        assert resolve_queue_locations(api, [Job(name="a")], None) == {}
        api.find_queue.assert_not_called()


class TestEnsureExperiment:
    def test_existing_experiment_skips_create(self):
        platform = MagicMock()
        platform.find_experiment.return_value = {"experiment_id": "eid-1"}
        api = MagicMock()
        exp_id = ensure_experiment(SPEC, platform, api, job_target(Job(name="j"), SPEC),
                                   confirm=None)
        assert exp_id == "eid-1"
        platform.find_experiment.assert_called_once_with("spec-exp")
        api.find_queue.assert_not_called()
        api.create_experiment.assert_not_called()

    def test_creates_when_missing(self):
        platform = MagicMock()
        platform.find_experiment.return_value = None
        api = MagicMock()
        api.find_queue.return_value = QUEUE
        api.create_experiment.return_value = "new-eid"
        job = Job(name="j", image="job-img", image_region="PRIVATE",
                  resource_config=ResourceConfig(queue_name="q1", queue_category="idle"))

        exp_id = ensure_experiment(SPEC, platform, api, job_target(job, SPEC),
                                   assume_yes=True, confirm=None)

        assert exp_id == "new-eid"
        kwargs = api.create_experiment.call_args.kwargs
        assert kwargs["image"] == "job-img"
        assert kwargs["image_region"] == "PRIVATE"
        assert kwargs["queue_category"] == "QUEUE_CATEGORY_IDLE"
        assert (kwargs["proj_id"], kwargs["projset_id"]) == ("p1", "ps1")

    def test_no_image_or_queue_raises(self):
        platform = MagicMock()
        platform.find_experiment.return_value = None
        spec = ExperimentSpec(name="e")
        with pytest.raises(ConfigError, match="no image/queue configured"):
            ensure_experiment(spec, platform, MagicMock(), job_target(Job(name="j"), spec),
                              assume_yes=True, confirm=None)

    def test_declined_confirmation_raises(self):
        platform = MagicMock()
        platform.find_experiment.return_value = None
        api = MagicMock()
        api.find_queue.return_value = QUEUE
        with pytest.raises(ConfigError, match="declined"):
            ensure_experiment(SPEC, platform, api, job_target(Job(name="j"), SPEC),
                              confirm=lambda *a, **k: False)
        api.create_experiment.assert_not_called()


class TestValidateRunOptions:
    def test_public_via_spec_default_requires_options(self):
        spec = ExperimentSpec(name="e", queue_name="Public-Pool", queue_category="public")
        with pytest.raises(ConfigError, match="job 'j': public queue requires"):
            validate_jobs([Job(name="j")], spec)

    def test_complete_options_pass(self):
        spec = ExperimentSpec(name="e", queue_name="Public-Pool", queue_category="public")
        validate_jobs([Job(name="j", run_options=PUBLIC_OPTIONS)], spec)


class TestResolveExperiment:
    def test_dry_run_never_touches_platform(self):
        platform, api = MagicMock(), MagicMock()
        resolved = resolve_experiment(platform, api, SPEC, [Job(name="j")],
                                      dry_run=True, confirm=None)
        assert resolved.name == "spec-exp" and resolved.id is None
        assert platform.mock_calls == [] and api.mock_calls == []

    def test_dry_run_still_validates(self):
        spec = ExperimentSpec(name="e", queue_category="public")
        with pytest.raises(ConfigError, match="public queue requires"):
            resolve_experiment(MagicMock(), None, spec, [Job(name="j")],
                               dry_run=True, confirm=None)

    def test_experiment_block_required(self):
        with pytest.raises(ConfigError, match="no 'experiment:' section"):
            resolve_experiment(MagicMock(), MagicMock(), None, [Job(name="j")],
                               dry_run=False, confirm=None)

    def test_api_required(self):
        with pytest.raises(ConfigError, match="jrun login"):
            resolve_experiment(MagicMock(), None, SPEC, [Job(name="j")],
                               dry_run=False, confirm=None)

    def test_reads_experiment_once_and_resolves_queues(self):
        platform = MagicMock()
        platform.find_experiment.return_value = {"experiment_id": "eid"}
        detail = {"experiment_id": "eid", "advance_config_infos": [{}, {}, {}]}
        platform.get_experiment.return_value = detail
        api = MagicMock()
        api.find_queue.return_value = QUEUE

        resolved = resolve_experiment(platform, api, SPEC, [Job(name="j")],
                                      dry_run=False, confirm=None, echo=lambda m: None)

        platform.get_experiment.assert_called_once_with("eid", check=True)
        assert resolved.id == "eid"
        assert resolved.experiment_data is detail
        assert resolved.config_count == 3
        assert list(resolved.queue_locations) == [job_target(Job(name="j"), SPEC).location_key]


class TestRunLocal:
    def test_returns_exit_code_and_reports_failure(self):
        errors = []
        code = run_local(Job(name="j", command="exit 3"), echo=lambda m: None,
                         err_echo=errors.append)
        assert code == 3
        assert errors == ["  Exit code: 3"]

    def test_success(self):
        assert run_local(Job(name="j", command="true"), echo=lambda m: None) == 0


class TestClassifyJobs:
    def test_new_overwrite_skipped(self):
        existing = {"b": ("id-b", {"status": "Failed"}), "c": ("id-c", {"status": "Running"})}
        result = classify_jobs([Job(name=n) for n in "abc"], existing.get, True, {"Failed"},
                               choose=None)
        assert [j.name for j in result.new] == ["a"]
        assert [j.name for j, _, _ in result.overwrite] == ["b"]
        assert [j.name for j, _, _ in result.skipped] == ["c"]

    def test_interactive_choose_callback(self):
        existing = {"a": ("id-a", {"status": "Failed"})}
        result = classify_jobs([Job(name="a")], existing.get, False, None,
                               choose=lambda j, i, s: True)
        assert len(result.overwrite) == 1


# --- submission ---------------------------------------------------------------

class TestSubmitSingle:
    def _submit(self, project, platform, job=None, **kwargs):
        kwargs.setdefault("overwrite", True)
        kwargs.setdefault("status_filter", None)
        return submit_single(project, platform, _resolved(kwargs.pop("spec", None)),
                             job or Job(name="j1", command="echo"),
                             confirm=lambda *a, **k: True, choose=lambda *a: True,
                             echo=lambda m: None, **kwargs)

    def test_fresh_job_modifies_runs_and_records(self, project):
        platform = _platform(["uuid-1"])
        assert self._submit(project, platform) == "uuid-1"
        platform.modify_configs.assert_called_once()
        platform.run_config.assert_called_once()
        info = project.load_tracker()["jobs"]["uuid-1"]
        assert info["experiment_id"] == "eid"

    def test_overwrite_stops_old_running_job_then_replaces_mapping(self, project):
        project.record_single_job("old-id", "j1")
        platform = _platform(["uuid-2"])
        platform.get_job_status.return_value = "Running"

        assert self._submit(project, platform) == "uuid-2"

        platform.job_stop.assert_called_once_with("old-id")
        tracker = project.load_tracker()
        assert "old-id" not in tracker["jobs"]
        assert project.find_job_by_name("j1")[0] == "uuid-2"

    def test_stop_failure_aborts_before_modify(self, project):
        project.record_single_job("old-id", "j1")
        platform = _platform()
        platform.get_job_status.return_value = "Running"
        platform.job_stop.side_effect = PlatformError("job stop", 1, "denied", "")

        with pytest.raises(PlatformError, match="denied"):
            self._submit(project, platform)

        platform.modify_configs.assert_not_called()
        assert "old-id" in project.load_tracker()["jobs"]

    def test_terminal_old_job_not_stopped(self, project):
        project.record_single_job("old-id", "j1")
        project.update_job_status("old-id", "Failed")
        platform = _platform()
        self._submit(project, platform)
        platform.job_stop.assert_not_called()

    def test_status_filter_skips(self, project):
        project.record_single_job("old-id", "j1")
        project.update_job_status("old-id", "Succeed")
        platform = _platform()
        assert self._submit(project, platform, status_filter={"Failed"}) is None
        platform.modify_configs.assert_not_called()

    def test_choose_skip(self, project):
        project.record_single_job("old-id", "j1")
        project.update_job_status("old-id", "Failed")
        platform = _platform()
        result = submit_single(project, platform, _resolved(), Job(name="j1"),
                               overwrite=False, status_filter=None, confirm=None,
                               choose=lambda *a: False, echo=lambda m: None)
        assert result is None
        platform.job_stop.assert_not_called()

    def test_run_failure_keeps_old_mapping(self, project):
        project.record_single_job("old-id", "j1")
        project.update_job_status("old-id", "Failed")
        platform = MagicMock()
        platform.run_config.side_effect = PlatformError("job run", 1, "boom", "")
        with pytest.raises(PlatformError):
            self._submit(project, platform)
        assert "old-id" in project.load_tracker()["jobs"]

    def test_effective_category_and_run_options_passed(self, project):
        spec = ExperimentSpec(name="e", queue_name="Public-Pool", queue_category="public")
        platform = _platform()
        job = Job(name="j1", run_options=PUBLIC_OPTIONS)
        self._submit(project, platform, job=job, spec=spec)
        platform.run_config.assert_called_once_with(
            "eid", "j1", run_options=PUBLIC_OPTIONS,
            queue_category="QUEUE_CATEGORY_PUBLIC",
        )


class TestSubmitJobs:
    def _submit(self, project, platform, jobs, sched=SCHED, **kwargs):
        errors = []
        result = submit_jobs(project, platform, _resolved(), "search", "c.yaml", jobs,
                             sched, echo=lambda m: None, err_echo=errors.append, **kwargs)
        return result, errors

    def test_one_modify_then_runs_every_job(self, project):
        platform = _platform(["id-a", "id-b"])
        jobs = [Job(name="a"), Job(name="b")]
        result, _ = self._submit(project, platform, jobs)
        platform.modify_configs.assert_called_once()
        assert platform.modify_configs.call_args.args[0] == jobs
        assert [e["job_id"] for e in result.job_entries] == ["id-a", "id-b"]
        assert project.load_tracker()["searches"]["search"]["job_ids"] == ["id-a", "id-b"]

    def test_modify_failure_runs_nothing_and_writes_nothing(self, project):
        platform = _platform()
        platform.modify_configs.side_effect = PlatformError("experiment modify", 1, "x", "")
        with pytest.raises(PlatformError):
            self._submit(project, platform, [Job(name="a")])
        platform.run_config.assert_not_called()
        assert project.load_tracker()["searches"] == {}

    def test_one_run_failure_does_not_block_others_or_replace_old_id(self, project):
        project.record_single_job("old-a", "a")
        project.update_job_status("old-a", "Failed")
        platform = MagicMock()
        platform.run_config.side_effect = [PlatformError("job run", 1, "boom", ""), "id-b"]
        result, errors = self._submit(project, platform, [Job(name="a"), Job(name="b")],
                                      replacements={"a": "old-a"})
        assert [e["name"] for e in result.job_entries] == ["b"]
        assert "boom" in errors[0]
        assert "old-a" in project.load_tracker()["jobs"]

    def test_overwrite_stops_old_job_and_replaces_mapping(self, project):
        project.record_single_job("old-id", "j1")
        platform = _platform(["new-id"])
        self._submit(project, platform, [Job(name="j1")], replacements={"j1": "old-id"})
        platform.job_stop.assert_called_once_with("old-id")
        tracker = project.load_tracker()
        assert "old-id" not in tracker["jobs"]
        assert tracker["jobs"]["new-id"]["name"] == "j1"

    def test_stop_failure_skips_only_that_job(self, project):
        project.record_single_job("old-a", "a")
        platform = _platform(["id-b"])
        platform.job_stop.side_effect = PlatformError("job stop", 1, "denied", "")
        platform.get_job_status.return_value = "Running"

        result, errors = self._submit(project, platform, [Job(name="a"), Job(name="b")],
                                      replacements={"a": "old-a"})

        assert [j.name for j in platform.modify_configs.call_args.args[0]] == ["b"]
        assert [e["name"] for e in result.job_entries] == ["b"]
        assert "could not stop old job" in errors[0]
        assert "old-a" in project.load_tracker()["jobs"]

    def test_all_stops_failed_does_nothing(self, project):
        project.record_single_job("old-a", "a")
        platform = _platform()
        platform.job_stop.side_effect = PlatformError("job stop", 1, "denied", "")
        platform.get_job_status.return_value = "Running"
        result, _ = self._submit(project, platform, [Job(name="a")],
                                 replacements={"a": "old-a"})
        assert result.job_entries == []
        platform.modify_configs.assert_not_called()

    def test_effective_category_persisted_for_scheduler(self, project):
        spec = ExperimentSpec(name="e", queue_name="P", queue_category="idle",
                              project_set_name="ps", project_name="p")
        job = Job(name="a")
        platform = _platform()
        submit_jobs(project, platform, _resolved(spec), "s", "c.yaml", [job], SCHED,
                    echo=lambda m: None)
        assert job.resource_config.queue_category == "QUEUE_CATEGORY_IDLE"
        # The scheduler checks submit slots in this project set.
        assert (job.resource_config.project_set_name,
                job.resource_config.project_name) == ("ps", "p")
        assert platform.run_config.call_args.kwargs["queue_category"] == "QUEUE_CATEGORY_IDLE"

    def test_parallel_trials_queues_rest_and_starts_scheduler(self, project):
        platform = _platform(["id-a"])
        jobs = [Job(name="a"), Job(name="b"), Job(name="c")]
        with patch("jrun.submission.Scheduler") as scheduler_cls:
            scheduler_cls.return_value.start.return_value = 4242
            result, _ = self._submit(project, platform, jobs,
                                     sched={"parallel_trials": 1, "poll_interval": 5})

        # every config is installed up front; only the first one runs now
        assert len(platform.modify_configs.call_args.args[0]) == 3
        assert platform.run_config.call_count == 1
        assert (result.pending, result.scheduler_pid) == (2, 4242)
        scheduler_cls.assert_called_once_with(project.jrun_dir, "search", None, "eid", 1, 5)
        pending = scheduler_cls.return_value.start.call_args.args[0]
        assert [p["name"] for p in pending] == ["b", "c"]
        tracker = project.load_tracker()
        statuses = [tracker["jobs"][j]["status"] for j in tracker["searches"]["search"]["job_ids"]]
        assert statuses == ["Submitted", "Queued", "Queued"]


# --- auto placement -------------------------------------------------------------

def _auto_job(name):
    return Job(name=name, run_options=PUBLIC_OPTIONS, resource_config=ResourceConfig(
        queue_name="auto", queue_category=["public", "idle"],
        accelerator_model=["A100", "V100"], accelerator_count=[4, 8]))


def _resolved_with(deferred_names):
    placement = Placement([Assignment(Job(name=n), MagicMock(), "A100", 4, 4, WAIT)
                           for n in deferred_names])
    return ResolvedExperiment(None, "eid", {}, {"experiment_id": "eid"}, placement=placement)


class TestAutoPlacementFlow:
    def test_placement_runs_before_experiment_creation(self):
        calls = []
        job = _auto_job("j")

        def fake_place(api, jobs, spec):
            calls.append("place")
            jobs[0].resource_config = ResourceConfig(queue_name="Public-Pool",
                                                     queue_category="public")
            return Placement(), {}

        platform = MagicMock()
        platform.find_experiment.side_effect = lambda name: calls.append("find") or {
            "experiment_id": "eid"}
        api = MagicMock()
        api.find_queue.return_value = QUEUE
        with patch("jrun.submission.place_auto_jobs", side_effect=fake_place):
            resolve_experiment(platform, api, SPEC, [job], dry_run=False, confirm=None,
                               echo=lambda m: None)
        assert calls == ["place", "find"]

    def test_known_queues_skip_lookup(self):
        job = Job(name="j", resource_config=ResourceConfig(queue_name="q1"))
        key = job_target(job, None).location_key
        api = MagicMock()
        locations = resolve_queue_locations(api, [job], None, {key: QUEUE})
        api.find_queue.assert_not_called()
        assert locations[key]["queue_id"] == "qid-1"

    def test_dry_run_places_with_api_and_warns_on_api_error(self):
        api = MagicMock()
        api.resolve_project_context.side_effect = ApiError("/x", 500, "down")
        messages = []
        resolved = resolve_experiment(MagicMock(), api, SPEC, [_auto_job("j")],
                                      dry_run=True, confirm=None, echo=messages.append)
        assert resolved.placement is None
        assert "cannot read queue capacity" in messages[0]

    def test_dry_run_without_auto_never_uses_api(self):
        api = MagicMock()
        resolve_experiment(MagicMock(), api, SPEC, [Job(name="j")], dry_run=True,
                           confirm=None)
        assert api.mock_calls == []

    def test_validates_run_options_for_public_in_list(self):
        job = _auto_job("j")
        job.run_options = RunOptions()
        with pytest.raises(ConfigError, match="job 'j': public queue requires"):
            validate_jobs([job], None)

    def test_deferred_jobs_go_to_scheduler_without_parallel_trials(self, project):
        platform = _platform(["id-a"])
        jobs = [Job(name="a"), Job(name="b")]
        messages = []
        with patch("jrun.submission.Scheduler") as scheduler_cls:
            scheduler_cls.return_value.start.return_value = 7
            result = submit_jobs(project, platform, _resolved_with(["b"]), "s", "c.yaml",
                                 jobs, SCHED, echo=messages.append)
        assert len(platform.modify_configs.call_args.args[0]) == 2  # both installed
        assert [c.args[1] for c in platform.run_config.call_args_list] == ["a"]
        assert result.pending == 1
        # parallel_trials unset -> scheduler gets 0 (no concurrency limit)
        scheduler_cls.assert_called_once_with(project.jrun_dir, "s", None, "eid", 0, 30)
        assert [p["name"] for p in scheduler_cls.return_value.start.call_args.args[0]] == ["b"]
        assert any("wait for a public/idle submit slot" in m for m in messages)

    def test_deferred_jobs_join_parallel_trials_pending(self, project):
        platform = _platform(["id-a"])
        jobs = [Job(name="a"), Job(name="b"), Job(name="c")]
        with patch("jrun.submission.Scheduler") as scheduler_cls:
            submit_jobs(project, platform, _resolved_with(["a"]), "s", "c.yaml", jobs,
                        {"parallel_trials": 1, "poll_interval": 5}, echo=lambda m: None)
        assert [c.args[1] for c in platform.run_config.call_args_list] == ["b"]
        pending = scheduler_cls.return_value.start.call_args.args[0]
        assert [p["name"] for p in pending] == ["c", "a"]

    def test_single_deferred_job_is_refused(self, project):
        platform = _platform()
        with pytest.raises(ConfigError, match="no public/idle submit slot"):
            submit_single(project, platform, _resolved_with(["j1"]), Job(name="j1"),
                          overwrite=False, status_filter=None, confirm=None, choose=None,
                          echo=lambda m: None)
        platform.modify_configs.assert_not_called()
