"""The submit command."""

import click

from jrun.api import PlatformAPI
from jrun.commands import echo_json, fail, open_project, progressbar, refresh_statuses
from jrun.config import ConfigLoader
from jrun.errors import ApiError, ConfigError, PlatformError
from jrun.formatter import StatusFormatter
from jrun.models import job_target, queue_category_alias
from jrun.placement import START, STATUS_LABELS, Placement, is_auto_job
from jrun.platform import PlatformClient
from jrun.project import Project
from jrun.remote import RemoteRegistry
from jrun.submission import (CONFIG_COUNT_WARNING, classify_jobs,
                             decide_single, has_auto_jobs, place_jobs,
                             resolve_experiment, run_local, split_remote,
                             submit_jobs, submit_remote, submit_single)
from jrun.tracking import plain_progress, refresh_job_for_display


def register(cli):
    cli.add_command(submit)


class _Ui:
    """Messages and prompts for one submit.

    With ``--json`` stdout carries only the JSON result: messages go to
    stderr, errors are also collected for the result, and nothing prompts
    (confirmations follow ``--yes``; an overwrite prompt means skip).
    """

    def __init__(self, as_json: bool, yes: bool):
        self.as_json = as_json
        self.yes = yes
        self.errors: list[str] = []

    def echo(self, message=""):
        click.echo(message, err=self.as_json)

    def err(self, message):
        self.errors.append(str(message).strip())
        click.echo(message, err=True)

    def confirm(self, prompt, default=False):
        if self.as_json:
            return self.yes
        return click.confirm(prompt, default=default)

    def choose(self, prompt_fn):
        if self.as_json:
            return lambda *args: False
        return prompt_fn

    @property
    def progress(self):
        return plain_progress if self.as_json else progressbar


def _prompt_overwrite(prompt: str) -> bool:
    choice = click.prompt(
        prompt, type=click.Choice(["o", "s"], case_sensitive=False), default="s",
    )
    return choice == "o"


def _choose_overwrite(job, old_id, old_status) -> bool:
    """Interactive [o]verwrite/[s]kip prompt for an existing grid job."""
    click.echo(f"Job '{job.name}' already exists (id={old_id[:8]}, status={old_status})")
    return _prompt_overwrite("  [o]verwrite / [s]kip?")


def _choose_overwrite_single(job, old_id, old_status) -> bool:
    # decide_single already printed the "already exists" line.
    return _prompt_overwrite("[o]verwrite / [s]kip?")


def _show_submitted_jobs(project: Project, platform: PlatformClient,
                         formatter: StatusFormatter, job_ids: list):
    tracker = project.load_tracker()
    jobs = {}
    for jid in job_ids:
        info = tracker.get("jobs", {}).get(jid)
        if info:
            refresh_job_for_display(project, platform, jid, info)
            jobs[jid] = info
    if jobs:
        click.echo()
        formatter.print_job_table(jobs)


def _echo_job_details(echo, job, spec, route=None):
    echo(f"  $ {job.command}")
    target = job_target(job, spec)
    if route:
        echo(f"  Route: {route}")
    if target.image:
        region = f" ({target.image_region})" if target.image_region else ""
        echo(f"  Image: {target.image}{region}")
    categories = "/".join(queue_category_alias(c) for c in target.categories)
    if target.is_auto:
        pairs = ", ".join(f"{m} x{c}" for m, c in job.resource_config.accelerator_choices())
        echo(f"  Queue: auto ({categories}; {pairs})")
    elif target.queue_name:
        echo(f"  Queue: {target.queue_name} ({categories})")
    if target.project_set_name or target.project_name or target.region_name or target.zone_name:
        echo(
            f"  Location: project_set={target.project_set_name or '-'} "
            f"project={target.project_name or '-'} region={target.region_name or '-'} "
            f"zone={target.zone_name or '-'}"
        )
    if job.run_options.to_dict():
        echo(f"  Run options: {job.run_options.to_dict()}")
    if job.envs:
        echo(f"  Envs: {job.envs}")
    if job.resource_config.roles:
        roles = [r.to_dict() for r in job.resource_config.roles]
        echo(f"  Roles: {roles}")


def _echo_placement(echo, placement: Placement | None):
    """Print where each queue_name: auto job was placed."""
    if not placement or not placement.assignments:
        return
    counts = placement.counts()
    echo(f"Auto placement: {counts['start']} start now, {counts['queue']} queued, "
         f"{counts['wait']} wait for a public/idle slot")
    for a in placement.assignments:
        location = a.candidate.location
        where = "/".join(x for x in (location.get("region_name"), location.get("zone_name")) if x)
        ahead = f" ({a.waiting} waiting ahead)" if a.status != START and a.waiting else ""
        echo(
            f"  {a.job.name}: {queue_category_alias(a.candidate.category)} "
            f"{a.candidate.queue_name}{f' ({where})' if where else ''} "
            f"{a.model} x{a.count} - {STATUS_LABELS[a.status]}{ahead}"
        )


