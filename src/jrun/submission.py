"""Submission service: experiment resolution and job submission orchestration.

Pure orchestration over ConfigLoader/Project/PlatformClient/PlatformAPI — no
click dependency. Interactive decisions (confirm/choose) and user-facing
messages are injected as callbacks by the command layer, so this module can be
unit-tested directly.
"""

import subprocess
from dataclasses import dataclass, field

from jrun.api import PlatformAPI, queue_location_info
from jrun.errors import ApiError, ConfigError, PlatformError
from jrun.models import (DEFAULT_IMAGE_REGION, SHARED_CATEGORIES, ExperimentSpec,
                         Job, JobTarget, TERMINAL_STATUSES, job_target,
                         queue_category_alias, validate_job)
from jrun.placement import Placement, is_auto_job, place_auto_jobs
from jrun.platform import PlatformClient
from jrun.project import Project
from jrun.scheduler import Scheduler
from jrun.tracking import is_queued_id, plain_progress, release_job

CONFIG_COUNT_WARNING = 100


@dataclass
class ResolvedExperiment:
    spec: ExperimentSpec | None
    id: str | None = None
    queue_locations: dict = field(default_factory=dict)  # JobTarget.location_key -> location
    # The experiment detail read once per submit: used for the config-count
    # warning and as the template for the single full-replacement modify.
    experiment_data: dict | None = None
    # Auto-placement result (queue_name: auto jobs); None when there were none.
    placement: Placement | None = None

    @property
    def deferred(self) -> set[str]:
        """Jobs that must wait for a public/idle submit slot."""
        return self.placement.deferred if self.placement else set()

    @property
    def name(self) -> str | None:
        return self.spec.name if self.spec else None

    @property
    def config_count(self) -> int | None:
        if self.experiment_data is None:
            return None
        return len(PlatformClient.experiment_configs(self.experiment_data))


@dataclass
class JobClassification:
    new: list = field(default_factory=list)
    overwrite: list = field(default_factory=list)  # (job, old_id, old_status)
    skipped: list = field(default_factory=list)    # (job, old_id, old_status)


@dataclass
class SubmitResult:
    job_entries: list = field(default_factory=list)
    pending: int = 0
    scheduler_pid: int | None = None


# --- queues & experiments ---------------------------------------------------

def find_queue(api: PlatformAPI, target: JobTarget) -> dict:
    """Resolve a job target's named queue to its platform row.

    Raises ConfigError when the queue is ambiguous or not found (listing the
    queues that are available in that category).
    """
    try:
        queue = api.find_queue(
            target.queue_name, queue_category=target.category,
            project_set_name=target.project_set_name,
            project_name=target.project_name,
            region_name=target.region_name, zone_name=target.zone_name,
        )
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
    if queue:
        return queue
    if target.category in SHARED_CATEGORIES:
        candidates = api.list_shared_queues(queue_category=target.category)
    else:
        candidates = api.list_queues()
    available = ", ".join(sorted({q.get("name", "") for q in candidates})) or "(none)"
    raise ConfigError(
        f"{queue_category_alias(target.category)} queue '{target.queue_name}' "
        f"not found. Available: {available}"
    )


def resolve_queue_locations(api: PlatformAPI, jobs: list[Job],
                            spec: ExperimentSpec | None,
                            known_queues: dict | None = None) -> dict:
    """Resolve each distinct effective queue once; keyed by location_key.

    ``known_queues`` (location_key -> queue row, from auto placement) skips
    the lookup for queues that were already read.
    """
    known_queues = known_queues or {}
    locations: dict[str, dict] = {}
    for job in jobs:
        target = job_target(job, spec)
        if not target.queue_name or target.location_key in locations:
            continue
        queue = known_queues.get(target.location_key) or find_queue(api, target)
        location = queue_location_info(queue)
        location["queue_category"] = target.category
        for key in ("project_set_name", "project_name", "region_name", "zone_name"):
            location[key] = location.get(key) or getattr(target, key) or ""
        locations[target.location_key] = location
    return locations


