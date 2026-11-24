"""The submit command."""

import click

from jrun.api import PlatformAPI
from jrun.commands import fail, open_project, progressbar, refresh_statuses
from jrun.config import ConfigLoader
from jrun.errors import ApiError, ConfigError, PlatformError
from jrun.formatter import StatusFormatter
from jrun.models import job_target, queue_category_alias
from jrun.placement import STATUS_LABELS, Placement
from jrun.platform import PlatformClient
from jrun.project import Project
from jrun.submission import (CONFIG_COUNT_WARNING, classify_jobs, has_auto_jobs,
                             resolve_experiment, run_local, submit_jobs,
                             submit_single)
from jrun.tracking import refresh_job_for_display


def register(cli):
    cli.add_command(submit)


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
    # submit_single already printed the "already exists" line.
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


def _echo_job_details(job, spec):
    click.echo(f"  $ {job.command}")
    target = job_target(job, spec)
    if target.image:
        region = f" ({target.image_region})" if target.image_region else ""
        click.echo(f"  Image: {target.image}{region}")
    categories = "/".join(queue_category_alias(c) for c in target.categories)
    if target.is_auto:
        pairs = ", ".join(f"{m} x{c}" for m, c in job.resource_config.accelerator_choices())
        click.echo(f"  Queue: auto ({categories}; {pairs})")
    elif target.queue_name:
        click.echo(f"  Queue: {target.queue_name} ({categories})")
    if target.project_set_name or target.project_name or target.region_name or target.zone_name:
        click.echo(
            f"  Location: project_set={target.project_set_name or '-'} "
            f"project={target.project_name or '-'} region={target.region_name or '-'} "
            f"zone={target.zone_name or '-'}"
        )
    if job.run_options.to_dict():
        click.echo(f"  Run options: {job.run_options.to_dict()}")
    if job.envs:
        click.echo(f"  Envs: {job.envs}")
    if job.resource_config.roles:
        roles = [r.to_dict() for r in job.resource_config.roles]
        click.echo(f"  Roles: {roles}")


def _echo_placement(placement: Placement | None):
    """Print where each queue_name: auto job was placed."""
    if not placement or not placement.assignments:
        return
    counts = placement.counts()
    click.echo(f"Auto placement: {counts['start']} start now, {counts['queue']} queued, "
               f"{counts['wait']} wait for a public/idle slot")
    for a in placement.assignments:
        location = a.candidate.location
        where = "/".join(x for x in (location.get("region_name"), location.get("zone_name")) if x)
        click.echo(
            f"  {a.job.name}: {queue_category_alias(a.candidate.category)} "
            f"{a.candidate.queue_name}{f' ({where})' if where else ''} "
            f"{a.model} x{a.count} - {STATUS_LABELS[a.status]}"
        )


def _dry_run(loader: ConfigLoader, jobs, spec, placement: Placement | None):
    if spec:
        click.echo(f"Experiment: {spec.name} (will be created if missing)")
    elif not jobs[0].is_local:
        click.echo("Warning: no 'experiment:' section; a real submit will fail.", err=True)
    _echo_placement(placement)
    if placement and placement.assignments:
        click.echo("(placement is a snapshot; a real submit places again with current capacity)")
    if not loader.is_search:
        click.echo(f"Job: {jobs[0].name}")
        _echo_job_details(jobs[0], spec)
        if jobs[0].name in (placement.deferred if placement else set()):
            click.echo("Warning: no public/idle submit slot now; a real submit would fail.",
                       err=True)
        click.echo("\n(dry run, nothing submitted)")
        return
    click.echo(f"Search: {loader.get_search_name()} ({len(jobs)} jobs)")
    for i, j in enumerate(jobs, 1):
        click.echo(f"\n--- Job {i}: {j.name} ---")
        click.echo(f"  Params: {j.params}")
        _echo_job_details(j, spec)
    click.echo(f"\nTotal: {len(jobs)} jobs (dry run, nothing submitted)")