def _routes(jobs, remote_jobs, remote) -> dict[str, str]:
    remote_names = {j.name for j in remote_jobs}
    return {j.name: (f"remote ({remote.url})" if j.name in remote_names else "local")
            for j in jobs}


def _dry_run(ui: _Ui, loader: ConfigLoader, jobs, spec, placement: Placement | None,
             routes: dict[str, str]):
    echo = ui.echo
    if spec:
        echo(f"Experiment: {spec.name} (will be created if missing)")
    elif not jobs[0].is_local:
        click.echo("Warning: no 'experiment:' section; a real submit will fail.", err=True)
    _echo_placement(echo, placement)
    if placement and placement.assignments:
        echo("(placement is a snapshot; a real submit places again with current capacity)")
    show_route = any(r != "local" for r in routes.values())
    if not loader.is_search:
        echo(f"Job: {jobs[0].name}")
        _echo_job_details(echo, jobs[0], spec, routes[jobs[0].name] if show_route else None)
        if (jobs[0].name in (placement.deferred if placement else set())
                and routes[jobs[0].name] == "local"):
            click.echo("Warning: no public/idle submit slot now; a real submit would fail.",
                       err=True)
        echo("\n(dry run, nothing submitted)")
        return
    echo(f"Search: {loader.get_search_name()} ({len(jobs)} jobs)")
    for i, j in enumerate(jobs, 1):
        echo(f"\n--- Job {i}: {j.name} ---")
        echo(f"  Params: {j.params}")
        _echo_job_details(echo, j, spec, routes[j.name] if show_route else None)
    echo(f"\nTotal: {len(jobs)} jobs (dry run, nothing submitted)")


def _auto_choices(jobs, spec) -> dict[str, list]:
    """Accelerator choices of queue_name: auto jobs, taken before placement
    rewrites them into concrete jobs."""
    return {j.name: [list(c) for c in j.resource_config.accelerator_choices()]
            for j in jobs if is_auto_job(j, spec)}


def _job_row(job, spec, placement: Placement | None, route: str, auto: dict) -> dict:
    """Planned-job fields shared by the dry-run and real --json results."""
    target = job_target(job, spec)
    row = {
        "name": job.name,
        "params": job.params,
        "route": "remote" if route.startswith("remote") else "local",
        "auto": job.name in auto,
        "accelerator_choices": auto.get(job.name) or [
            list(c) for c in job.resource_config.accelerator_choices()],
        "queue_name": target.queue_name,
        "queue_category": queue_category_alias(target.category),
        "project_set_name": target.project_set_name,
        "project_name": target.project_name,
        "region_name": target.region_name,
        "zone_name": target.zone_name,
        "placement": None,
    }
    for a in (placement.assignments if placement else []):
        if a.job.name == job.name:
            row["placement"] = {"status": a.status, "queue_name": a.candidate.queue_name,
                                "queue_category": queue_category_alias(a.candidate.category),
                                "model": a.model, "count": a.count}
    return row


_TRACKED_KEYS = ("job_id", "status", "search_name", "experiment_id", "experiment_name",
                 "platform", "remote", "submitted_at")


def _json_result(ui: _Ui, loader, spec, jobs, placement, routes, *, dry_run, auto=None,
                 project=None, before_ids=None, local_experiment=None) -> dict:
    """The --json result: planned jobs, plus tracker fields for new IDs."""
    rows = {j.name: _job_row(j, spec, placement, routes[j.name], auto or {}) for j in jobs}
    if not dry_run:
        tracker = project.load_tracker()
        for jid, info in tracker.get("jobs", {}).items():
            if jid in before_ids or info.get("name") not in rows:
                continue
            row = rows[info["name"]]
            row.update({k: info.get(k) for k in _TRACKED_KEYS if k != "job_id"})
            row["job_id"] = jid
    submitted = [r for r in rows.values() if r.get("job_id")]
    return {
        "dry_run": dry_run,
        "search_name": loader.get_search_name() if loader.is_search else None,
        "experiment": local_experiment or ({"name": spec.name, "id": None} if spec else None),
        "jobs": list(rows.values()) if dry_run else submitted,
        "not_submitted": [] if dry_run else [n for n, r in rows.items() if not r.get("job_id")],
        "errors": ui.errors,
    }


