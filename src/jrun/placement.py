"""Automatic queue placement for ``resource_config.queue_name: auto``.

An auto job lists acceptable (accelerator_model, accelerator_count) pairs and
queue categories, both in preference order. At submit time jrun reads every
candidate queue's free cards and the project set's public/idle submit budget,
then turns each auto job into an ordinary concrete job (queue, category,
model, count, location). Everything downstream (queue resolution, experiment
creation, modify/run, the scheduler) only ever sees concrete jobs.

Placement order:

1. **start** - in job order, the first candidate (category preference, then
   pair preference, then most free cards) that has enough free cards and, for
   public/idle, a remaining submit slot. Free cards and slots are consumed.
2. **queue** - jobs that did not fit: a dedicated candidate if dedicated is
   allowed (dedicated accepts queued submissions), else a public/idle
   candidate that still has a submit slot. They wait on the platform.
3. **wait** - no slot anywhere: installed on the preferred candidate and run
   later by the scheduler once a public/idle slot frees up.

Public and idle list the same physical pools, so free cards are tracked per
pool (queue ID + model) while submit budgets are tracked per mode.
"""

from dataclasses import dataclass, field

from jrun.api import (PlatformAPI, filter_queues, queue_location_info,
                      with_project_context)
from jrun.errors import ConfigError
from jrun.models import (DEDICATED, SHARED_CATEGORIES, ExperimentSpec, Job,
                         JobTarget, job_target, queue_category_alias,
                         total_replicas)

START, QUEUE, WAIT = "start", "queue", "wait"
STATUS_LABELS = {
    START: "free cards, starts now",
    QUEUE: "queued on the platform",
    WAIT: "waits for a public/idle slot (scheduler)",
}

PLACEMENT_ENVS = ("JRUN_QUEUE_NAME", "JRUN_ACCELERATOR_MODEL", "JRUN_ACCELERATOR_COUNT")


@dataclass(frozen=True)
class Candidate:
    """One accelerator model offered by one queue in one category."""

    category: str
    queue: dict = field(hash=False, compare=False)  # row with project context
    model: str = ""
    total: float = 0
    pool_key: tuple = ()              # (queue_id, model): shared by public/idle
    budget_key: tuple | None = None   # (projset_id, category) for public/idle

    @property
    def queue_name(self) -> str:
        return self.queue.get("name", "")

    @property
    def location(self) -> dict:
        return queue_location_info(self.queue)


@dataclass
class Assignment:
    job: Job
    candidate: Candidate
    model: str
    count: int
    cards: int        # count x replicas
    status: str       # START / QUEUE / WAIT


@dataclass
class Placement:
    assignments: list[Assignment] = field(default_factory=list)

    @property
    def deferred(self) -> set[str]:
        """Names of jobs that must wait for a public/idle submit slot."""
        return {a.job.name for a in self.assignments if a.status == WAIT}

    def counts(self) -> dict[str, int]:
        result = {START: 0, QUEUE: 0, WAIT: 0}
        for a in self.assignments:
            result[a.status] += 1
        return result


@dataclass
class _Request:
    job: Job
    target: JobTarget
    choices: list[tuple[str, int]]
    replicas: int
    candidates: list[Candidate]


# --- candidate collection -----------------------------------------------------

def _context_key(target: JobTarget) -> tuple:
    return (target.project_set_name, target.project_name)


def collect_candidates(api: PlatformAPI, targets: list[JobTarget]):
    """Query queues and budgets once per (project context, category).

    Returns ``(candidates_per_target, free, budgets)``: a list of candidates
    for each target (location-filtered), free cards per pool_key, and remaining
    submit slots per budget_key (None = unlimited).
    """
    contexts: dict[tuple, tuple] = {}
    rows_cache: dict[tuple, list[dict]] = {}
    free: dict[tuple, float] = {}
    budgets: dict[tuple, int | None] = {}
    per_target = []
    for target in targets:
        key = _context_key(target)
        if key not in contexts:
            try:
                contexts[key] = api.resolve_project_context(*key)
            except ValueError as exc:
                raise ConfigError(str(exc)) from exc
        proj_id, projset_id = contexts[key]
        candidates = []
        for category in target.categories:
            cache_key = key + (category,)
            if cache_key not in rows_cache:
                rows_cache[cache_key] = api.list_category_queues(
                    category, proj_id=proj_id, projset_id=projset_id)
            budget_key = None
            if category in SHARED_CATEGORIES:
                budget_key = (projset_id or "", category)
                if budget_key not in budgets:
                    remaining = api.shared_queue_budgets(proj_id=proj_id, projset_id=projset_id)
                    for cat, value in remaining.items():
                        budgets[(projset_id or "", cat)] = value
                    budgets.setdefault(budget_key, None)
            rows = filter_queues(rows_cache[cache_key], target.project_set_name,
                                 target.project_name, target.region_name, target.zone_name)
            for row in rows:
                queue = with_project_context(row, proj_id, projset_id,
                                             target.project_name, target.project_set_name)
                queue_id = queue.get("id") or queue.get("queueId") or queue.get("name")
                for res in PlatformAPI.queue_free_resources(queue):
                    if not res.get("model"):
                        continue
                    pool_key = (queue_id, res["model"])
                    free.setdefault(pool_key, max(res.get("free") or 0, 0))
                    candidates.append(Candidate(
                        category=category, queue=queue, model=res["model"],
                        total=res.get("total") or 0, pool_key=pool_key,
                        budget_key=budget_key,
                    ))
        per_target.append(candidates)
    return per_target, free, budgets


