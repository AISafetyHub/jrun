"""The job status command (mounted on the `job` group in jobs.py)."""

from collections import Counter

import click

from jrun.formatter import StatusFormatter
from jrun.platform import PlatformClient
from jrun.project import Project
from jrun.scheduler import Scheduler
from jrun.tracking import refresh_job_for_display


@click.command("status")
@click.argument("name_or_id", required=False)
@click.option("--status", "-s", multiple=True, help="Filter by status")
@click.option("-n", "max_jobs", default=10, help="Max jobs to show (0 for all)")
def status_cmd(name_or_id, status, max_jobs):
    """Show job status."""
    project = Project()
    platform = PlatformClient()
    formatter = StatusFormatter(project)
    tracker = project.load_tracker()
    status_filter = set(status) if status else None

    if name_or_id:
        if name_or_id in tracker.get("searches", {}):
            _show_search_jobs(project, platform, formatter, tracker, name_or_id, status_filter, max_jobs)
            return

        for jid, info in tracker.get("jobs", {}).items():
            if jid == name_or_id or info.get("name") == name_or_id:
                _show_single_job(project, platform, formatter, jid, tracker)
                return

        for sname, sinfo in tracker.get("searches", {}).items():
            if name_or_id.lower() in sname.lower():
                _show_search_jobs(project, platform, formatter, tracker, sname, status_filter, max_jobs)
                return

        click.echo(f"No job or search matching '{name_or_id}'.")
        return

    _show_all_jobs(project, platform, formatter, tracker, status_filter, max_jobs)


def _show_all_jobs(project: Project, platform: PlatformClient, formatter: StatusFormatter,
                   tracker: dict, status_filter: set | None, max_jobs: int):
    searches = tracker.get("searches", {})
    standalone_jobs = {
        jid: info for jid, info in tracker.get("jobs", {}).items()
        if not info.get("search_name")
    }

    if not searches and not standalone_jobs:
        click.echo("No jobs tracked. Submit jobs with 'jrun submit <config>'.")
        return

    rows = []
    for sname, sinfo in searches.items():
        job_ids = sinfo.get("job_ids", [])
        statuses = []
        for jid in job_ids:
            job_info = tracker["jobs"].get(jid)
            if job_info:
                if jid.startswith("queued-"):
                    if job_info.get("status") != "Queued":
                        job_info["status"] = "Queued"
                        project.update_job_status(jid, "Queued")
                else:
                    refresh_job_for_display(project, platform, jid, job_info)
                statuses.append(job_info.get("status", "?"))
        if status_filter and not any(s in status_filter for s in statuses):
            continue
        counts = Counter(statuses)
        summary = ", ".join(f"{v} {k}" for k, v in counts.items())
        n_jobs = len(job_ids)
        rows.append((f"{sname} ({n_jobs} jobs)", summary, sinfo.get("submitted_at", ""), None, "search"))

    job_urls = {}
    for jid, info in standalone_jobs.items():
        refresh_job_for_display(project, platform, jid, info)
        if status_filter and info.get("status") not in status_filter:
            continue
        job_urls[jid] = project.build_job_url(
            jid,
            platform=info.get("platform"),
            experiment_name=info.get("experiment_name"),
        )
        rows.append((info["name"], info.get("status", "?"), info.get("submitted_at", ""), jid, "job"))

    if not rows:
        click.echo("No jobs match the filter.")
        return

    total = len(rows)
    if max_jobs > 0 and total > max_jobs:
        rows = rows[:max_jobs]

    formatter.print_summary_table(rows, job_urls)

    if max_jobs > 0 and total > max_jobs:
        click.echo(f"\n... showing {max_jobs}/{total} entries (use -n to show more)")

    for sname in searches:
        scheduler = Scheduler(project.jrun_dir, sname, None, "", 0, 0)
        pid = scheduler.is_running()
        if pid:
            click.echo(f"\nScheduler active for '{sname}' (pid={pid})")


def _show_search_jobs(project: Project, platform: PlatformClient, formatter: StatusFormatter,
                      tracker: dict, search_name: str, status_filter: set | None, max_jobs: int):
    search = tracker["searches"][search_name]
    click.echo(f"Search: {search_name}")
    click.echo(f"Config: {search['config_file']}")
    click.echo(f"Submitted: {search['submitted_at']}")

    scheduler = Scheduler(project.jrun_dir, search_name, None, "", 0, 0)
    pid = scheduler.is_running()
    if pid:
        click.echo(f"Scheduler: active (pid={pid})")
    click.echo()

    jobs = {}
    for jid in search["job_ids"]:
        if jid in tracker["jobs"]:
            if jid.startswith("queued-"):
                if tracker["jobs"][jid].get("status") != "Queued":
                    tracker["jobs"][jid]["status"] = "Queued"
                    project.update_job_status(jid, "Queued")
            else:
                refresh_job_for_display(project, platform, jid, tracker["jobs"][jid])
            jobs[jid] = tracker["jobs"][jid]

    if status_filter:
        jobs = {jid: info for jid, info in jobs.items() if info.get("status") in status_filter}

    total = len(jobs)
    if max_jobs > 0 and total > max_jobs:
        jobs = dict(list(jobs.items())[:max_jobs])

    formatter.print_job_table(jobs)

    if max_jobs > 0 and total > max_jobs:
        click.echo(f"\n... showing {max_jobs}/{total} jobs (use -n to show more)")


def _show_single_job(project: Project, platform: PlatformClient, formatter: StatusFormatter,
                     job_id: str, tracker: dict):
    tracked_info = tracker.get("jobs", {}).get(job_id)
    if tracked_info:
        refresh_job_for_display(project, platform, job_id, tracked_info)
        formatter.print_job_table({job_id: tracked_info})
        return
    job_info = platform.get_job_info(job_id)
    new_status = job_info.get("status") if isinstance(job_info, dict) else None

    click.echo(f"Job:    {job_id}")
    click.echo(f"Status: {new_status or '?'}")
