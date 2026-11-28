import pytest

from jrun.models import (mount_entries, mount_entries_rest, normalize_mounts,
                         ACTIVE_STATUSES, STOPPABLE_STATUSES, TERMINAL_STATUSES,
                         ExperimentSpec, Job, JobTarget, ResourceConfig,
                         RoleConfig, RunOptions, job_target,
                         normalize_image_region, normalize_queue_categories,
                         normalize_queue_category, queue_category_alias,
                         queue_category_value, total_replicas, validate_job)


class TestStatuses:
    def test_terminal_and_active_are_disjoint(self):
        assert not TERMINAL_STATUSES & ACTIVE_STATUSES
        assert {"Completed", "Succeed", "Failed", "Stopped", "Cancelled"} == TERMINAL_STATUSES

    def test_platform_waiting_statuses_are_active_and_stoppable(self):
        # Observed on real jobs: Approving (public approval), Pending (waiting
        # to be scheduled). Both are real platform jobs that must be stopped.
        assert {"Approving", "Pending"} <= ACTIVE_STATUSES
        assert STOPPABLE_STATUSES == ACTIVE_STATUSES


class TestQueueCategory:
    @pytest.mark.parametrize("alias,expected", [
        (None, "QUEUE_CATEGORY_DEDICATED"),
        ("dedicated", "QUEUE_CATEGORY_DEDICATED"),
        ("quota", "QUEUE_CATEGORY_DEDICATED"),
        ("private", "QUEUE_CATEGORY_DEDICATED"),
        ("Public", "QUEUE_CATEGORY_PUBLIC"),
        (" idle ", "QUEUE_CATEGORY_IDLE"),
        ("QUEUE_CATEGORY_IDLE", "QUEUE_CATEGORY_IDLE"),
    ])
    def test_normalize(self, alias, expected):
        assert normalize_queue_category(alias) == expected

    def test_unknown_raises(self):
        with pytest.raises(ValueError, match="unknown queue_category"):
            normalize_queue_category("shared")

    def test_platform_values(self):
        assert queue_category_value("dedicated") == 0
        assert queue_category_value("public") == 1
        assert queue_category_value("idle") == 2

    def test_alias(self):
        assert queue_category_alias(None) == "dedicated"
        assert queue_category_alias("QUEUE_CATEGORY_PUBLIC") == "public"


class TestImageRegion:
    def test_normalize(self):
        assert normalize_image_region(None) is None
        assert normalize_image_region("private") == "PRIVATE"

    def test_unknown_raises(self):
        with pytest.raises(ValueError, match="unknown image_region"):
            normalize_image_region("INTERNAL")


class TestRunOptions:
    def test_normalizes_and_validates_public(self):
        options = RunOptions.from_dict({
            "duration_hours": 12, "enable_checkpoint": True,
            "queue_priority_mode": "NORMAL", "usage_desc": "benchmark",
        })
        assert options.to_dict()["queue_priority_mode"] == "normal"
        options.validate_for_category("public")

    def test_public_requires_fields(self):
        with pytest.raises(ValueError, match="duration_hours, usage_desc, queue_priority_mode"):
            RunOptions().validate_for_category("public")

    def test_dedicated_and_idle_need_nothing(self):
        RunOptions().validate_for_category(None)
        RunOptions().validate_for_category("idle")

    def test_unknown_field_rejected(self):
        with pytest.raises(ValueError, match="unknown run_options"):
            RunOptions.from_dict({"duration": 1})

    @pytest.mark.parametrize("data,match", [
        ({"queue_priority_mode": "urgent"}, "normal or high"),
        ({"duration_hours": 0}, "greater than zero"),
    ])
    def test_invalid_values(self, data, match):
        with pytest.raises(ValueError, match=match):
            RunOptions.from_dict(data)