# --- planning (pure) ----------------------------------------------------------

def _options(request: _Request, free: dict):
    """Sorted (category_rank, choice_rank, candidate, model, count, cards) tuples.

    Order: category preference, pair preference, most free cards, queue name.
    Candidates whose total capacity is below the job's cards are dropped.
    """
    options = []
    for cat_rank, category in enumerate(request.target.categories):
        for choice_rank, (model, count) in enumerate(request.choices):
            cards = count * request.replicas
            for cand in request.candidates:
                if cand.category != category or cand.model.lower() != str(model).lower():
                    continue
                if cand.total < cards:
                    continue  # can never run there
                options.append((cat_rank, choice_rank, cand, cand.model, count, cards))
    options.sort(key=lambda o: (o[0], o[1], -free.get(o[2].pool_key, 0), o[2].queue_name))
    return options


def _no_candidate_error(request: _Request) -> ConfigError:
    models = sorted({c.model for c in request.candidates})
    categories = "/".join(queue_category_alias(c) for c in request.target.categories)
    wanted = ", ".join(f"{m} x{c}" for m, c in request.choices)
    replicas = f" per replica, {request.replicas} replicas" if request.replicas > 1 else ""
    return ConfigError(
        f"job '{request.job.name}': no {categories} queue offers {wanted}{replicas}. "
        f"Available models: {', '.join(models) or '(none)'}"
    )


def plan_placement(requests: list[_Request], free: dict, budgets: dict) -> Placement:
    """Assign every request to a candidate (inputs are not mutated)."""
    free = dict(free)
    budgets = dict(budgets)

    def has_slot(cand: Candidate) -> bool:
        return cand.budget_key is None or budgets.get(cand.budget_key) is None \
            or budgets[cand.budget_key] > 0

    def take_slot(cand: Candidate):
        if cand.budget_key is not None and budgets.get(cand.budget_key) is not None:
            budgets[cand.budget_key] -= 1

    for request in requests:
        if not _options(request, free):
            raise _no_candidate_error(request)

    chosen: dict[str, Assignment] = {}
    for request in requests:
        # Options are re-sorted with the current free counts so that
        # consecutive jobs spread over pools.
        for _, _, cand, model, count, cards in _options(request, free):
            if free.get(cand.pool_key, 0) >= cards and has_slot(cand):
                free[cand.pool_key] = free.get(cand.pool_key, 0) - cards
                take_slot(cand)
                chosen[request.job.name] = Assignment(request.job, cand, model, count,
                                                      cards, START)
                break

    for request in requests:
        if request.job.name in chosen:
            continue
        options = _options(request, free)
        dedicated = [o for o in options if o[2].category == DEDICATED]
        if dedicated:
            # Dedicated accepts queued submissions: pair preference, then the
            # queue with the most free cards.
            _, _, cand, model, count, cards = min(
                dedicated, key=lambda o: (o[1], -free.get(o[2].pool_key, 0), o[2].queue_name))
            status = QUEUE
        else:
            with_slot = [o for o in options if has_slot(o[2])]
            if with_slot:
                _, _, cand, model, count, cards = with_slot[0]
                take_slot(cand)
                status = QUEUE
            else:
                _, _, cand, model, count, cards = options[0]
                status = WAIT
        chosen[request.job.name] = Assignment(request.job, cand, model, count, cards, status)

    return Placement([chosen[r.job.name] for r in requests])


# --- applying -------------------------------------------------------------------

def apply_placement(assignment: Assignment):
    """Rewrite the job into a concrete job on the chosen queue."""
    rc = assignment.job.resource_config
    cand = assignment.candidate
    location = cand.location
    rc.queue_name = cand.queue_name
    rc.queue_category = queue_category_alias(cand.category)
    rc.accelerator_model = cand.model
    rc.accelerator_count = assignment.count
    rc.region_name = location.get("region_name") or rc.region_name
    rc.zone_name = location.get("zone_name") or rc.zone_name
    rc.project_set_name = rc.project_set_name or location.get("project_set_name") or None
    rc.project_name = rc.project_name or location.get("project_name") or None
    assignment.job.envs.update({
        "JRUN_QUEUE_NAME": cand.queue_name,
        "JRUN_ACCELERATOR_MODEL": cand.model,
        "JRUN_ACCELERATOR_COUNT": str(assignment.count),
    })


def is_auto_job(job: Job, spec: ExperimentSpec | None) -> bool:
    return job_target(job, spec).is_auto


def place_auto_jobs(api: PlatformAPI, jobs: list[Job],
                    spec: ExperimentSpec | None) -> tuple[Placement, dict]:
    """Place every auto job and rewrite it in place.

    Returns ``(placement, queues)`` where ``queues`` maps each placed job's
    ``JobTarget.location_key`` to its queue row, so the regular queue
    resolution does not have to look these queues up again.
    """
    auto_jobs = [j for j in jobs if is_auto_job(j, spec)]
    if not auto_jobs:
        return Placement(), {}
    targets = [job_target(j, spec) for j in auto_jobs]
    per_target, free, budgets = collect_candidates(api, targets)
    requests = [
        _Request(job, target, job.resource_config.accelerator_choices(),
                 total_replicas(job.resource_config), candidates)
        for job, target, candidates in zip(auto_jobs, targets, per_target)
    ]
    placement = plan_placement(requests, free, budgets)
    queues = {}
    for assignment in placement.assignments:
        apply_placement(assignment)
        queues[job_target(assignment.job, spec).location_key] = assignment.candidate.queue
    return placement, queues
