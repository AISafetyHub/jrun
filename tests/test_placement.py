"""Auto queue placement (queue_name: auto)."""

import pytest

from jrun.errors import ApiError, ConfigError
from jrun.models import (ExperimentSpec, Job, ResourceConfig, RoleConfig,
                         job_target)
from jrun.placement import QUEUE, START, WAIT, place_auto_jobs

A100 = "NVIDIA_A100-SXM4-40GB"
V100 = "NVIDIA_V100-PCIE-32GB"


def dedicated_row(name, model, total, used, qid=None, region="北京-大兴", zone="可用区A",
                  strategy=0):
    """queue/select row (usedQuota form); strategy 0 = FIFO, 1 = Traverse, 2 = Smart."""
    return {
        "id": qid or f"id-{name}", "name": name, "projsetName": "ps", "projName": "p",
        "projId": "p1", "projsetId": "ps1", "scheduleStrategy": strategy,
        "usedQuota": {"computing": {model: {"total": {"high": total},
                                            "used": {"high": used}}}},
        "quotaInfoList": [{"clusterDisplayName": region,
                           "zoneQuotaInfoList": [{"zoneDisplayName": zone}]}],
    }


def shared_row(name, model, total, used, qid=None, region="北京-大兴", zone="可用区A"):
    """shared-queue/list row (quota/used form)."""
    return {
        "id": qid or f"id-{name}", "name": name, "acceleratorModel": model,
        "quota": {"acceleratorCount": total}, "used": {"acceleratorCount": used},
        "clusterDisplayName": region, "zoneDisplayName": zone,
    }


class FakeAPI:
    def __init__(self, dedicated=(), shared=(), budgets=None, waiting=None,
                 shared_waiting=None, waiting_error=False):
        self.dedicated = list(dedicated)
        self.shared = list(shared)
        self.budgets = budgets if budgets is not None else {
            "QUEUE_CATEGORY_PUBLIC": 4, "QUEUE_CATEGORY_IDLE": 5}
        self.waiting = waiting or {}                # queue_id -> waiting jobs
        self.shared_waiting = shared_waiting or {}  # (category, queue_id) -> waiting
        self.waiting_error = waiting_error
        self.list_calls = []
        self.budget_calls = 0
        self.waiting_calls = []
        self.shared_waiting_calls = 0

    def resolve_project_context(self, project_set_name=None, project_name=None):
        return ("p1", "ps1") if project_set_name else (None, None)

    def list_category_queues(self, category, proj_id=None, projset_id=None):
        self.list_calls.append(category)
        return self.dedicated if category == "QUEUE_CATEGORY_DEDICATED" else self.shared

    def shared_queue_budgets(self, proj_id=None, projset_id=None):
        self.budget_calls += 1
        return dict(self.budgets)

    def queue_waiting_count(self, queue_id, proj_id=None, projset_id=None):
        self.waiting_calls.append(queue_id)
        if self.waiting_error:
            raise ApiError("job/select", 403, "forbidden")
        return self.waiting.get(queue_id, 0)

    def shared_waiting_counts(self, proj_id=None, projset_id=None):
        self.shared_waiting_calls += 1
        if self.waiting_error:
            raise ApiError("job-count", 500, "boom")
        return dict(self.shared_waiting)


def auto_job(name, models=A100, counts=1, category="public", roles=None, **rc):
    return Job(name=name, resource_config=ResourceConfig(
        queue_name="auto", queue_category=category,
        accelerator_model=models, accelerator_count=counts, roles=roles, **rc))


def place(api, jobs, spec=None):
    placement, queues = place_auto_jobs(api, jobs, spec)
    return placement, queues, {a.job.name: a for a in placement.assignments}