def ensure_experiment(spec: ExperimentSpec, platform: PlatformClient,
                      api: PlatformAPI, target: JobTarget, *,
                      assume_yes: bool = False, confirm, echo=print) -> str:
    """Look up the experiment named by spec; create it via the API if missing.

    ``target`` (usually the first job's) supplies the image and queue for the
    creation placeholder config. Returns experiment_id.
    """
    existing = platform.find_experiment(spec.name)
    if existing:
        echo(f"Experiment '{spec.name}' exists (id={existing['experiment_id']})")
        return existing["experiment_id"]

    if not target.image or not target.queue_name:
        raise ConfigError(
            f"cannot create experiment '{spec.name}': no image/queue configured. "
            "Set image: and resource_config.queue_name: (plus optional "
            "project_set_name/project_name/region_name/zone_name) at job or "
            "experiment level."
        )
    queue = find_queue(api, target)
    if not assume_yes and not confirm(
        f"Experiment '{spec.name}' not found. Create it "
        f"(queue={target.queue_name}, image={target.image})?",
        default=True,
    ):
        raise ConfigError("Aborted: experiment creation declined.")
    location = queue_location_info(queue)
    exp_id = api.create_experiment(
        name=spec.name,
        queue=queue,
        image=target.image,
        proj_id=location["proj_id"],
        projset_id=location["projset_id"],
        description=spec.description or "",
        image_region=target.image_region or DEFAULT_IMAGE_REGION,
        queue_category=target.category,
        train_frame=spec.train_frame or "PyTorch",
        restart_policy=spec.restart_policy or "Never",
        storage_info=spec.storage_info,
        platform_overrides=spec.platform_overrides,
    )
    echo(f"Created experiment '{spec.name}' (id={exp_id})")
    return exp_id


def validate_jobs(jobs: list[Job], spec: ExperimentSpec | None):
    """Check each job's effective queue settings and run_options (no API)."""
    for job in jobs:
        try:
            validate_job(job, spec)
        except ValueError as exc:
            raise ConfigError(f"job '{job.name}': {exc}") from exc


def has_auto_jobs(jobs: list[Job], spec: ExperimentSpec | None) -> bool:
    return any(is_auto_job(j, spec) for j in jobs)


def resolve_experiment(platform: PlatformClient, api: PlatformAPI | None,
                       spec: ExperimentSpec | None, jobs: list[Job], *,
                       dry_run: bool, assume_yes: bool = False,
                       confirm, echo=print) -> ResolvedExperiment:
    """Find or create the experiment from the config's ``experiment:`` block.

    ``jobs`` are the jobs about to be submitted. ``queue_name: auto`` jobs are
    placed first (rewritten into concrete jobs), so the experiment can be
    created from a real queue. Then queue locations are resolved for every
    distinct effective job queue and the experiment detail is read once.

    In dry-run mode nothing is created; auto jobs are still placed when an
    API client is given (read-only queries), and an API failure only warns.
    """
    validate_jobs(jobs, spec)
    if dry_run:
        placement = None
        if api is not None and has_auto_jobs(jobs, spec):
            try:
                placement, _ = place_auto_jobs(api, jobs, spec)
            except ApiError as e:
                echo(f"Warning: cannot read queue capacity for auto placement: {e}")
        return ResolvedExperiment(spec, placement=placement)
    if spec is None:
        raise ConfigError(
            "config has no 'experiment:' section; add one with at least 'name:'."
        )
    if api is None:
        raise ConfigError("submitting requires platform API credentials (run 'jrun login').")

    placement, known_queues = place_auto_jobs(api, jobs, spec)
    exp_id = ensure_experiment(spec, platform, api, job_target(jobs[0], spec),
                               assume_yes=assume_yes, confirm=confirm, echo=echo)
    queue_locations = resolve_queue_locations(api, jobs, spec, known_queues)
    experiment_data = platform.get_experiment(exp_id, check=True)
    return ResolvedExperiment(spec, exp_id, queue_locations, experiment_data,
                              placement=placement if placement.assignments else None)


# --- local runs ---------------------------------------------------------------

def run_local(job: Job, *, echo=print, err_echo=None) -> int:
    """Run a job's command locally (queue_name: local). Returns the exit code."""
    err_echo = err_echo or echo
    echo(f"[local] {job.name}")
    echo(f"  $ {job.command}")
    result = subprocess.run(job.command, shell=True)
    if result.returncode != 0:
        err_echo(f"  Exit code: {result.returncode}")
    return result.returncode