@click.command()
@click.argument("config_file")
@click.option("--dry-run", is_flag=True, help="Print jobs without submitting")
@click.option("--overwrite", "-o", is_flag=True, help="Auto-overwrite existing jobs matching --status")
@click.option("--status", "-s", multiple=True, help="Only overwrite jobs with this status (can be repeated)")
@click.option("--yes", "-y", is_flag=True, help="Auto-create the experiment without confirmation")
@click.option("--only", multiple=True, metavar="NAME",
              help="Only submit the job with this name (can be repeated)")
@click.option("--json", "as_json", is_flag=True,
              help="Print the result as JSON on stdout (no prompts; messages on stderr)")
def submit(config_file, dry_run, overwrite, status, yes, only, as_json):
    """Submit jobs from a config file.

    Overwriting an existing job stops the old platform job first. With a
    top-level remote: block, jobs of the listed categories (public by
    default) are submitted through that jrun service instead.
    """
    ui = _Ui(as_json, yes)
    try:
        loader = ConfigLoader(config_file)
        jobs = loader.select(loader.jobs(), only)
        spec = loader.get_experiment_spec()
        remote = loader.get_remote()
    except ConfigError as e:
        fail(e)

    # Local debug runs before any platform interaction.
    if not dry_run and jobs[0].is_local:
        if loader.is_search:
            ui.echo("Local debug: running first job only")
        code = run_local(jobs[0], echo=ui.echo, err_echo=lambda m: click.echo(m, err=True))
        raise SystemExit(code)

    project = open_project()
    platform = PlatformClient()
    remotes = RemoteRegistry()
    if dry_run:
        try:
            # Only auto placement needs the API in a dry run (read-only).
            api = PlatformAPI() if has_auto_jobs(jobs, spec) else None
            auto = _auto_choices(jobs, spec)
            placement, _ = place_jobs(api, jobs, spec, dry_run=True, echo=ui.echo)
            _, remote_jobs = split_remote(jobs, spec, remote)
        except (ApiError, ConfigError, PlatformError) as e:
            fail(e)
        routes = _routes(jobs, remote_jobs, remote)
        if as_json:
            echo_json(_json_result(ui, loader, spec, jobs, placement, routes, dry_run=True,
                                   auto=auto))
            return
        _dry_run(ui, loader, jobs, spec, placement, routes)
        return

    status_filter = set(status) if status else None
    search_name = loader.get_search_name() if loader.is_search else None
    if loader.is_search:
        ui.echo(f"Search: {search_name} ({len(jobs)} jobs)")
        selection = _select_search_jobs(ui, project, platform, remotes, search_name, jobs,
                                        overwrite, status_filter)
        if selection is None:
            if as_json:
                echo_json(_json_result(ui, loader, spec, [], None, {}, dry_run=False,
                                       project=project, before_ids=set()))
            return
        submit_list, replacements = selection
    else:
        tracker = project.load_tracker()
        existing = project.find_job_by_name(jobs[0].name)
        if existing:
            refresh_statuses(project, platform, tracker, job_ids=[existing[0]],
                             remotes=remotes, as_json=as_json)
        submit_list, replacements = jobs, {}

    before_ids = set(project.load_tracker().get("jobs", {}))
    try:
        api = PlatformAPI()
        auto = _auto_choices(submit_list, spec)
        # Placement first: a job's route depends on the category it lands on.
        placement, known_queues = place_jobs(api, submit_list, spec, dry_run=False,
                                             echo=ui.echo)
        local_jobs, remote_jobs = split_remote(submit_list, spec, remote)
        if remote_jobs and spec is None:
            raise ConfigError("config has no 'experiment:' section; add one with at least 'name:'.")
    except (ApiError, ConfigError, PlatformError) as e:
        fail(e)
    routes = _routes(submit_list, remote_jobs, remote)
    _echo_placement(ui.echo, placement)
    if remote_jobs:
        ui.echo(f"Routing: {len(local_jobs)} local, {len(remote_jobs)} through {remote.url}")

    local_experiment = None
    if local_jobs:
        local_experiment = _submit_local(ui, project, platform, api, loader, spec, config_file,
                                         search_name, local_jobs, replacements,
                                         (placement, known_queues), overwrite, status_filter)
    if remote_jobs:
        _submit_remote(ui, project, platform, remotes, remote, loader, config_file, search_name,
                       remote_jobs, replacements, overwrite, status_filter)

    if as_json:
        echo_json(_json_result(ui, loader, spec, submit_list, placement, routes,
                               dry_run=False, auto=auto, project=project, before_ids=before_ids,
                               local_experiment=local_experiment))
        return
    new_ids = [jid for jid in project.load_tracker().get("jobs", {}) if jid not in before_ids]
    if new_ids:
        _show_submitted_jobs(project, platform, StatusFormatter(), new_ids)


