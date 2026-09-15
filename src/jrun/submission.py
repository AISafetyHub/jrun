"""Submission service: experiment resolution and job submission orchestration.

Pure orchestration over ConfigLoader/Project/PlatformClient/PlatformAPI — no
click dependency. Interactive decisions (confirm/choose) and user-facing
messages are injected as callbacks by the command layer, so this module can be
unit-tested directly.
"""

import json
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from jrun.api import PlatformAPI, queue_location_info
from jrun.config import ConfigLoader
from jrun.errors import ApiError, ConfigError, PlatformError
from jrun.models import ExperimentSpec, Job, effective_spec, job_runtime
from jrun.platform import PlatformClient
from jrun.project import Project
from jrun.scheduler import Scheduler, TERMINAL_STATUSES
from jrun.tracking import refresh_active_statuses


@contextmanager
def _plain_progress(items, label="", **kwargs):
    """Default progress display: no bar, just iterate."""
    yield items


@dataclass
class ResolvedExperiment:
    name: str | None
    id: str | None
    queue_locations: dict = field(default_factory=dict)  # queue name -> location
    spec: ExperimentSpec | None = None
    config_count: int | None = None
    # The exact snapshot used for the config-count warning and subsequent
    # full replacement.  Keeping it here prevents a second experiment list
    # request during the same submit operation.
    experiment_data: dict | list | None = None


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


def _lookup_queue(api: PlatformAPI, queue_name: str,
                  spec: ExperimentSpec | None) -> dict:
    """Find a queue by name, with an informative error listing the options."""
    proj_id = spec.proj_id if spec else None
    projset_id = spec.projset_id if spec else None
    queue = api.find_queue(queue_name, proj_id=proj_id, projset_id=projset_id)
    if not queue:
        available = ", ".join(
            sorted(q.get("name", "") for q in api.list_queues(proj_id=proj_id,
                                                              projset_id=projset_id))
        )
        raise ApiError(
            "/api/v1/queue/select",
            message=f"queue '{queue_name}' not found. Available queues: {available}",
        )
    return queue


def resolve_queue_locations(api: PlatformAPI, jobs: list[Job],
                            spec: ExperimentSpec | None) -> dict:
    """Resolve queue/select location info for each distinct effective queue.

    Returns {queue_name: location}. Explicit cluster_id/zone_id in the spec
    override the queue lookup.
    """
    locations: dict[str, dict] = {}
    for job in jobs:
        _, _, queue_name = job_runtime(job, spec)
        if not queue_name or queue_name in locations:
            continue
        location = queue_location_info(
            _lookup_queue(api, queue_name, spec))
        if spec and spec.cluster_id:
            location["cluster_id"] = spec.cluster_id
        if spec and spec.zone_id:
            location["zone_id"] = spec.zone_id
        locations[queue_name] = location
    return locations


def ensure_experiment(spec: ExperimentSpec, platform: PlatformClient,
                      api: PlatformAPI, assume_yes: bool = False, *,
                      confirm, echo=print, image: str | None = None,
                      image_region: str | None = None,
                      queue_name: str | None = None) -> str:
    """Look up the experiment named by spec; create it via the API if missing.

    The creation placeholder config needs an image and a queue: taken from the
    job-level arguments first, then from the deprecated spec fields.
    Returns experiment_id.
    """
    exp_id = None
    result = platform.experiment_list(exp_name=spec.name, check=False)
    if getattr(result, "returncode", 1) == 0 and getattr(result, "stdout", ""):
        try:
            experiment_data = json.loads(result.stdout)
            exp_id = PlatformClient._experiment_id_from_data(experiment_data)
            # A detail response already contains the exact template needed
            # later in this submit.  Cache it so resolving the count does not
            # perform a second `experiment list` call.
            document = PlatformClient._experiment_document(experiment_data)
            if exp_id and document:
                cache = getattr(platform, "_experiment_cache", None)
                if isinstance(cache, dict):
                    cache[exp_id] = document
        except (json.JSONDecodeError, TypeError):
            pass
    if exp_id:
        echo(f"Experiment '{spec.name}' exists (id={exp_id})")
        return exp_id

    image = image or spec.image
    image_region = image_region or spec.image_region
    queue_name = queue_name or spec.queue_name
    if not image or not queue_name:
        raise ConfigError(
            f"cannot create experiment '{spec.name}': no image/queue configured. "
            f"Set image: and resource_config.queue_name: at job level."
        )

    queue = _lookup_queue(api, queue_name, spec)

    if not assume_yes and not confirm(
        f"Experiment '{spec.name}' not found. Create it "
        f"(queue={queue_name}, image={image})?",
        default=True,
    ):
        raise ConfigError("Aborted: experiment creation declined.")
    queue_location = queue_location_info(queue)
    exp_id = api.create_experiment(
        name=spec.name,
        queue=queue,
        image=image,
        proj_id=spec.proj_id or queue_location["proj_id"],
        projset_id=spec.projset_id or queue_location["projset_id"],
        description=spec.description or "",
        image_region=image_region,
    )
    echo(f"Created experiment '{spec.name}' (id={exp_id})")
    return exp_id


