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

from jrun.errors import ApiError, PlatformError
from jrun.models import TERMINAL_STATUSES
from jrun.platform import PlatformClient
from jrun.project import Project
from jrun.remote import RemoteRegistry

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


def is_remote(info: dict) -> bool:
    """Jobs submitted through a remote service (tracked with its URL)."""
    return bool(info.get("remote"))


def refresh_job_for_display(project: Project, platform: PlatformClient,
                            job_id: str, tracked_info: dict):
    """Sync a job's status/platform context from the platform before display.

    Remote jobs are refreshed in bulk by ``sync_remote_jobs`` instead.
    """
    if is_queued_id(job_id) or is_remote(tracked_info):
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
              if jid in jobs and not is_queued_id(jid) and not is_remote(jobs[jid])
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


def _needs_remote_refresh(info: dict) -> bool:
    return (info.get("status") not in TERMINAL_STATUSES
            or not (info.get("platform") or {}).get("userId"))


def sync_remote_jobs(project: Project, tracker: dict, remotes: RemoteRegistry | None = None,
                     job_ids: list[str] | None = None, *, on_error=None) -> int:
    """Refresh remote jobs from their services (one call per service URL).

    Updates status and platform context; a remote ``queued-*`` entry whose
    job has started on the service is re-keyed to the real job ID (matched
    by search and job name). Updates ``tracker`` in place and on disk and
    returns the number of changed entries. A service that cannot be reached
    is reported through ``on_error(url, error)`` and its jobs are left as is.
    """
    jobs = tracker.get("jobs", {})
    candidates = job_ids if job_ids is not None else list(jobs)
    by_url: dict[str, list[str]] = defaultdict(list)
    for jid in candidates:
        info = jobs.get(jid)
        if info and is_remote(info) and _needs_remote_refresh(info):
            by_url[info["remote"]].append(jid)
    if not by_url:
        return 0
    remotes = remotes or RemoteRegistry()
    changed = 0
    for url, ids in by_url.items():
        try:
            rows = remotes.get(url).jobs()
        except ApiError as e:
            if on_error:
                on_error(url, e)
            continue
        by_id = {r.get("job_id"): r for r in rows if r.get("job_id")}
        by_name = {(r.get("search_name"), r.get("name")): r for r in rows
                   if r.get("job_id") and not is_queued_id(r["job_id"])}
        for jid in ids:
            info = jobs[jid]
            row = by_id.get(jid)
            new_id = jid
            if row is None and is_queued_id(jid):
                row = by_name.get((info.get("search_name"), info.get("name")))
                if row is not None:
                    new_id = row["job_id"]
            if row is None:
                continue
            updates = {key: row[key] for key in ("status", "platform", "experiment_id",
                                                 "experiment_name") if row.get(key)}
            if new_id != jid:
                project.replace_job_id(jid, new_id, updates)
                jobs[new_id] = {**jobs.pop(jid), **updates}
                for search in tracker.get("searches", {}).values():
                    ids_list = search.get("job_ids", [])
                    if jid in ids_list:
                        ids_list[ids_list.index(jid)] = new_id
                changed += 1
            elif any(info.get(k) != v for k, v in updates.items()):
                info.update(updates)
                project.update_job_status(jid, updates.get("status"), updates.get("platform"))
                changed += 1
    return changed


def find_tracked_job(tracker: dict, name_or_id: str) -> tuple[str, dict] | None:
    """Find a tracked job by exact job ID or job name."""
    for jid, info in tracker.get("jobs", {}).items():
        if jid == name_or_id or info.get("name") == name_or_id:
            return jid, info
    return None


def _remote_stop(project: Project, remotes: RemoteRegistry | None,
                 job_id: str, job_info: dict) -> dict:
    """Ask the job's service to stop it; returns that job's result row."""
    remotes = remotes or RemoteRegistry()
    result = remotes.get(job_info["remote"]).stop([job_id])
    row = (result.get("results") or {}).get(job_id) or {}
    if row.get("status"):
        job_info["status"] = row["status"]
        project.update_job_status(job_id, row["status"])
    return row