class TestResourceConfig:
    def test_from_dict_empty(self):
        rc = ResourceConfig.from_dict({})
        assert rc.queue_name is None
        assert rc.roles is None
        assert rc.to_dict() == {}

    def test_removed_id_fields_fail_fast(self):
        with pytest.raises(ValueError, match="IDs/display names were removed"):
            ResourceConfig.from_dict({"queue_name": "q", "queue_id": "id"})

    def test_worker_is_rejected_with_roles_hint(self):
        with pytest.raises(ValueError, match="worker was removed.*roles"):
            ResourceConfig.from_dict({"worker": {"replicas": 2}})

    def test_invalid_category_rejected(self):
        with pytest.raises(ValueError, match="unknown queue_category"):
            ResourceConfig.from_dict({"queue_category": "shared"})

    def test_unknown_keys_become_overrides(self):
        rc = ResourceConfig.from_dict({"queue_name": "q", "newField": 1})
        assert rc.platform_overrides == {"newField": 1}
        assert rc.to_dict() == {"queue_name": "q", "newField": 1}

    def test_roles_and_location_roundtrip(self):
        original = {
            "queue_category": "idle",
            "queue_name": "Public-Pool-Nvidia-A100",
            "project_set_name": "baai-safety",
            "project_name": "baai-safety_research",
            "region_name": "北京-大兴",
            "zone_name": "可用区A",
            "selected_nodes": ["node-1"],
            "roles": [
                {"name": "Master", "replicas": 1, "accelerator_count": 1,
                 "resource_region": "ACCELERATOR_SUITE"},
                {"name": "Worker", "replicas": 3, "extra_label_info": {"a": "b"}},
            ],
            "newField": "kept",
        }
        assert ResourceConfig.from_dict(original).to_dict() == original


class TestRoleConfig:
    def test_defaults(self):
        role = RoleConfig.from_dict({})
        assert role.to_dict() == {"name": "Master", "replicas": 1}

    def test_unknown_keys_become_overrides(self):
        role = RoleConfig.from_dict({"name": "Worker", "custom": 1})
        assert role.platform_overrides == {"custom": 1}


class TestJob:
    def test_basic(self):
        j = Job(name="test", command="echo hi")
        assert j.params == {} and j.envs == {}
        assert j.is_local is False

    def test_is_local(self):
        j = Job(name="l", command="c", resource_config=ResourceConfig(queue_name="local"))
        assert j.is_local is True

    def test_to_dict_always_has_core_keys(self):
        d = Job(name="test", command="echo hi").to_dict()
        assert d == {"name": "test", "command": "echo hi", "params": {},
                     "envs": {}, "resource_config": {}}

    def test_from_dict_minimal(self):
        j = Job.from_dict({"name": "x"})
        assert j.command == "" and j.params == {}

    def test_from_dict_requires_name(self):
        with pytest.raises(ValueError, match="name"):
            Job.from_dict({"command": "x"})

    def test_from_dict_normalizes_image_region(self):
        assert Job.from_dict({"name": "x", "image_region": "private"}).image_region == "PRIVATE"

    def test_roundtrip(self):
        original = {
            "name": "job1",
            "command": "train.py",
            "params": {"lr": 0.001, "bs": 32},
            "resource_config": {
                "queue_name": "gpu", "accelerator_count": 8,
                "roles": [{"name": "Master", "replicas": 1},
                          {"name": "Worker", "replicas": 2, "accelerator_count": 4}],
            },
            "envs": {"NCCL_DEBUG": "INFO"},
            "image": "img",
            "run_options": {"duration_hours": 4.0, "usage_desc": "t",
                            "queue_priority_mode": "normal"},
            "git_branch": "main",
            "platform_field": 1,
        }
        j = Job.from_dict(original)
        assert j.platform_overrides == {"platform_field": 1}
        assert j.to_dict() == original