class TestPreference:
    def test_pair_order_then_fallback_when_full(self):
        api = FakeAPI(shared=[shared_row("A", A100, 8, 0), shared_row("V", V100, 16, 0)])
        jobs = [auto_job(f"j{i}", [A100, V100], [4, 8]) for i in range(3)]
        _, _, by_name = place(api, jobs)
        assert [by_name[n].candidate.queue_name for n in ("j0", "j1", "j2")] == ["A", "A", "V"]
        assert by_name["j2"].count == 8
        assert all(a.status == START for a in by_name.values())

    def test_category_order(self):
        api = FakeAPI(dedicated=[dedicated_row("D", A100, 4, 0)],
                      shared=[shared_row("S", A100, 16, 0)])
        jobs = [auto_job(f"j{i}", A100, 4, category=["dedicated", "public"]) for i in range(2)]
        _, _, by_name = place(api, jobs)
        assert by_name["j0"].candidate.queue_name == "D"
        assert by_name["j1"].candidate.queue_name == "S"
        assert by_name["j1"].candidate.category == "QUEUE_CATEGORY_PUBLIC"

    def test_most_free_queue_first_within_same_preference(self):
        api = FakeAPI(shared=[shared_row("small", A100, 8, 6), shared_row("big", A100, 32, 0)])
        _, _, by_name = place(api, [auto_job("j", A100, 1)])
        assert by_name["j"].candidate.queue_name == "big"

    def test_model_match_is_case_insensitive_and_uses_platform_spelling(self):
        api = FakeAPI(shared=[shared_row("A", A100, 8, 0)])
        _, _, by_name = place(api, [auto_job("j", A100.lower(), 1)])
        assert by_name["j"].model == A100


class TestCapacity:
    def test_public_and_idle_share_one_pool(self):
        api = FakeAPI(shared=[shared_row("S", A100, 8, 0)])
        jobs = [auto_job(f"j{i}", A100, 8, category=["public", "idle"]) for i in range(2)]
        _, _, by_name = place(api, jobs)
        assert by_name["j0"].status == START
        # The pool's 8 cards are taken; idle sees the same pool.
        assert by_name["j1"].status == QUEUE

    def test_budget_is_per_mode(self):
        api = FakeAPI(shared=[shared_row("S", A100, 32, 0)],
                      budgets={"QUEUE_CATEGORY_PUBLIC": 1, "QUEUE_CATEGORY_IDLE": 1})
        jobs = [auto_job(f"j{i}", A100, 1, category=["public", "idle"]) for i in range(3)]
        placement, _, by_name = place(api, jobs)
        assert by_name["j0"].candidate.category == "QUEUE_CATEGORY_PUBLIC"
        assert by_name["j1"].candidate.category == "QUEUE_CATEGORY_IDLE"
        assert by_name["j2"].status == WAIT
        assert placement.deferred == {"j2"}

    def test_unlimited_budget(self):
        api = FakeAPI(shared=[shared_row("S", A100, 32, 0)],
                      budgets={"QUEUE_CATEGORY_PUBLIC": None, "QUEUE_CATEGORY_IDLE": None})
        placement, _, _ = place(api, [auto_job(f"j{i}", A100, 1) for i in range(10)])
        assert placement.counts() == {START: 10, QUEUE: 0, WAIT: 0}

    def test_cards_scale_with_role_replicas(self):
        api = FakeAPI(shared=[shared_row("S", A100, 16, 10)])
        roles = [RoleConfig(name="Master"), RoleConfig(name="Worker", replicas=1)]
        _, _, by_name = place(api, [auto_job("j", A100, 4, roles=roles)])
        assert by_name["j"].cards == 8
        assert by_name["j"].status == QUEUE  # only 6 free

    def test_queue_too_small_is_never_used(self):
        api = FakeAPI(dedicated=[dedicated_row("empty", A100, 0, 0)],
                      shared=[shared_row("S", A100, 8, 8)])
        _, _, by_name = place(api, [auto_job("j", A100, 4, category=["dedicated", "public"])])
        assert by_name["j"].candidate.queue_name == "S"