def _submit_local(ui, project, platform, api, loader, spec, config_file, search_name,
                  jobs, replacements, placed, overwrite, status_filter) -> dict | None:
    """The existing local flow for the jobs not routed to a remote service."""
    try:
        resolved = resolve_experiment(platform, api, spec, jobs, dry_run=False,
                                      assume_yes=ui.yes, confirm=ui.confirm,
                                      echo=ui.echo, placed=placed)
    except (ApiError, ConfigError, PlatformError) as e:
        fail(e)
    experiment = {"name": resolved.name, "id": resolved.id}

    if resolved.config_count is not None and resolved.config_count >= CONFIG_COUNT_WARNING:
        click.echo(
            f"Warning: experiment '{resolved.name}' already has "
            f"{resolved.config_count} configs. "
            "Too many configs may cause platform performance issues.",
            err=True,
        )
        if not ui.confirm("Continue?", default=False):
            ui.echo("Aborted.")
            return experiment

    if not loader.is_search:
        try:
            submit_single(project, platform, resolved, jobs[0],
                          overwrite=overwrite, status_filter=status_filter,
                          confirm=ui.confirm, choose=ui.choose(_choose_overwrite_single),
                          echo=ui.echo)
        except (ConfigError, PlatformError) as e:
            fail(e)
        return experiment

    # Stop replaced runs, install the working set once, run.
    try:
        submit_jobs(project, platform, resolved, search_name, config_file,
                    jobs, loader.get_scheduler_params(),
                    replacements={j.name: replacements[j.name] for j in jobs
                                  if j.name in replacements},
                    echo=ui.echo, err_echo=ui.err, progress=ui.progress)
    except PlatformError as e:
        fail(e)
    return experiment


def _submit_remote(ui, project, platform, remotes, remote, loader, config_file, search_name,
                   jobs, replacements, overwrite, status_filter):
    """Send the routed jobs to the remote service and track what it started."""
    if not loader.is_search:
        proceed, existing = decide_single(
            project, platform, jobs[0], overwrite=overwrite, status_filter=status_filter,
            confirm=ui.confirm, choose=ui.choose(_choose_overwrite_single), echo=ui.echo)
        if not proceed:
            return
        replacements = {jobs[0].name: existing[0]} if existing else {}
    try:
        submit_remote(project, platform, remote.url, loader.config, search_name, config_file,
                      jobs, replacements={j.name: replacements[j.name] for j in jobs
                                          if j.name in replacements},
                      remotes=remotes, echo=ui.echo, err_echo=ui.err)
    except (ApiError, PlatformError) as e:
        if loader.is_search:
            ui.err(f"Remote submit failed: {e}")
            return
        fail(e)


def _select_search_jobs(ui, project, platform, remotes, search_name, jobs, overwrite,
                        status_filter):
    """Classify grid jobs against the tracker and confirm overwrites.

    Returns ``(submit_list, replacements)`` or None when nothing is submitted.
    """
    tracker = project.load_tracker()
    search_job_ids = list(tracker.get("searches", {}).get(search_name, {}).get("job_ids", []))
    refresh_statuses(project, platform, tracker, job_ids=search_job_ids, remotes=remotes,
                     as_json=ui.as_json)
    classified = classify_jobs(jobs, project.find_job_by_name, overwrite,
                               status_filter, choose=ui.choose(_choose_overwrite))

    if classified.new:
        ui.echo(f"  New: {len(classified.new)} jobs")
    if classified.overwrite:
        ui.echo(f"  Overwrite: {len(classified.overwrite)} jobs (old runs will be stopped)")
        for j, old_id, old_status in classified.overwrite:
            ui.echo(f"    {j.name} (id={old_id[:8]}, status={old_status})")
    if classified.skipped:
        ui.echo(f"  Skip: {len(classified.skipped)} jobs")

    total = len(classified.new) + len(classified.overwrite)
    if total == 0:
        ui.echo("Nothing to submit.")
        return None
    if overwrite and classified.overwrite and not ui.as_json:
        if not click.confirm(f"Proceed with {total} jobs?", default=True):
            ui.echo("Aborted.")
            return None
    submit_list = classified.new + [j for j, _, _ in classified.overwrite]
    replacements = {j.name: old_id for j, old_id, _ in classified.overwrite}
    return submit_list, replacements
