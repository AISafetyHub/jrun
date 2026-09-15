"""The submit command."""

import click

from jrun.api import PlatformAPI
from jrun.config import ConfigLoader
from jrun.errors import ApiError, ConfigError, PlatformError
from jrun.formatter import StatusFormatter
from jrun.models import job_runtime
from jrun.platform import PlatformClient
from jrun.project import Project
from jrun.submission import (classify_jobs, resolve_experiment, run_local,
                             submit_jobs, submit_single)
from jrun.tracking import refresh_active_statuses, refresh_job_for_display


def register(cli):
    cli.add_command(submit)


def _choose_overwrite(job, old_id, old_status) -> bool:
    """Interactive [o]verwrite/[s]kip prompt for an existing job."""
    click.echo(f"Job '{job.name}' already exists (id={old_id[:8]}, status={old_status})")
    choice = click.prompt(
        "  [o]verwrite / [s]kip?",
        type=click.Choice(["o", "s"], case_sensitive=False),
        default="s",
    )
    return choice == "o"


def _choose_overwrite_single(job, old_id, old_status) -> bool:
    choice = click.prompt(
        "[o]verwrite / [s]kip?",
        type=click.Choice(["o", "s"], case_sensitive=False),
        default="s",
    )
    return choice == "o"


def _progress(items, label="", **kwargs):
    return click.progressbar(items, label=label, show_pos=True, **kwargs)


def _show_submitted_jobs(project: Project, formatter: StatusFormatter, job_ids: list):
    platform = PlatformClient()
    tracker = project.load_tracker()
    jobs = {}
    for jid in job_ids:
        if jid in tracker.get("jobs", {}):
            info = tracker["jobs"][jid]
            refresh_job_for_display(project, platform, jid, info)
            jobs[jid] = info
    if jobs:
        click.echo()
        formatter.print_job_table(jobs)


def _echo_job_details(job, spec):
    click.echo(f"  $ {job.command}")
    image, image_region, queue_name = job_runtime(job, spec)
    if image:
        region = f" ({image_region})" if image_region else ""
        click.echo(f"  Image: {image}{region}")
    if queue_name:
        click.echo(f"  Queue: {queue_name}")
    if job.envs:
        click.echo(f"  Envs: {job.envs}")
    if job.resource_config.worker:
        click.echo(f"  Worker: {job.resource_config.worker.to_dict()}")