class TestOverflow:
    def test_overflow_prefers_dedicated(self):
        api = FakeAPI(dedicated=[dedicated_row("D", A100, 8, 8)],
                      shared=[shared_row("S", A100, 8, 8)])
        _, _, by_name = place(api, [auto_job("j", A100, 4, category=["public", "dedicated"])])
        assert by_name["j"].candidate.queue_name == "D"
        assert by_name["j"].status == QUEUE

    def test_shared_only_queues_while_slots_remain_then_waits(self):
        api = FakeAPI(shared=[shared_row("S", A100, 8, 8)],
                      budgets={"QUEUE_CATEGORY_PUBLIC": 1, "QUEUE_CATEGORY_IDLE": 0})
        jobs = [auto_job(f"j{i}", A100, 4, category=["public", "idle"]) for i in range(2)]
        placement, _, by_name = place(api, jobs)
        assert (by_name["j0"].status, by_name["j1"].status) == (QUEUE, WAIT)
        assert by_name["j1"].candidate.queue_name == "S"


class TestCandidates:
    def test_no_candidate_lists_available_models(self):
        api = FakeAPI(shared=[shared_row("V", V100, 16, 0)])
        with pytest.raises(ConfigError, match=f"no public queue offers {A100} x1.*{V100}"):
            place(api, [auto_job("j", A100, 1)])

    def test_location_filters_apply(self):
        api = FakeAPI(shared=[shared_row("far", A100, 64, 0, region="北京-亦庄"),
                              shared_row("near", A100, 8, 0)])
        _, _, by_name = place(api, [auto_job("j", A100, 1, region_name="北京-大兴")])
        assert by_name["j"].candidate.queue_name == "near"

    def test_queries_once_per_context_and_category(self):
        api = FakeAPI(shared=[shared_row("S", A100, 64, 0)])
        place(api, [auto_job(f"j{i}", A100, 1, category=["public", "idle"]) for i in range(5)])
        assert sorted(api.list_calls) == ["QUEUE_CATEGORY_IDLE", "QUEUE_CATEGORY_PUBLIC"]
        assert api.budget_calls == 1

    def test_non_auto_jobs_untouched(self):
        api = FakeAPI()
        job = Job(name="fixed", resource_config=ResourceConfig(queue_name="Q"))
        placement, queues = place_auto_jobs(api, [job], None)
        assert placement.assignments == [] and queues == {}
        assert api.list_calls == []


class TestApply:
    def test_job_becomes_concrete_with_envs(self):
        api = FakeAPI(shared=[shared_row("S", V100, 16, 0)])
        job = auto_job("j", [A100, V100], [4, 8], category=["public", "idle"],
                       project_set_name="ps")
        job.envs["KEEP"] = "1"
        _, queues, _ = place(api, [job])
        rc = job.resource_config
        assert (rc.queue_name, rc.queue_category) == ("S", "public")
        assert (rc.accelerator_model, rc.accelerator_count) == (V100, 8)
        assert (rc.region_name, rc.zone_name) == ("北京-大兴", "可用区A")
        assert not rc.has_choice_lists
        assert job.envs == {"KEEP": "1", "JRUN_QUEUE_NAME": "S",
                            "JRUN_ACCELERATOR_MODEL": V100, "JRUN_ACCELERATOR_COUNT": "8"}
        target = job_target(job, None)
        assert not target.is_auto
        assert queues[target.location_key]["name"] == "S"
        assert queues[target.location_key]["projsetId"] == "ps1"  # project context filled

    def test_experiment_level_auto(self):
        api = FakeAPI(shared=[shared_row("S", A100, 16, 0)])
        spec = ExperimentSpec(name="e", queue_name="auto", queue_category=["public", "idle"])
        job = Job(name="j", resource_config=ResourceConfig(accelerator_model=A100,
                                                           accelerator_count=2))
        place(api, [job], spec)
        assert job_target(job, spec).queue_name == "S"
        assert job_target(job, spec).category == "QUEUE_CATEGORY_PUBLIC"


PUB, IDL = "QUEUE_CATEGORY_PUBLIC", "QUEUE_CATEGORY_IDLE"