# --- submission -----------------------------------------------------------------

def classify_jobs(jobs: list[Job], find_existing, overwrite: bool,
                  status_filter, *, choose) -> JobClassification:
    """Split grid jobs into new/overwrite/skipped against the tracker.

    find_existing: name -> (old_id, old_info) | None  (Project.find_job_by_name)
    choose: (job, old_id, old_status) -> bool — interactive decision callback,
    only called when overwrite is False.
    """
    result = JobClassification()
    for j in jobs:
        existing = find_existing(j.name)
        if not existing:
            result.new.append(j)
            continue
        old_id, old_info = existing
        old_status = old_info.get("status", "?")
        if overwrite:
            if status_filter and old_status not in status_filter:
                result.skipped.append((j, old_id, old_status))
            else:
                result.overwrite.append((j, old_id, old_status))
        elif choose(j, old_id, old_status):
            result.overwrite.append((j, old_id, old_status))
        else:
            result.skipped.append((j, old_id, old_status))
    return result


def _run_one(platform: PlatformClient, job: Job, exp_id: str) -> str:
    """Start one config that was already installed by modify_configs."""
    return platform.run_config(
        exp_id, job.name, run_options=job.run_options,
        queue_category=job.resource_config.queue_category,
    )


def _run_all(platform: PlatformClient, jobs, exp_id: str, err_echo) -> dict[str, str]:
    """Run each job; per-job failures are reported and skipped. name -> job_id."""
    started = {}
    for job in jobs:
        try:
            started[job.name] = _run_one(platform, job, exp_id)
        except PlatformError as e:
            err_echo(f"\n  {job.name}: Error: {e}")
    return started


def _install(platform: PlatformClient, resolved: ResolvedExperiment, jobs: list[Job]):
    """Install exactly ``jobs`` as the experiment's config set (one modify).

    The effective queue category and project names are persisted on each job
    first, so the background scheduler passes the same public/idle mode to
    ``job run`` and checks submit slots in the right project set.
    """
    for job in jobs:
        target = job_target(job, resolved.spec)
        rc = job.resource_config
        rc.queue_category = target.queue_category
        rc.project_set_name = target.project_set_name
        rc.project_name = target.project_name
    platform.modify_configs(jobs, resolved.id, spec=resolved.spec,
                            queue_locations=resolved.queue_locations,
                            experiment_data=resolved.experiment_data)


def _drop_replaced(project: Project, replacements: dict[str, str],
                   started: dict[str, str]):
    """Drop old local IDs only for replacements that actually started."""
    for name, new_id in started.items():
        old_id = replacements.get(name)
        if old_id and old_id != new_id:
            project.remove_job(old_id)