def resolve_experiment(loader: ConfigLoader, project: Project,
                       platform: PlatformClient, api: PlatformAPI | None,
                       spec: ExperimentSpec | None, dry_run: bool,
                       assume_yes: bool, *, jobs: list[Job] | None = None,
                       confirm, echo=print) -> ResolvedExperiment:
    """Resolve the target experiment from the config's experiment: block
    (creating it if missing) or the legacy experiment_id/name settings.

    Also resolves queue locations for every distinct effective job queue.
    Raises ConfigError when no experiment can be determined.
    """
    queue_locations: dict = {}
    if spec and (spec.image or spec.queue_name
                 or spec.image_region != ExperimentSpec.DEFAULT_IMAGE_REGION):
        echo("Warning: experiment.image/image_region/queue_name are deprecated; "
             "set image/image_region at job level and the queue via "
             "resource_config.queue_name.")

    if spec and not dry_run:
        image, region, queue_name = job_runtime(jobs[0], spec) if jobs else (None, None, None)
        exp_id = ensure_experiment(spec, platform, api, assume_yes=assume_yes,
                                   confirm=confirm, echo=echo,
                                   image=image, image_region=region,
                                   queue_name=queue_name)
        exp_name = spec.name
    else:
        exp_name = ((spec.name if spec else None)
                    or loader.get_experiment_name() or project.experiment_name)
        exp_id = loader.get_experiment_id() or project.experiment_id

    if not dry_run and not exp_id:
        raise ConfigError("no experiment. Add an 'experiment' section to the config, "
                          "or set experiment_id.")

    config_count = None
    experiment_data = None
    if not dry_run:
        if jobs and any(job_runtime(j, spec)[2] for j in jobs):
            if api is None:
                raise ConfigError(
                    "resolving resource_config.queue_name requires platform API "
                    "credentials (run 'jrun login')."
                )
            queue_locations = resolve_queue_locations(api, jobs, spec)
        # Fetch the required template once, derive the count from the same
        # response, and carry the snapshot into modify_configs.  This makes a
        # successful submit issue exactly one experiment detail request.
        getter = getattr(platform, "get_experiment", None)
        if callable(getter):
            try:
                candidate = getter(exp_id, check=True)
            except TypeError:
                candidate = getter(exp_id)
            if isinstance(candidate, (dict, list)):
                experiment_data = candidate
                config_count = len(PlatformClient._experiment_configs(candidate))

        # Compatibility for older PlatformClient-like adapters and loose test
        # doubles that expose only the pre-split count method.
        if experiment_data is None:
            counter = getattr(platform, "count_experiment_configs", None)
            if callable(counter):
                counted = counter(exp_id)
                config_count = counted if isinstance(counted, int) else None

    return ResolvedExperiment(
        exp_name, exp_id, queue_locations, spec, config_count,
        experiment_data,
    )


def run_local(job: Job, *, echo=print, err_echo=None):
    """Run a job's command locally (queue_name: local)."""
    err_echo = err_echo or echo
    echo(f"[local] {job.name}")
    echo(f"  $ {job.command}")
    result = subprocess.run(job.command, shell=True)
    if result.returncode != 0:
        err_echo(f"  Exit code: {result.returncode}")


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


def teardown_overwrites(project: Project, platform: PlatformClient,
                        overwrite_jobs: list, exp_id: str | None, *,
                        echo=print, progress=_plain_progress):
    """Deprecated compatibility hook.

    Submits now replace the experiment config list in one operation and never
    stop/cancel jobs as part of overwrite.  The old local mapping is removed
    only after its replacement has successfully started, so this legacy hook
    intentionally does nothing.
    """
    return None


def _submit_one(platform: PlatformClient, job: Job, exp_name: str | None,
                exp_id: str | None, spec: ExperimentSpec | None,
                queue_locations: dict) -> str:
    """Run one config that was already installed by ``modify_configs``."""
    run_config = getattr(platform, "run_config", None)
    if not callable(run_config):
        raise PlatformError(
            "job run", 1,
            stderr="platform client has no run_config method",
        )

    job_id = run_config(exp_id, job.name)
    if isinstance(job_id, str) and job_id:
        return job_id
    raise PlatformError("job run", 1, stderr="platform returned no job ID")


def _modify_batch(platform: PlatformClient, jobs: list[Job], exp_id: str | None,
                  spec: ExperimentSpec | None, queue_locations: dict,
                  experiment_data: dict | list | None):
    """Install the complete current working set in one modify call."""
    modify = getattr(platform, "modify_configs", None)
    if not callable(modify):
        raise PlatformError(
            "experiment modify", 1,
            stderr="platform client has no modify_configs method",
        )
    return modify(jobs, exp_id, spec=spec, queue_locations=queue_locations,
                  experiment_data=experiment_data)


def _remove_replaced_ids(project: Project, replacements: dict[str, str],
                         successful_names: set[str],
                         successful_ids: dict[str, str] | None = None):
    """Drop old local IDs only for replacements that actually started."""
    successful_ids = successful_ids or {}
    for name in successful_names:
        old_id = replacements.get(name)
        if old_id and old_id != successful_ids.get(name):
            project.remove_job(old_id)