@click.command()
@click.argument("config_file")
@click.option("--dry-run", is_flag=True, help="Print jobs without submitting")
@click.option("--overwrite", "-o", is_flag=True, help="Auto-overwrite existing jobs matching --status")
@click.option("--status", "-s", multiple=True, help="Only overwrite jobs with this status (can be repeated)")
@click.option("--yes", "-y", is_flag=True, help="Auto-create the experiment without confirmation")
def submit(config_file, dry_run, overwrite, status, yes):
    """Submit jobs from a config file.

    Overwriting an existing job stops the old platform job first.
    """
    try:
        loader = ConfigLoader(config_file)
        jobs = loader.jobs()
        spec = loader.get_experiment_spec()
    except ConfigError as e:
        fail(e)

    # Local debug runs before any platform interaction.
    if not dry_run and jobs[0].is_local:
        if loader.is_search:
            click.echo("Local debug: running first job only")
        code = run_local(jobs[0], echo=click.echo, err_echo=lambda m: click.echo(m, err=True))
        raise SystemExit(code)

    project = open_project()
    platform = PlatformClient()
    if dry_run:
        try:
            # Only auto placement needs the API in a dry run (read-only).
            api = PlatformAPI() if has_auto_jobs(jobs, spec) else None
            resolved = resolve_experiment(platform, api, spec, jobs, dry_run=True,
                                          confirm=click.confirm, echo=click.echo)
        except (ApiError, ConfigError, PlatformError) as e:
            fail(e)
        _dry_run(loader, jobs, spec, resolved.placement)
        return

    status_filter = set(status) if status else None
    if loader.is_search:
        search_name = loader.get_search_name()
        click.echo(f"Search: {search_name} ({len(jobs)} jobs)")
        selection = _select_search_jobs(project, platform, search_name, jobs,
                                        overwrite, status_filter)
        if selection is None:
            return
        submit_list, replacements = selection
    else:
        submit_list, replacements = jobs, {}

    # Experiment + auto placement only for the jobs actually being submitted.
    try:
        resolved = resolve_experiment(platform, PlatformAPI(), spec, submit_list,
                                      dry_run=False, assume_yes=yes,
                                      confirm=click.confirm, echo=click.echo)
    except (ApiError, ConfigError, PlatformError) as e:
        fail(e)

    if resolved.config_count is not None and resolved.config_count >= CONFIG_COUNT_WARNING:
        click.echo(
            f"Warning: experiment '{resolved.name}' already has "
            f"{resolved.config_count} configs. "
            "Too many configs may cause platform performance issues.",
            err=True,
        )
        if not click.confirm("Continue?", default=False):
            click.echo("Aborted.")
            return
    _echo_placement(resolved.placement)

    formatter = StatusFormatter()
    if not loader.is_search:
        try:
            job_id = submit_single(project, platform, resolved, jobs[0],
                                   overwrite=overwrite, status_filter=status_filter,
                                   confirm=click.confirm, choose=_choose_overwrite_single,
                                   echo=click.echo)
        except (ConfigError, PlatformError) as e:
            fail(e)
        if job_id:
            _show_submitted_jobs(project, platform, formatter, [job_id])
        return

    # Stop replaced runs, install the working set once, run.
    try:
        result = submit_jobs(project, platform, resolved, search_name, config_file,
                             submit_list, loader.get_scheduler_params(),
                             replacements=replacements, echo=click.echo,
                             err_echo=lambda m: click.echo(m, err=True),
                             progress=progressbar)
    except PlatformError as e:
        fail(e)
    _show_submitted_jobs(project, platform, formatter,
                         [e["job_id"] for e in result.job_entries])


def _select_search_jobs(project, platform, search_name, jobs, overwrite, status_filter):
    """Classify grid jobs against the tracker and confirm overwrites.

    Returns ``(submit_list, replacements)`` or None when nothing is submitted.
    """
    tracker = project.load_tracker()
    search_job_ids = tracker.get("searches", {}).get(search_name, {}).get("job_ids", [])
    refresh_statuses(project, platform, tracker, job_ids=search_job_ids)
    classified = classify_jobs(jobs, project.find_job_by_name, overwrite,
                               status_filter, choose=_choose_overwrite)

    if classified.new:
        click.echo(f"  New: {len(classified.new)} jobs")
    if classified.overwrite:
        click.echo(f"  Overwrite: {len(classified.overwrite)} jobs (old runs will be stopped)")
        for j, old_id, old_status in classified.overwrite:
            click.echo(f"    {j.name} (id={old_id[:8]}, status={old_status})")
    if classified.skipped:
        click.echo(f"  Skip: {len(classified.skipped)} jobs")

    total = len(classified.new) + len(classified.overwrite)
    if total == 0:
        click.echo("Nothing to submit.")
        return None
    if overwrite and classified.overwrite:
        if not click.confirm(f"Proceed with {total} jobs?", default=True):
            click.echo("Aborted.")
            return None
    submit_list = classified.new + [j for j, _, _ in classified.overwrite]
    replacements = {j.name: old_id for j, old_id, _ in classified.overwrite}
    return submit_list, replacements