def submit_jobs(project: Project, platform: PlatformClient,
                resolved: ResolvedExperiment, search_name: str, config_file: str,
                jobs: list[Job], sched_params: dict, *,
                replacements: dict[str, str] | None = None,
                echo=print, err_echo=None, progress=plain_progress) -> SubmitResult:
    """Stop replaced jobs, install the configs once, then run each job.

    ``replacements`` maps a job name to the old tracked ID it overwrites. The
    old job is stopped first; if that fails, the job is skipped (reported via
    ``err_echo``) so two runs never compete. A modify failure is fatal before
    any run or tracker write; individual run failures are reported while the
    remaining jobs continue.
    """
    err_echo = err_echo or echo
    replacements = replacements or {}

    tracked = project.load_tracker().get("jobs", {})
    selected = []
    for job in jobs:
        old_id = replacements.get(job.name)
        if old_id:
            try:
                release_job(project, platform, old_id, tracked.get(old_id, {}))
            except PlatformError as e:
                err_echo(f"  {job.name}: could not stop old job {old_id[:8]}, "
                         f"skipped: {e}")
                continue
        selected.append(job)
    if not selected:
        return SubmitResult()

    _install(platform, resolved, selected)

    # Jobs placed with no public/idle submit slot left go straight to the
    # scheduler, which starts them once a slot frees up.
    deferred = resolved.deferred
    ready = [j for j in selected if j.name not in deferred]
    waiting = [j for j in selected if j.name in deferred]
    parallel = sched_params.get("parallel_trials")
    if parallel and parallel < len(ready):
        immediate, pending = ready[:parallel], ready[parallel:]
        echo(f"Submitting first {len(immediate)} jobs (parallel_trials={parallel})...")
    else:
        immediate, pending = ready, []
    if waiting:
        echo(f"{len(waiting)} jobs wait for a public/idle submit slot (scheduler).")
    pending += waiting

    with progress(immediate, label="Submitting",
                  item_show_func=lambda j: j.name if j else "") as bar:
        started = _run_all(platform, bar, resolved.id, err_echo)
    entries = [{"job_id": started[j.name], "name": j.name, "params": j.params}
               for j in immediate if j.name in started]
    project.record_search(search_name, config_file, entries,
                          experiment_id=resolved.id, experiment_name=resolved.name)
    _drop_replaced(project, replacements, started)

    if not pending:
        echo(f"\nSubmitted {len(entries)} jobs under search '{search_name}'")
        return SubmitResult(entries)

    pending_dicts = [j.to_dict() for j in pending]
    project.record_pending_jobs(
        search_name, config_file, pending_dicts,
        experiment_id=resolved.id, experiment_name=resolved.name,
        replacement_ids={j.name: replacements[j.name]
                         for j in pending if j.name in replacements},
    )
    poll = sched_params.get("poll_interval", 30)
    scheduler = Scheduler(project.jrun_dir, search_name, resolved.name,
                          resolved.id, parallel or 0, poll)
    pid = scheduler.start(pending_dicts)
    echo(f"Submitted {len(entries)} jobs, {len(pending)} queued")
    echo(f"Scheduler daemon started (pid={pid}, poll={poll}s)")
    echo(f"  Log: {project.jrun_dir / 'scheduler' / f'{search_name}.log'}")
    return SubmitResult(entries, pending=len(pending), scheduler_pid=pid)


def submit_single(project: Project, platform: PlatformClient,
                  resolved: ResolvedExperiment, job: Job, *,
                  overwrite: bool, status_filter, confirm, choose,
                  echo=print) -> str | None:
    """Submit a single job, handling existing-name overwrite/skip.

    Returns the new job ID, or None when skipped/aborted. Raises
    PlatformError when the old job cannot be stopped or the submit fails, and
    ConfigError when an auto job found no public/idle submit slot.
    choose: (job, old_id, old_status) -> bool — interactive overwrite decision.
    """
    if job.name in resolved.deferred:
        raise ConfigError(
            f"job '{job.name}': no public/idle submit slot left for this project "
            "set (see RUN/LIMIT in 'jrun queues'); add dedicated to "
            "queue_category or submit again when a job finishes."
        )
    existing = project.find_job_by_name(job.name)
    if existing:
        old_id, old_info = existing
        if not is_queued_id(old_id) and old_info.get("status") not in TERMINAL_STATUSES:
            new_status = platform.get_job_status(old_id)
            if new_status:
                old_info["status"] = new_status
                project.update_job_status(old_id, new_status)
        old_status = old_info.get("status", "?")
        if overwrite:
            if status_filter and old_status not in status_filter:
                echo(f"Skipped {job.name} (status={old_status} not in {list(status_filter)}).")
                return None
            echo(f"Will overwrite: {job.name} (id={old_id[:8]}, status={old_status})")
            if not confirm("Proceed?", default=True):
                echo("Aborted.")
                return None
        else:
            echo(f"Job '{job.name}' already exists (id={old_id[:8]}, status={old_status})")
            if not choose(job, old_id, old_status):
                echo("Skipped.")
                return None
        release_job(project, platform, old_id, old_info)

    echo(f"Submitting {job.name}...")
    # The previous local mapping stays until both modify and run succeed.
    _install(platform, resolved, [job])
    job_id = _run_one(platform, job, resolved.id)
    project.record_single_job(job_id, job.name, experiment_id=resolved.id,
                              experiment_name=resolved.name)
    if existing and existing[0] != job_id:
        project.remove_job(existing[0])
    echo(f"Submitted → {job_id[:8]}")
    return job_id