def stop_tracked_job(project: Project, platform: PlatformClient,
                     job_id: str, job_info: dict,
                     remotes: RemoteRegistry | None = None) -> bool:
    """Best-effort stop: local queued-* jobs are just marked Stopped; remote
    jobs are stopped by their service. Returns whether the stop went through."""
    if is_remote(job_info):
        try:
            return bool(_remote_stop(project, remotes, job_id, job_info).get("ok"))
        except ApiError:
            return False
    if is_queued_id(job_id):
        project.update_job_status(job_id, "Stopped")
        job_info["status"] = "Stopped"
        return True
    try:
        platform.job_stop(job_id)
    except PlatformError:
        return False
    return True


def release_job(project: Project, platform: PlatformClient,
                job_id: str, job_info: dict, remotes: RemoteRegistry | None = None):
    """Stop a job that is about to be replaced by an overwrite.

    Unlike stop_tracked_job this is strict: raises PlatformError when a
    still-active platform job could not be stopped, so the caller does not
    start a duplicate run. Remote jobs are stopped by their service.
    """
    if job_info.get("status") in TERMINAL_STATUSES:
        return
    if is_remote(job_info):
        try:
            row = _remote_stop(project, remotes, job_id, job_info)
        except ApiError as e:
            raise PlatformError("job stop (remote)", 1, str(e)) from e
        if not row.get("ok") and row.get("status") not in TERMINAL_STATUSES:
            raise PlatformError("job stop (remote)", 1,
                                row.get("error") or f"service could not stop {job_id}")
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


def _remove_remote(project: Project, remotes: RemoteRegistry | None, jobs: dict,
                   job_ids: list[str]) -> tuple[list[str], dict[str, str]]:
    """Remove remote jobs through their services (one call per URL).

    Returns ``(removed, still_active)``. IDs the service no longer tracks
    count as removed; an unreachable service keeps its jobs tracked.
    """
    if not job_ids:
        return [], {}
    remotes = remotes or RemoteRegistry()
    by_url: dict[str, list[str]] = defaultdict(list)
    for jid in job_ids:
        by_url[jobs[jid]["remote"]].append(jid)
    removed, still_active = [], {}
    for url, ids in by_url.items():
        try:
            result = remotes.get(url).remove(ids)
        except ApiError as e:
            still_active.update({jid: f"unknown (remote error: {e})" for jid in ids})
            continue
        active = result.get("still_active") or {}
        gone = set(result.get("removed") or []) | set(result.get("missing") or [])
        for jid in ids:
            if jid in active:
                still_active[jid] = active[jid]
            elif jid in gone:
                removed.append(jid)
                project.remove_job(jid)
            else:
                still_active[jid] = jobs[jid].get("status") or "?"
    return removed, still_active


def remove_tracked_jobs(project: Project, platform: PlatformClient, tracker: dict,
                        job_ids: list[str], default_exp_id: str | None = None, *,
                        wait_seconds: float = 60, poll_seconds: float = 5,
                        sleep=None, remotes: RemoteRegistry | None = None) -> RemoveResult:
    """Stop, archive (cancel) and untrack jobs, and remove their configs.

    ``airsctl job cancel`` only archives jobs that already ended, so active
    jobs are stopped first and polled until they end (up to
    ``wait_seconds``). A job that is still active afterwards stays tracked
    (``still_active``) so a running job never silently leaves the tracker.
    Archiving and config removal are best-effort. Configs are removed per
    experiment (the job's own ``experiment_id``, else ``default_exp_id``).
    """
    jobs = tracker.get("jobs", {})
    remote_ids = [jid for jid in job_ids if is_remote(jobs.get(jid, {}))]
    job_ids = [jid for jid in job_ids if jid not in set(remote_ids)]
    remote_removed, remote_active = _remove_remote(project, remotes, jobs, remote_ids)

    platform_ids = [jid for jid in job_ids if not is_queued_id(jid)]
    still_active = _stop_active(platform, platform_ids, jobs,
                                wait_seconds, poll_seconds, sleep or time.sleep)

    configs_by_exp: dict[str, set[str]] = defaultdict(set)
    removed = list(remote_removed)
    still_active.update(remote_active)
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
        if jid not in remote_removed:
            project.remove_job(jid)
    return RemoveResult(removed, still_active)