class TestExperimentSpec:
    def test_from_dict_minimal(self):
        spec = ExperimentSpec.from_dict({"name": "exp"})
        assert spec.image is None
        assert spec.image_region is None
        assert spec.to_dict() == {"name": "exp"}

    def test_name_required(self):
        with pytest.raises(ValueError, match="name"):
            ExperimentSpec.from_dict({"image": "i"})

    def test_removed_id_fields_fail_fast(self):
        with pytest.raises(ValueError, match="IDs/display names were removed"):
            ExperimentSpec.from_dict({"name": "e", "projset_id": "id"})

    def test_image_region_normalized(self):
        spec = ExperimentSpec.from_dict({"name": "e", "image_region": "private"})
        assert spec.image_region == "PRIVATE"
        with pytest.raises(ValueError, match="unknown image_region"):
            ExperimentSpec.from_dict({"name": "e", "image_region": "INTERNAL"})

    def test_storage_info_rejected_for_mounts(self):
        with pytest.raises(ValueError, match="storage_info: use mounts"):
            ExperimentSpec.from_dict({"name": "e", "storage_info": []})

    def test_mounts_normalized(self):
        spec = ExperimentSpec.from_dict({"name": "e", "mounts": {
            "home": "RW", "project": False, "projset": "ro"}})
        assert spec.mounts == {"home": "rw", "project": False, "projset": "ro"}
        assert ExperimentSpec.from_dict({"name": "e"}).mounts is None
        assert normalize_mounts({"project": "false", "projset": True}) == {
            "project": False, "projset": "rw"}

    @pytest.mark.parametrize("mounts, message", [
        ({"data": "rw"}, "unknown mount 'data'"),
        ({"home": "write"}, "expected rw, ro, or false"),
        (["home"], "must be a mapping"),
    ])
    def test_mounts_invalid(self, mounts, message):
        with pytest.raises(ValueError, match=message):
            ExperimentSpec.from_dict({"name": "e", "mounts": mounts})

    def test_mount_entries(self):
        # Unset: every default directory read-write, platform order.
        assert mount_entries(None) == [(5, 1), (4, 1), (2, 1)]
        assert mount_entries({"project": False, "projset": "ro"}) == [(5, 1), (2, 0)]
        assert mount_entries({"home": False, "project": False, "projset": False}) == []
        assert mount_entries_rest({"home": False, "project": False}) == [
            {"directoryType": 2, "accessMode": 1}]

    def test_roundtrip(self):
        original = {
            "name": "exp", "image": "img:1", "image_region": "PRIVATE",
            "queue_name": "q1", "queue_category": "public", "description": "d",
            "project_set_name": "ps", "project_name": "p", "region_name": "r",
            "zone_name": "z", "train_frame": "PyTorch", "extra": True,
        }
        spec = ExperimentSpec.from_dict(original)
        assert spec.platform_overrides == {"extra": True}
        assert spec.to_dict() == original


class TestJobTarget:
    def test_job_values_win(self):
        spec = ExperimentSpec(name="e", image="spec-img", image_region="PRIVATE",
                              queue_name="spec-q", queue_category="public",
                              region_name="spec-r", zone_name="spec-z")
        job = Job(name="j", image="job-img", resource_config=ResourceConfig(
            queue_name="job-q", queue_category="idle", region_name="job-r"))
        target = job_target(job, spec)
        assert target == JobTarget(
            image="job-img", image_region="PRIVATE", queue_name="job-q",
            queue_category="QUEUE_CATEGORY_IDLE", region_name="job-r", zone_name="spec-z",
        )

    def test_spec_fallback(self):
        spec = ExperimentSpec(name="e", image="i", queue_name="q",
                              project_set_name="ps", project_name="p")
        target = job_target(Job(name="j"), spec)
        assert (target.image, target.queue_name) == ("i", "q")
        assert (target.project_set_name, target.project_name) == ("ps", "p")
        assert target.queue_category is None
        assert target.category == "QUEUE_CATEGORY_DEDICATED"

    def test_no_spec(self):
        assert job_target(Job(name="j"), None) == JobTarget()

    def test_local_queue_is_not_a_platform_queue(self):
        job = Job(name="j", resource_config=ResourceConfig(queue_name="local"))
        spec = ExperimentSpec(name="e", queue_name="spec-q")
        assert job_target(job, spec).queue_name is None

    def test_location_key_distinguishes_category_and_location(self):
        a = JobTarget(queue_name="q", region_name="r1")
        b = JobTarget(queue_name="q", region_name="r2")
        c = JobTarget(queue_name="q", region_name="r1", queue_category="QUEUE_CATEGORY_IDLE")
        assert len({a.location_key, b.location_key, c.location_key}) == 3
        assert a.location_key == JobTarget(queue_name="q", region_name="r1").location_key


