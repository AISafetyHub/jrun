"""Tracking service: status refresh and job lifecycle helpers.

Sits between the command layer (jrun.commands) and Project/PlatformClient.
No click dependency: progress display is injected by the caller. Platform
failures in best-effort cleanup paths are intentionally swallowed
(`except PlatformError: pass`).
"""

import time
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass, field

from jrun.errors import PlatformError
from jrun.models import TERMINAL_STATUSES
from jrun.platform import PlatformClient
from jrun.project import Project

_CONTEXT_FIELDS = (
    "projsetId", "projId", "userId", "projsetName", "projectName",
    "clusterName", "zoneName",
)


@contextmanager
def plain_progress(items, label="", **kwargs):
    """Default progress display: none, just iterate."""
    yield items


def is_queued_id(job_id: str) -> bool:
    """Synthetic IDs for scheduler-held jobs that never reached the platform."""
    return job_id.startswith("queued-")


def refresh_job_for_display(project: Project, platform: PlatformClient,
                            job_id: str, tracked_info: dict):
    """Sync a job's status/platform context from the platform before display."""
    if is_queued_id(job_id):
        return
    context = tracked_info.get("platform") or {}
    if tracked_info.get("status") in TERMINAL_STATUSES and all(
        context.get(key) for key in _CONTEXT_FIELDS
    ):
        return
    job_info = platform.get_job_info(job_id)
    if isinstance(job_info, dict):
        project.sync_job_info(job_id, tracked_info, job_info)


def refresh_active_statuses(project: Project, platform: PlatformClient, tracker: dict,
                            job_ids: list[str] | None = None,
                            progress=plain_progress) -> int:
    """Refresh status from the platform for non-terminal, non-queued jobs.

    Updates ``tracker`` in place and on disk. If ``job_ids`` is given, only
    those jobs are refreshed. Returns the number of changed statuses.
    """
    jobs = tracker.get("jobs", {})
    candidates = job_ids if job_ids is not None else list(jobs)
    active = [(jid, jobs[jid]) for jid in candidates
              if jid in jobs and not is_queued_id(jid)
              and jobs[jid].get("status") not in TERMINAL_STATUSES]
    if not active:
        return 0
    refreshed = 0
    with progress(active, label="Refreshing statuses") as bar:
        for jid, info in bar:
            new_status = platform.get_job_status(jid)
            if new_status and new_status != info.get("status"):
                info["status"] = new_status
                project.update_job_status(jid, new_status)
                refreshed += 1
    return refreshed


def find_tracked_job(tracker: dict, name_or_id: str) -> tuple[str, dict] | None:
    """Find a tracked job by exact job ID or job name."""
    for jid, info in tracker.get("jobs", {}).items():
        if jid == name_or_id or info.get("name") == name_or_id:
            return jid, info
    return None


def stop_tracked_job(project: Project, platform: PlatformClient,
                     job_id: str, job_info: dict):
    """Best-effort stop: local queued-* jobs are just marked Stopped."""
    if is_queued_id(job_id):
        project.update_job_status(job_id, "Stopped")
        return
    try:
        platform.job_stop(job_id)
    except PlatformError:
        pass


def release_job(project: Project, platform: PlatformClient,
                job_id: str, job_info: dict):
    """Stop a job that is about to be replaced by an overwrite.

    Unlike stop_tracked_job this is strict: raises PlatformError when a
    still-active platform job could not be stopped, so the caller does not
    start a duplicate run.
    """
    if job_info.get("status") in TERMINAL_STATUSES:
        return
    if is_queued_id(job_id):
        project.update_job_status(job_id, "Stopped")
        return
    try:
        platform.job_stop(job_id)
    except PlatformError:
        # The cached status may be stale; a job that already ended is fine.
        status = platform.get_job_status(job_id)
        if status not in TERMINAL_STATUSES:
            raise
        project.update_job_status(job_id, status)
        return
    project.update_job_status(job_id, "Stopped")


@dataclass
class RemoveResult:
    removed: list[str] = field(default_factory=list)
    # Jobs still active on the platform after stop + wait: kept in the tracker.
    still_active: dict[str, str] = field(default_factory=dict)  # job_id -> status


def _stop_active(platform: PlatformClient, job_ids: list[str], jobs: dict,
                 wait_seconds: float, poll_seconds: float, sleep) -> dict[str, str]:
    """Stop the non-terminal jobs and wait until they end.

    Returns job_id -> last status for jobs that are still not terminal.
    """
    pending = {}
    for jid in job_ids:
        status = jobs.get(jid, {}).get("status")
        if status in TERMINAL_STATUSES:
            continue
        try:
            platform.job_stop(jid)
        except PlatformError:
            pass  # may already be ending; the status check below decides
        pending[jid] = status or "?"
    waited = 0.0
    while pending:
        for jid in list(pending):
            status = platform.get_job_status(jid)
            if status in TERMINAL_STATUSES:
                del pending[jid]
            elif status:
                pending[jid] = status
        if not pending or waited >= wait_seconds:
            break
        sleep(poll_seconds)
        waited += poll_seconds
    return pending


def remove_tracked_jobs(project: Project, platform: PlatformClient, tracker: dict,
                        job_ids: list[str], default_exp_id: str | None = None, *,
                        wait_seconds: float = 60, poll_seconds: float = 5,
                        sleep=None) -> RemoveResult:
    """Stop, archive (cancel) and untrack jobs, and remove their configs.

    ``airsctl job cancel`` only archives jobs that already ended, so active
    jobs are stopped first and polled until they end (up to
    ``wait_seconds``). A job that is still active afterwards stays tracked
    (``still_active``) so a running job never silently leaves the tracker.
    Archiving and config removal are best-effort. Configs are removed per
    experiment (the job's own ``experiment_id``, else ``default_exp_id``).
    """
    jobs = tracker.get("jobs", {})
    platform_ids = [jid for jid in job_ids if not is_queued_id(jid)]
    still_active = _stop_active(platform, platform_ids, jobs,
                                wait_seconds, poll_seconds, sleep or time.sleep)

    configs_by_exp: dict[str, set[str]] = defaultdict(set)
    removed = []
    for jid in job_ids:
        if jid in still_active:
            continue
        removed.append(jid)
        if is_queued_id(jid):
            continue
        try:
            platform.job_cancel(jid)
        except PlatformError:
            pass
        info = jobs.get(jid, {})
        exp_id = info.get("experiment_id") or default_exp_id
        if exp_id and info.get("name"):
            configs_by_exp[exp_id].add(info["name"])
    for exp_id, names in configs_by_exp.items():
        try:
            platform.remove_configs(exp_id, names)
        except PlatformError:
            pass
    for jid in removed:
        project.remove_job(jid)
    return RemoveResult(removed, still_active)
