"""Tracking service: status refresh and job lifecycle helpers.

Sits between the command layer (jrun.commands) and Project/PlatformClient.
Never formats errors for the user — platform failures in best-effort cleanup
paths are intentionally swallowed (`except PlatformError: pass`).
"""

import click

from jrun.errors import PlatformError
from jrun.platform import PlatformClient
from jrun.project import Project
from jrun.scheduler import TERMINAL_STATUSES

# Statuses in which a job can be stopped on the platform.
STOPPABLE_STATUSES = ("Submitted", "Running", "Queued", "Starting", "Scheduling")


def refresh_job_for_display(project: Project, platform: PlatformClient,
                            job_id: str, tracked_info: dict):
    """Sync a job's status/platform context from the platform before display."""
    if job_id.startswith("queued-"):
        return
    platform_context = tracked_info.get("platform") or {}
    context_fields = (
        "projsetId", "projId", "userId", "projsetName", "projectName",
        "clusterName", "zoneName",
    )
    if tracked_info.get("status") in TERMINAL_STATUSES and all(
        platform_context.get(key) for key in context_fields
    ):
        return
    job_info = platform.get_job_info(job_id)
    if not isinstance(job_info, dict):
        return
    project.sync_job_info(job_id, tracked_info, job_info)


def refresh_active_statuses(project: Project, platform: PlatformClient, tracker: dict,
                            job_ids: list[str] | None = None):
    """Refresh status from platform for non-terminal, non-queued jobs.

    If job_ids is given, only refresh those jobs. Otherwise refresh all.
    """
    jobs = tracker.get("jobs", {})
    candidates = job_ids if job_ids is not None else list(jobs.keys())
    active = [(jid, jobs[jid]) for jid in candidates
              if jid in jobs and not jid.startswith("queued-")
              and jobs[jid].get("status") not in TERMINAL_STATUSES]
    if not active:
        return
    refreshed = 0
    with click.progressbar(active, label="Refreshing statuses", show_pos=True) as bar:
        for jid, info in bar:
            new_status = platform.get_job_status(jid)
            if new_status and new_status != info.get("status"):
                info["status"] = new_status
                project.update_job_status(jid, new_status)
                refreshed += 1
    if refreshed:
        click.echo(f"Refreshed {refreshed} job statuses from platform.")


def find_tracked_job(tracker: dict, name_or_id: str) -> tuple[str, dict] | None:
    """Find a tracked job by exact job ID or job name."""
    for jid, info in tracker.get("jobs", {}).items():
        if jid == name_or_id or info.get("name") == name_or_id:
            return jid, info
    return None


def stop_tracked_job(project: Project, platform: PlatformClient,
                     job_id: str, job_info: dict):
    """Stop one tracked job: queued/pending jobs are just marked Stopped."""
    if job_id.startswith("queued-") or job_info.get("status") == "Pending":
        project.update_job_status(job_id, "Stopped")
    else:
        try:
            platform.job_stop(job_id)
        except PlatformError:
            pass


def remove_one_job(platform: PlatformClient, job_id: str, job_name: str,
                   exp_id: str | None):
    """Best-effort platform cleanup for one job (cancel + delete config)."""
    if not job_id.startswith("queued-"):
        try:
            platform.job_cancel(job_id)
        except PlatformError:
            pass
    if exp_id:
        try:
            platform.remove_configs(exp_id, {job_name})
        except PlatformError:
            pass