class TestAutoQueue:
    def _job(self, **rc):
        return Job(name="j", resource_config=ResourceConfig(**rc))

    def test_category_list_normalized_and_deduplicated(self):
        assert normalize_queue_categories(["public", "idle", "Public"]) == (
            "QUEUE_CATEGORY_PUBLIC", "QUEUE_CATEGORY_IDLE")
        assert normalize_queue_categories(None) == ("QUEUE_CATEGORY_DEDICATED",)
        with pytest.raises(ValueError, match="must not be empty"):
            normalize_queue_categories([])
        with pytest.raises(ValueError, match="unknown queue_category"):
            ResourceConfig.from_dict({"queue_category": ["public", "shared"]})

    @pytest.mark.parametrize("models,counts,expected", [
        (["A", "B"], [4, 8], [("A", 4), ("B", 8)]),
        (["A", "B"], 8, [("A", 8), ("B", 8)]),
        ("A", [8, 4], [("A", 8), ("A", 4)]),
        ("A", 2, [("A", 2)]),
    ])
    def test_accelerator_choices(self, models, counts, expected):
        rc = ResourceConfig(accelerator_model=models, accelerator_count=counts)
        assert rc.accelerator_choices() == expected

    def test_choice_lists_must_pair_up(self):
        with pytest.raises(ValueError, match=r"\(2\) and accelerator_count \(3\)"):
            ResourceConfig.from_dict({"accelerator_model": ["A", "B"],
                                      "accelerator_count": [1, 2, 3]})
        with pytest.raises(ValueError, match="non-negative integer"):
            ResourceConfig.from_dict({"accelerator_model": ["A"], "accelerator_count": [-1]})

    def test_lists_require_auto(self):
        job = self._job(queue_name="Q", accelerator_model=["A", "B"], accelerator_count=1)
        with pytest.raises(ValueError, match="require resource_config.queue_name: auto"):
            validate_job(job, None)
        spec = ExperimentSpec(name="e", queue_category=["public", "idle"])
        with pytest.raises(ValueError, match="require resource_config.queue_name: auto"):
            validate_job(self._job(queue_name="Q"), spec)

    def test_auto_requires_model_and_count(self):
        with pytest.raises(ValueError, match="requires resource_config.accelerator_model"):
            validate_job(self._job(queue_name="auto", accelerator_count=1), None)
        with pytest.raises(ValueError, match="requires resource_config.accelerator_count"):
            validate_job(self._job(queue_name="auto", accelerator_model="A"), None)

    def test_auto_roles_cannot_override_accelerators(self):
        job = self._job(queue_name="auto", accelerator_model="A", accelerator_count=1,
                        roles=[RoleConfig(name="Worker", accelerator_count=2)])
        with pytest.raises(ValueError, match="role 'Worker' must not override"):
            validate_job(job, None)

    def test_public_in_category_list_requires_run_options(self):
        job = self._job(queue_name="auto", accelerator_model="A", accelerator_count=1,
                        queue_category=["dedicated", "public"])
        with pytest.raises(ValueError, match="public queue requires run_options"):
            validate_job(job, None)
        validate_job(self._job(queue_name="auto", accelerator_model="A",
                               accelerator_count=1, queue_category=["dedicated", "idle"]), None)

    def test_target_categories(self):
        spec = ExperimentSpec(name="e", queue_name="auto", queue_category=["idle", "public"])
        target = job_target(self._job(), spec)
        assert target.is_auto
        assert target.categories == ("QUEUE_CATEGORY_IDLE", "QUEUE_CATEGORY_PUBLIC")
        assert target.category == "QUEUE_CATEGORY_IDLE"
        single = job_target(self._job(queue_name="Q", queue_category=["public"]), None)
        assert single.queue_category == "QUEUE_CATEGORY_PUBLIC"

    def test_total_replicas(self):
        assert total_replicas(ResourceConfig()) == 1
        assert total_replicas(ResourceConfig(roles=[RoleConfig(), RoleConfig(replicas=3)])) == 4