@click.command()
@click.argument("config_file")
@click.option("--dry-run", is_flag=True, help="Print jobs without submitting")
@click.option("--overwrite", "-o", is_flag=True, help="Auto-overwrite existing jobs matching --status")
@click.option("--status", "-s", multiple=True, help="Only overwrite jobs with this status (can be repeated)")
@click.option("--yes", "-y", is_flag=True, help="Auto-create the experiment without confirmation")
def submit(config_file, dry_run, overwrite, status, yes):
    """Submit jobs from a config file."""
    project = Project()
    platform = PlatformClient()
    loader = ConfigLoader(config_file)
    formatter = StatusFormatter(project)
    err = lambda msg: click.echo(msg, err=True)

    search = loader.config.get("search")
    if search:
        jobs = loader.expand_grid()
    else:
        single = loader.build_single_job()
        if not single:
            click.echo("Error: config has neither 'search' nor 'job' section.", err=True)
            raise SystemExit(1)
        jobs = [single]

    # Local debug runs before any platform interaction.
    if not dry_run and jobs[0].is_local:
        if search:
            click.echo(f"Local debug: running first job only")
        run_local(jobs[0], echo=click.echo, err_echo=err)
        return

    try:
        spec = loader.get_experiment_spec()
        resolved = resolve_experiment(
            loader, project, platform, PlatformAPI() if not dry_run else None,
            spec, dry_run, yes, jobs=jobs, confirm=click.confirm, echo=click.echo,
        )
    except (ApiError, ConfigError, PlatformError) as e:
        click.echo(f"Error: {e}", err=True)
        raise SystemExit(1)

    exp_name, exp_id = resolved.name, resolved.id

    if dry_run and spec:
        click.echo(f"Experiment: {spec.name} (will be created if missing)")

    if not dry_run and resolved.config_count is not None and resolved.config_count >= 100:
        click.echo(
            f"Warning: experiment '{exp_name or exp_id}' already has "
            f"{resolved.config_count} configs. "
            f"Too many configs may cause platform performance issues.",
            err=True,
        )
        if not click.confirm("Continue?", default=False):
            click.echo("Aborted.")
            return

    if not search:
        _submit_single(project, platform, jobs[0], formatter, config_file,
                       resolved, dry_run, overwrite, status, spec)
        return

    search_name = loader.get_search_name()
    click.echo(f"Search: {search_name} ({len(jobs)} jobs)")

    if dry_run:
        for i, j in enumerate(jobs, 1):
            click.echo(f"\n--- Job {i}: {j.name} ---")
            click.echo(f"  Params: {j.params}")
            _echo_job_details(j, spec)
        click.echo(f"\nTotal: {len(jobs)} jobs (dry run, nothing submitted)")
        return

    # Phase 1: scan and classify
    tracker = project.load_tracker()
    search_job_ids = tracker.get("searches", {}).get(search_name, {}).get("job_ids", [])
    refresh_active_statuses(project, platform, tracker, job_ids=search_job_ids)
    classified = classify_jobs(jobs, project.find_job_by_name, overwrite,
                               set(status) if status else None,
                               choose=_choose_overwrite)

    # Phase 2: summary and confirm
    if classified.new:
        click.echo(f"  New: {len(classified.new)} jobs")
    if classified.overwrite:
        click.echo(f"  Overwrite: {len(classified.overwrite)} jobs")
        for j, old_id, old_status in classified.overwrite:
            click.echo(f"    {j.name} (id={old_id[:8]}, status={old_status})")
    if classified.skipped:
        click.echo(f"  Skip: {len(classified.skipped)} jobs (status not in {list(status)})")

    total = len(classified.new) + len(classified.overwrite)
    if total == 0:
        click.echo("Nothing to submit.")
        return

    if overwrite and classified.overwrite:
        if not click.confirm(f"Proceed with {total} jobs?", default=True):
            click.echo("Aborted.")
            return

    # Build the current working set.  The platform config is replaced once;
    # old IDs are only removed from the local tracker after their replacement
    # has successfully started.
    submit_list = classified.new + [j for j, _, _ in classified.overwrite]
    replacements = {j.name: old_id for j, old_id, _ in classified.overwrite}
    sched_params = loader.get_scheduler_params()
    try:
        result = submit_jobs(project, platform, search_name, config_file, submit_list,
                             exp_name, exp_id, spec, resolved.queue_locations,
                             sched_params, echo=click.echo, err_echo=err,
                             progress=_progress, replacements=replacements,
                             experiment_data=resolved.experiment_data)
    except PlatformError as e:
        click.echo(f"Error: {e}", err=True)
        raise SystemExit(1)
    _show_submitted_jobs(project, formatter, [e["job_id"] for e in result.job_entries])


def _submit_single(project, platform, job, formatter, config_file,
                   resolved, dry_run, overwrite, status, spec):
    if dry_run:
        click.echo(f"Job: {job.name}")
        _echo_job_details(job, spec)
        click.echo("\n(dry run, nothing submitted)")
        return

    try:
        job_id = submit_single(project, platform, job, resolved.name, resolved.id,
                               resolved.spec, resolved.queue_locations,
                               overwrite, set(status) if status else None,
                               confirm=click.confirm, choose=_choose_overwrite_single,
                               echo=click.echo,
                               experiment_data=resolved.experiment_data)
    except PlatformError as e:
        click.echo(f"Error: {e}", err=True)
        raise SystemExit(1)
    if job_id:
        _show_submitted_jobs(project, formatter, [job_id])
