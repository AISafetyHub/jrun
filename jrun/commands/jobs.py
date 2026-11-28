"""Job commands: job status / stop / remove.

Jobs are individual runs tracked in the project's `.jrun/jobs.json`; this group
covers their whole lifecycle after submission. Jobs submitted through a remote
service (tracked with ``remote: <url>``) are stopped/removed by that service.
"""

import click

from jrun.commands import echo_json, fail, open_project, refresh_statuses
from jrun.commands.status import status_cmd
from jrun.models import STOPPABLE_STATUSES
from jrun.platform import PlatformClient
from jrun.remote import RemoteRegistry
from jrun.scheduler import Scheduler
from jrun.tracking import find_tracked_job, remove_tracked_jobs, stop_tracked_job


def register(cli):
    cli.add_command(job)


@click.group()
def job():
    """Manage jobs tracked by this project (status / stop / remove)."""


job.add_command(status_cmd)

_JSON_HELP = "Print the result as JSON (messages on stderr)"


def _say(as_json):
    return lambda message: click.echo(message, err=as_json)


def _stop_each(project, platform, remotes, tracker, job_ids, status_filter, say,
               results) -> int:
    stopped = 0
    for jid in job_ids:
        info = tracker["jobs"].get(jid, {})
        job_status = info.get("status", "")
        if status_filter and job_status not in status_filter:
            continue
        if job_status in STOPPABLE_STATUSES:
            ok = stop_tracked_job(project, platform, jid, info, remotes)
            results[jid] = _stop_row(info, ok)
            stopped += ok
            say(f"{'Stopped' if ok else 'Could not stop'} {info.get('name', jid)} "
                f"(status={job_status})")
    return stopped


def _stop_row(info: dict, ok: bool) -> dict:
    row = {"ok": ok, "name": info.get("name"), "status": info.get("status")}
    if not ok:
        row["error"] = "stop failed"
    return row


@job.command()
@click.argument("name_or_id", required=False)
@click.option("--status", "-s", multiple=True, help="Only stop jobs with this status (can be repeated)")
@click.option("--json", "as_json", is_flag=True, help=_JSON_HELP)
def stop(name_or_id, status, as_json):
    """Stop a job, a whole search (and its scheduler), or all jobs with --status."""
    project = open_project()
    platform = PlatformClient()
    remotes = RemoteRegistry()
    tracker = project.load_tracker()
    status_filter = set(status) if status else None
    say = _say(as_json)
    results: dict[str, dict] = {}

    def refresh(job_ids=None):
        refresh_statuses(project, platform, tracker, job_ids=job_ids, remotes=remotes,
                         as_json=as_json)

    if not name_or_id:
        if not status_filter:
            fail("must provide either a job/search name or --status filter.")
        refresh()
        count = _stop_each(project, platform, remotes, tracker,
                           list(tracker.get("jobs", {})), status_filter, say, results)
        say(f"Stopped {count} jobs matching status {sorted(status_filter)}.")
    elif name_or_id in tracker.get("searches", {}):
        job_ids = list(tracker["searches"][name_or_id].get("job_ids", []))
        refresh(job_ids)
        if Scheduler(project.jrun_dir, name_or_id).stop():
            say(f"Stopped scheduler for '{name_or_id}'")
        job_ids = list(tracker["searches"][name_or_id].get("job_ids", []))
        count = _stop_each(project, platform, remotes, tracker, job_ids, status_filter,
                           say, results)
        say(f"Stopped {count} jobs in search '{name_or_id}'.")
    else:
        found = find_tracked_job(tracker, name_or_id)
        if not found:
            if as_json:
                echo_json({"results": {name_or_id: {"ok": False, "missing": True,
                                                    "error": "not tracked"}}})
                return
            fail(f"no job or search matching '{name_or_id}'.")
        job_id, job_info = found
        refresh([job_id])
        job_id, job_info = find_tracked_job(tracker, job_info.get("name") or job_id) or found
        ok = stop_tracked_job(project, platform, job_id, job_info, remotes)
        results[job_id] = _stop_row(job_info, ok)
        say(f"{'Stopped' if ok else 'Could not stop'} {job_info.get('name', job_id)}")
    if as_json:
        echo_json({"results": results})


def _report_still_active(result) -> bool:
    """Print jobs that are still active after stop; True when there were any."""
    for jid, status in result.still_active.items():
        click.echo(f"  {jid[:8]}: still {status} after stop, kept in the tracker; "
                   "run 'jrun job remove' again once it has stopped.", err=True)
    return bool(result.still_active)


def _remove_json(result, missing=()) -> dict:
    return {"removed": list(result.removed) if result else [],
            "still_active": dict(result.still_active) if result else {},
            "missing": list(missing)}


@job.command()
@click.argument("name_or_id")
@click.option("--status", "-s", multiple=True, help="Only remove jobs with this status (can be repeated)")
@click.option("--json", "as_json", is_flag=True, help=_JSON_HELP)
def remove(name_or_id, status, as_json):
    """Remove a job or search: stop it, cancel (archive), delete its config, untrack it."""
    project = open_project()
    platform = PlatformClient()
    remotes = RemoteRegistry()
    tracker = project.load_tracker()
    status_filter = set(status) if status else None
    say = _say(as_json)

    def refresh(job_ids):
        refresh_statuses(project, platform, tracker, job_ids=job_ids, remotes=remotes,
                         as_json=as_json)

    if name_or_id in tracker.get("searches", {}):
        search = tracker["searches"][name_or_id]
        refresh(list(search.get("job_ids", [])))
        job_ids = list(search.get("job_ids", []))
        Scheduler(project.jrun_dir, name_or_id).stop()
        exp_id = search.get("experiment_id")

        if status_filter:
            selected = [jid for jid in job_ids
                        if tracker["jobs"].get(jid, {}).get("status") in status_filter]
            result = remove_tracked_jobs(project, platform, tracker, selected, exp_id,
                                         remotes=remotes)
            say(f"Removed {len(result.removed)} jobs in search '{name_or_id}' "
                f"matching status {sorted(status_filter)}.")
            _report_still_active(result)
            if as_json:
                echo_json(_remove_json(result))
            return

        say(f"Removing search '{name_or_id}' ({len(job_ids)} jobs)")
        result = remove_tracked_jobs(project, platform, tracker, job_ids, exp_id,
                                     remotes=remotes)
        if _report_still_active(result):
            say(f"Removed {len(result.removed)} jobs; search '{name_or_id}' "
                f"kept for {len(result.still_active)} still-active jobs.")
        else:
            project.remove_search(name_or_id)
            say(f"Search '{name_or_id}' removed.")
        if as_json:
            echo_json(_remove_json(result))
        return

    found = find_tracked_job(tracker, name_or_id)
    if not found:
        if as_json:
            echo_json(_remove_json(None, missing=[name_or_id]))
            return
        fail(f"no job or search matching '{name_or_id}'.")
    job_id, job_info = found
    job_name = job_info.get("name", job_id)
    refresh([job_id])
    job_id, job_info = find_tracked_job(tracker, job_name) or found
    say(f"Removing {job_name}...")
    result = remove_tracked_jobs(project, platform, tracker, [job_id], remotes=remotes)
    still = _report_still_active(result)
    if as_json:
        echo_json(_remove_json(result))
        return
    if still:
        raise SystemExit(1)
    say(f"Removed {job_name}")