class TestCategoryPriority:
    """Public accepts only priority high, idle only low (platform-enforced)."""

    OPTIONS = RunOptions(duration_hours=1, usage_desc="u", queue_priority_mode="normal")

    def _job(self, **rc):
        return Job(name="j", run_options=self.OPTIONS,
                   resource_config=ResourceConfig(queue_name="Q", **rc))

    def test_wrong_priority_rejected_per_category(self):
        with pytest.raises(ValueError, match="idle queues only accept priority low"):
            validate_job(self._job(queue_category="idle", priority="high"), None)
        with pytest.raises(ValueError, match="public queues only accept priority high"):
            validate_job(self._job(queue_category="public", priority="low"), None)
        auto = Job(name="j", run_options=RunOptions(duration_hours=1, usage_desc="u",
                                                    queue_priority_mode="normal"),
                   resource_config=ResourceConfig(
                       queue_name="auto", queue_category=["public", "idle"],
                       accelerator_model="A", accelerator_count=1, priority="high"))
        # No single priority satisfies both public and idle.
        with pytest.raises(ValueError, match="idle queues only accept priority low"):
            validate_job(auto, None)

    def test_matching_or_unset_priority_ok(self):
        validate_job(self._job(queue_category="idle", priority="low"), None)
        validate_job(self._job(queue_category="public", priority="HIGH"), None)
        validate_job(self._job(queue_category="idle"), None)
        validate_job(self._job(queue_category="public"), None)
        validate_job(self._job(queue_category="dedicated", priority="low"), None)


class TestRemoteSpec:
    def test_defaults_to_public(self):
        from jrun.models import PUBLIC, RemoteSpec
        spec = RemoteSpec.from_dict({"url": "https://svc:8443/"})
        assert spec.url == "https://svc:8443"
        assert spec.categories == (PUBLIC,)

    def test_categories_normalized(self):
        from jrun.models import IDLE, PUBLIC, RemoteSpec
        spec = RemoteSpec.from_dict({"url": "http://svc", "categories": ["public", "idle"]})
        assert spec.categories == (PUBLIC, IDLE)

    @pytest.mark.parametrize("data, message", [
        ({"url": "svc:8080"}, "http"),
        ({}, "http"),
        ("http://svc", "mapping"),
        ({"url": "http://svc", "token": "x"}, "unknown field"),
        ({"url": "http://svc", "categories": ["vip"]}, "queue_category"),
    ])
    def test_invalid(self, data, message):
        from jrun.models import RemoteSpec
        with pytest.raises(ValueError, match=message):
            RemoteSpec.from_dict(data)

    def test_routes_by_effective_category(self):
        from jrun.models import RemoteSpec
        remote = RemoteSpec.from_dict({"url": "http://svc"})
        spec = ExperimentSpec(name="e", queue_name="pool", queue_category="public")
        public = Job(name="a")
        idle = Job(name="b", resource_config=ResourceConfig(queue_category="idle"))
        local = Job(name="c", resource_config=ResourceConfig(queue_name="local"))
        assert remote.routes(public, spec)          # experiment default: public
        assert not remote.routes(idle, spec)        # job-level category wins
        assert not remote.routes(local, spec)       # local runs never go remote
