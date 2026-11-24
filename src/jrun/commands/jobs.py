"""Job commands: job status / stop / remove.

Jobs are individual runs tracked in the project's `.jrun/jobs.json`; this group
covers their whole lifecycle after submission.
"""

import click

from jrun.commands import fail, open_project, refresh_statuses
from jrun.commands.status import status_cmd
from jrun.models import STOPPABLE_STATUSES
from jrun.platform import PlatformClient
from jrun.scheduler import Scheduler
from jrun.tracking import find_tracked_job, remove_tracked_jobs, stop_tracked_job


def register(cli):
    cli.add_command(job)


@click.group()
def job():
    """Manage jobs tracked by this project (status / stop / remove)."""


job.add_command(status_cmd)


def _stop_each(project, platform, tracker, job_ids, status_filter) -> int:
    stopped = 0
    for jid in job_ids:
        info = tracker["jobs"].get(jid, {})
        job_status = info.get("status", "")
        if status_filter and job_status not in status_filter:
            continue
        if job_status in STOPPABLE_STATUSES:
            stop_tracked_job(project, platform, jid, info)
            stopped += 1
            click.echo(f"Stopped {info.get('name', jid)} (status={job_status})")
    return stopped


@job.command()
@click.argument("name_or_id", required=False)
@click.option("--status", "-s", multiple=True, help="Only stop jobs with this status (can be repeated)")
def stop(name_or_id, status):
    """Stop a job, a whole search (and its scheduler), or all jobs with --status."""
    project = open_project()
    platform = PlatformClient()
    tracker = project.load_tracker()
    status_filter = set(status) if status else None

    if not name_or_id:
        if not status_filter:
            fail("must provide either a job/search name or --status filter.")
        refresh_statuses(project, platform, tracker)
        count = _stop_each(project, platform, tracker, list(tracker.get("jobs", {})),
                           status_filter)
        click.echo(f"Stopped {count} jobs matching status {sorted(status_filter)}.")
        return

    if name_or_id in tracker.get("searches", {}):
        job_ids = tracker["searches"][name_or_id].get("job_ids", [])
        refresh_statuses(project, platform, tracker, job_ids=job_ids)
        if Scheduler(project.jrun_dir, name_or_id).stop():
            click.echo(f"Stopped scheduler for '{name_or_id}'")
        count = _stop_each(project, platform, tracker, job_ids, status_filter)
        click.echo(f"Stopped {count} jobs in search '{name_or_id}'.")
        return

    found = find_tracked_job(tracker, name_or_id)
    if not found:
        fail(f"no job or search matching '{name_or_id}'.")
    job_id, job_info = found
    refresh_statuses(project, platform, tracker, job_ids=[job_id])
    stop_tracked_job(project, platform, job_id, job_info)
    click.echo(f"Stopped {job_info.get('name', job_id)}")


def _report_still_active(result) -> bool:
    """Print jobs that are still active after stop; True when there were any."""
    for jid, status in result.still_active.items():
        click.echo(f"  {jid[:8]}: still {status} after stop, kept in the tracker; "
                   "run 'jrun job remove' again once it has stopped.", err=True)
    return bool(result.still_active)


@job.command()
@click.argument("name_or_id")
@click.option("--status", "-s", multiple=True, help="Only remove jobs with this status (can be repeated)")
def remove(name_or_id, status):
    """Remove a job or search: stop it, cancel (archive), delete its config, untrack it."""
    project = open_project()
    platform = PlatformClient()
    tracker = project.load_tracker()
    status_filter = set(status) if status else None

    if name_or_id in tracker.get("searches", {}):
        search = tracker["searches"][name_or_id]
        job_ids = search.get("job_ids", [])
        refresh_statuses(project, platform, tracker, job_ids=job_ids)
        Scheduler(project.jrun_dir, name_or_id).stop()
        exp_id = search.get("experiment_id")

        if status_filter:
            selected = [jid for jid in job_ids
                        if tracker["jobs"].get(jid, {}).get("status") in status_filter]
            result = remove_tracked_jobs(project, platform, tracker, selected, exp_id)
            click.echo(f"Removed {len(result.removed)} jobs in search '{name_or_id}' "
                       f"matching status {sorted(status_filter)}.")
            _report_still_active(result)
            return

        click.echo(f"Removing search '{name_or_id}' ({len(job_ids)} jobs)")
        result = remove_tracked_jobs(project, platform, tracker, job_ids, exp_id)
        if _report_still_active(result):
            click.echo(f"Removed {len(result.removed)} jobs; search '{name_or_id}' "
                       f"kept for {len(result.still_active)} still-active jobs.")
            return
        project.remove_search(name_or_id)
        click.echo(f"Search '{name_or_id}' removed.")
        return

    found = find_tracked_job(tracker, name_or_id)
    if not found:
        fail(f"no job or search matching '{name_or_id}'.")
    job_id, job_info = found
    job_name = job_info.get("name", job_id)
    refresh_statuses(project, platform, tracker, job_ids=[job_id])
    click.echo(f"Removing {job_name}...")
    result = remove_tracked_jobs(project, platform, tracker, [job_id])
    if _report_still_active(result):
        raise SystemExit(1)
    click.echo(f"Removed {job_name}")