def submit_jobs(project: Project, platform: PlatformClient,
                search_name: str, config_file: str, submit_list: list[Job],
                exp_name: str | None, exp_id: str | None,
                spec: ExperimentSpec | None, queue_locations: dict,
                sched_params: dict, *,
                echo=print, err_echo=None, progress=_plain_progress,
                replacements: dict[str, str] | None = None,
                experiment_data: dict | list | None = None) -> SubmitResult:
    """Replace configs once, then run each selected job.

    ``experiment_data`` is the snapshot returned by ``resolve_experiment``.
    Passing it through makes the whole submit use one experiment read.  A
    modify failure is fatal before any run or tracker write; individual run
    failures are reported while the remaining jobs continue.
    """
    err_echo = err_echo or echo
    replacements = replacements or {}
    if not submit_list:
        return SubmitResult([])

    # The experiment is the current submission working set, not an append
    # only history.  Do this exactly once, before starting any job.
    _modify_batch(platform, submit_list, exp_id, spec, queue_locations,
                  experiment_data)

    parallel = sched_params["parallel_trials"]

    if parallel and parallel < len(submit_list):
        immediate = submit_list[:parallel]
        pending = submit_list[parallel:]

        job_entries = []
        echo(f"Submitting first {len(immediate)} jobs (parallel_trials={parallel})...")
        successful_names = set()
        successful_ids = {}
        for j in immediate:
            try:
                job_id = _submit_one(platform, j, exp_name, exp_id, spec, queue_locations)
                job_entries.append({"job_id": job_id, "name": j.name, "params": j.params})
                successful_names.add(j.name)
                successful_ids[j.name] = job_id
            except PlatformError as e:
                err_echo(f"  {j.name}: Error: {e}")

        project.record_search(search_name, config_file, job_entries,
                              experiment_id=exp_id, experiment_name=exp_name)
        pending_dicts = [j.to_dict() for j in pending]
        project.record_pending_jobs(search_name, config_file, pending_dicts,
                                    experiment_id=exp_id, experiment_name=exp_name,
                                    replacement_ids={
                                        j.name: replacements[j.name]
                                        for j in pending if j.name in replacements
                                    })
        _remove_replaced_ids(project, replacements, successful_names, successful_ids)

        scheduler = Scheduler(
            project.jrun_dir, search_name, exp_name, exp_id,
            parallel, sched_params["poll_interval"],
        )
        pid = scheduler.start(pending_dicts)
        echo(f"Submitted {len(job_entries)} jobs, {len(pending)} queued")
        echo(f"Scheduler daemon started (pid={pid}, poll={sched_params['poll_interval']}s)")
        echo(f"  Log: {project.jrun_dir / 'scheduler' / f'{search_name}.log'}")
        return SubmitResult(job_entries, pending=len(pending), scheduler_pid=pid)

    job_entries = []
    successful_names = set()
    successful_ids = {}
    with progress(submit_list, label="Submitting",
                  item_show_func=lambda j: j.name if j else "") as bar:
        for j in bar:
            try:
                job_id = _submit_one(platform, j, exp_name, exp_id, spec, queue_locations)
                job_entries.append({"job_id": job_id, "name": j.name, "params": j.params})
                successful_names.add(j.name)
                successful_ids[j.name] = job_id
            except PlatformError as e:
                err_echo(f"\n  {j.name}: Error: {e}")
    project.record_search(search_name, config_file, job_entries,
                          experiment_id=exp_id, experiment_name=exp_name)
    _remove_replaced_ids(project, replacements, successful_names, successful_ids)
    echo(f"\nSubmitted {len(job_entries)} jobs under search '{search_name}'")
    return SubmitResult(job_entries)


def submit_single(project: Project, platform: PlatformClient, job: Job,
                  exp_name: str | None, exp_id: str | None,
                  spec: ExperimentSpec | None, queue_locations: dict,
                  overwrite: bool, status_filter, *,
                  confirm, choose, echo=print,
                  experiment_data: dict | list | None = None) -> str | None:
    """Submit a single job, handling existing-name overwrite/skip.

    Returns the new job ID, or None when skipped/aborted.
    choose: (job, old_id, old_status) -> bool — interactive overwrite decision.
    """
    existing = project.find_job_by_name(job.name)
    if existing:
        old_id, old_info = existing
        if not old_id.startswith("queued-") and old_info.get("status") not in TERMINAL_STATUSES:
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
    echo(f"Submitting {job.name}...")
    # Install the one-job working set first.  The previous local mapping is
    # deliberately untouched until both modify and run succeed.
    _modify_batch(platform, [job], exp_id, spec, queue_locations,
                  experiment_data)
    job_id = _submit_one(platform, job, exp_name, exp_id, spec, queue_locations)
    project.record_single_job(job_id, job.name,
                              experiment_id=exp_id, experiment_name=exp_name)
    if existing and old_id != job_id:
        project.remove_job(old_id)
    echo(f"Submitted → {job_id[:8]}")
    return job_id