class TestFifo:
    """Queues with jobs already waiting do not start new jobs (FIFO)."""

    def test_fifo_queue_with_waiting_jobs_is_skipped_for_start(self):
        # "busy" shows the most free cards but has a job waiting ahead.
        api = FakeAPI(dedicated=[dedicated_row("busy", A100, 56, 51),
                                 dedicated_row("quiet", A100, 8, 4)],
                      waiting={"id-busy": 1})
        _, _, by_name = place(api, [auto_job("j", A100, 4, category="dedicated")])
        assert by_name["j"].candidate.queue_name == "quiet"
        assert by_name["j"].status == START

    def test_traverse_queue_still_starts(self):
        api = FakeAPI(dedicated=[dedicated_row("trav", A100, 8, 0, strategy=1)],
                      waiting={"id-trav": 3})
        _, _, by_name = place(api, [auto_job("j", A100, 4, category="dedicated")])
        assert by_name["j"].status == START
        assert api.waiting_calls == []  # Traverse never needs the count

    def test_smart_counts_as_fifo(self):
        api = FakeAPI(dedicated=[dedicated_row("smart", A100, 8, 0, strategy=2)],
                      waiting={"id-smart": 2})
        _, _, by_name = place(api, [auto_job("j", A100, 4, category="dedicated")])
        assert by_name["j"].status == QUEUE
        assert by_name["j"].waiting == 2

    def test_blocked_dedicated_queue_with_fewest_waiting_is_used_for_queue(self):
        api = FakeAPI(dedicated=[dedicated_row("long", A100, 8, 0),
                                 dedicated_row("short", A100, 8, 6)],
                      waiting={"id-long": 5, "id-short": 1})
        _, _, by_name = place(api, [auto_job("j", A100, 4, category="dedicated")])
        assert by_name["j"].candidate.queue_name == "short"
        assert by_name["j"].status == QUEUE

    def test_shared_pool_with_waiting_public_jobs_is_skipped(self):
        api = FakeAPI(shared=[shared_row("busy", A100, 272, 248),
                              shared_row("quiet", A100, 16, 8)],
                      shared_waiting={(PUB, "id-busy"): 7})
        _, _, by_name = place(api, [auto_job("j", A100, 8)])
        assert by_name["j"].candidate.queue_name == "quiet"
        assert by_name["j"].status == START

    def test_public_not_blocked_by_waiting_idle_jobs(self):
        api = FakeAPI(shared=[shared_row("S", A100, 16, 0)],
                      shared_waiting={(IDL, "id-S"): 9})
        _, _, by_name = place(api, [auto_job("j", A100, 8, category="public")])
        assert by_name["j"].status == START

    def test_idle_blocked_by_waiting_public_jobs(self):
        api = FakeAPI(shared=[shared_row("S", A100, 16, 0)],
                      shared_waiting={(PUB, "id-S"): 1})
        _, _, by_name = place(api, [auto_job("j", A100, 8, category="idle")])
        assert by_name["j"].status == QUEUE
        assert by_name["j"].waiting == 1

    def test_idle_blocked_by_waiting_idle_jobs(self):
        api = FakeAPI(shared=[shared_row("S", A100, 16, 0)],
                      shared_waiting={(IDL, "id-S"): 2})
        _, _, by_name = place(api, [auto_job("j", A100, 8, category="idle")])
        assert by_name["j"].status == QUEUE

    def test_waiting_read_failure_falls_back_to_free_cards(self):
        api = FakeAPI(dedicated=[dedicated_row("D", A100, 8, 0)],
                      shared=[shared_row("S", A100, 8, 0)], waiting_error=True)
        jobs = [auto_job("d", A100, 4, category="dedicated"),
                auto_job("s", A100, 4, category="public")]
        _, _, by_name = place(api, jobs)
        assert by_name["d"].status == START and by_name["d"].waiting == 0
        assert by_name["s"].status == START

    def test_waiting_read_once_per_queue_and_projset(self):
        api = FakeAPI(dedicated=[dedicated_row("D", A100, 64, 0),
                                 dedicated_row("other-model", V100, 64, 0)],
                      shared=[shared_row("S", A100, 64, 0)])
        jobs = [auto_job(f"j{i}", A100, 1, category=["dedicated", "public", "idle"])
                for i in range(4)]
        place(api, jobs)
        # Only the queue offering a wanted model is counted, once.
        assert api.waiting_calls == ["id-D"]
        assert api.shared_waiting_calls == 1
