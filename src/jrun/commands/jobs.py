"""Job commands: job status / stop / remove.

Jobs are individual runs tracked in the project's `.jrun/jobs.json`; this group
covers their whole lifecycle after submission.
"""

import click

from jrun.commands.status import status_cmd
from jrun.errors import PlatformError
from jrun.platform import PlatformClient
from jrun.project import Project
from jrun.scheduler import Scheduler
from jrun.tracking import (find_tracked_job, refresh_active_statuses,
                           remove_one_job, stop_tracked_job)


def register(cli):
    cli.add_command(job)


@click.group()
def job():
    """Manage jobs tracked by this project (status / stop / remove)."""
    pass


job.add_command(status_cmd)


@job.command()
@click.argument("name_or_id", required=False)
@click.option("--status", "-s", multiple=True, help="Only stop jobs with this status (can be repeated)")
def stop(name_or_id, status):
    """Stop a running job or search scheduler by name or ID, or stop jobs by status."""
    project = Project()
    platform = PlatformClient()
    tracker = project.load_tracker()
    status_filter = set(status) if status else None

    if not name_or_id:
        if not status_filter:
            click.echo("Error: must provide either a job/search name or --status filter.", err=True)
            raise SystemExit(1)

        refresh_active_statuses(project, platform, tracker)
        stopped_count = 0
        for jid, job_info in tracker.get("jobs", {}).items():
            if job_info.get("status") in status_filter:
                stop_tracked_job(project, platform, jid, job_info)
                stopped_count += 1
                click.echo(f"Stopped {job_info['name']} (status={job_info.get('status')})")

        click.echo(f"Stopped {stopped_count} jobs matching status {list(status_filter)}.")
        return

    if name_or_id in tracker.get("searches", {}):
        search = tracker["searches"][name_or_id]
        search_job_ids = search.get("job_ids", [])
        refresh_active_statuses(project, platform, tracker, job_ids=search_job_ids)

        scheduler = Scheduler(project.jrun_dir, name_or_id, None, "", 0, 0)
        stopped_sched = scheduler.stop()
        if stopped_sched:
            click.echo(f"Stopped scheduler for '{name_or_id}'")

        stopped_count = 0
        for jid in search_job_ids:
            job_info = tracker["jobs"].get(jid, {})
            job_status = job_info.get("status", "")
            if status_filter and job_status not in status_filter:
                continue
            if job_status in ("Submitted", "Running", "Queued", "Starting", "Scheduling"):
                stop_tracked_job(project, platform, jid, job_info)
                stopped_count += 1
                click.echo(f"Stopped {job_info.get('name', jid)} (status={job_status})")

        click.echo(f"Stopped {stopped_count} jobs in search '{name_or_id}'.")
        return

    found = find_tracked_job(tracker, name_or_id)
    if not found:
        click.echo(f"No job or search matching '{name_or_id}'.", err=True)
        raise SystemExit(1)
    job_id, job_info = found

    refresh_active_statuses(project, platform, tracker, job_ids=[job_id])

    stop_tracked_job(project, platform, job_id, job_info)
    click.echo(f"Stopped {job_info.get('name', job_id)}")


@job.command()
@click.argument("name_or_id")
@click.option("--status", "-s", multiple=True, help="Only remove jobs with this status (can be repeated)")
def remove(name_or_id, status):
    """Remove a job or search: stop + delete config + remove from tracker."""
    project = Project()
    platform = PlatformClient()
    tracker = project.load_tracker()
    status_filter = set(status) if status else None

    if name_or_id in tracker.get("searches", {}):
        search = tracker["searches"][name_or_id]
        search_job_ids = search.get("job_ids", [])
        refresh_active_statuses(project, platform, tracker, job_ids=search_job_ids)
        scheduler = Scheduler(project.jrun_dir, name_or_id, None, "", 0, 0)
        scheduler.stop()

        search = tracker["searches"][name_or_id]
        job_ids = search.get("job_ids", [])
        exp_id = search.get("experiment_id") or project.experiment_id

        if status_filter:
            config_names = set()
            removed_count = 0
            for jid in job_ids:
                job_info = tracker["jobs"].get(jid, {})
                if job_info.get("status") not in status_filter:
                    continue
                job_name = job_info.get("name", jid)
                if jid.startswith("queued-"):
                    project.remove_job(jid)
                else:
                    try:
                        platform.job_cancel(jid)
                    except PlatformError:
                        pass
                    if job_name:
                        config_names.add(job_name)
                    project.remove_job(jid)
                removed_count += 1

            if exp_id and config_names:
                try:
                    platform.remove_configs(exp_id, config_names)
                except PlatformError:
                    pass

            click.echo(f"Removed {removed_count} jobs in search '{name_or_id}' matching status {list(status_filter)}.")
        else:
            click.echo(f"Removing search '{name_or_id}' ({len(job_ids)} jobs)")

            config_names = set()
            for jid in job_ids:
                job_info = tracker["jobs"].get(jid, {})
                if jid.startswith("queued-"):
                    continue
                try:
                    platform.job_cancel(jid)
                except PlatformError:
                    pass
                name = job_info.get("name")
                if name:
                    config_names.add(name)

            if exp_id and config_names:
                try:
                    platform.remove_configs(exp_id, config_names)
                except PlatformError:
                    pass

            project.remove_search(name_or_id)
            click.echo(f"Search '{name_or_id}' removed.")
    else:
        found = find_tracked_job(tracker, name_or_id)
        if not found:
            click.echo(f"No job or search matching '{name_or_id}'.", err=True)
            raise SystemExit(1)
        job_id, job_info = found

        exp_id = job_info.get("experiment_id") or project.experiment_id
        refresh_active_statuses(project, platform, tracker, job_ids=[job_id])
        job_name = job_info.get("name", job_id)
        click.echo(f"Removing {job_name}...")
        remove_one_job(platform, job_id, job_name, exp_id)
        project.remove_job(job_id)
        click.echo(f"Removed {job_name}")
