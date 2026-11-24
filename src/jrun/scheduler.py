"""Background scheduler daemon for parallel_trials job management.

Usage: python -m jrun.scheduler <args_json_path>
"""

import json
import os
import signal
import sys
import time
from datetime import datetime
from pathlib import Path

from jrun.api import PlatformAPI
from jrun.errors import ApiError, PlatformError
from jrun.models import (ACTIVE_STATUSES, SHARED_CATEGORIES, TERMINAL_STATUSES, Job,
                         normalize_queue_category)
from jrun.platform import PlatformClient
from jrun.project import Project


class Scheduler:
    """Run-only daemon for one search's queued jobs.

    All pending configs are installed by the submit command's single
    experiment modify; the scheduler only calls ``job run`` for them as
    slots free up. ``parallel_trials`` <= 0 means no concurrency limit.
    Public/idle jobs additionally wait for a free per-projset submit slot
    (read from the platform each poll). Constructing with just ``jrun_dir``
    and ``search_name`` is enough for PID management (is_running/stop).
    """

    def __init__(self, jrun_dir: Path, search_name: str, exp_name: str | None = None,
                 exp_id: str | None = None, parallel_trials: int = 0,
                 poll_interval: int = 30):
        self.jrun_dir = jrun_dir
        self.search_name = search_name
        self.exp_name = exp_name
        self.exp_id = exp_id
        self.parallel_trials = parallel_trials
        self.poll_interval = poll_interval
        self.platform = PlatformClient()
        self._api: PlatformAPI | None = None

    def _shared_slots(self, job: Job, cache: dict) -> int | None:
        """Remaining public/idle submit slots for the job's mode, or None.

        None means dedicated, unlimited, or unknown (the run is attempted and
        a platform rejection keeps the job Queued). ``cache`` is per poll.
        """
        rc = job.resource_config
        category = normalize_queue_category(rc.queue_category)
        if category not in SHARED_CATEGORIES:
            return None
        key = (rc.project_set_name, rc.project_name)
        if key not in cache:
            try:
                if self._api is None:
                    self._api = PlatformAPI()
                proj_id, projset_id = self._api.resolve_project_context(*key)
                cache[key] = self._api.shared_queue_budgets(proj_id=proj_id,
                                                            projset_id=projset_id)
            except (ApiError, ValueError) as e:
                self._log(f"  Cannot read public/idle submit slots ({e}); trying anyway")
                cache[key] = {}
        return cache[key].get(category)

    @staticmethod
    def _use_shared_slot(job: Job, cache: dict):
        rc = job.resource_config
        budgets = cache.get((rc.project_set_name, rc.project_name)) or {}
        category = normalize_queue_category(rc.queue_category)
        if budgets.get(category) is not None:
            budgets[category] -= 1

    @property
    def _scheduler_dir(self) -> Path:
        return self.jrun_dir / "scheduler"

    @property
    def _pid_file(self) -> Path:
        return self._scheduler_dir / f"{self.search_name}.pid"

    @property
    def _log_file(self) -> Path:
        return self._scheduler_dir / f"{self.search_name}.log"

    @property
    def _args_file(self) -> Path:
        return self._scheduler_dir / f"{self.search_name}.args.json"

    def is_running(self) -> int | None:
        if not self._pid_file.exists():
            return None
        try:
            pid = int(self._pid_file.read_text().strip())
            os.kill(pid, 0)
            return pid
        except (ValueError, ProcessLookupError, PermissionError):
            self._pid_file.unlink(missing_ok=True)
            return None

    def stop(self) -> bool:
        pid = self.is_running()
        if pid is None:
            return False
        try:
            os.kill(pid, signal.SIGTERM)
            self._pid_file.unlink(missing_ok=True)
            return True
        except ProcessLookupError:
            self._pid_file.unlink(missing_ok=True)
            return False

    def start(self, pending_jobs: list) -> int:
        import subprocess as sp

        self._scheduler_dir.mkdir(exist_ok=True)

        pending_dicts = [j.to_dict() if hasattr(j, "to_dict") else j for j in pending_jobs]
        self._args_file.write_text(json.dumps({
            "jrun_dir": str(self.jrun_dir),
            "search_name": self.search_name,
            "exp_name": self.exp_name,
            "exp_id": self.exp_id,
            "parallel_trials": self.parallel_trials,
            "poll_interval": self.poll_interval,
            "pending_jobs": pending_dicts,
        }, indent=2))

        log_handle = open(self._log_file, "a")
        proc = sp.Popen(
            [sys.executable, "-m", "jrun.scheduler", str(self._args_file)],
            stdout=log_handle,
            stderr=sp.STDOUT,
            start_new_session=True,
        )
        log_handle.close()

        self._pid_file.write_text(str(proc.pid))
        return proc.pid

    def run_loop(self):
        self._pid_file.parent.mkdir(exist_ok=True)
        self._pid_file.write_text(str(os.getpid()))

        self._log(f"Scheduler started: search={self.search_name}, parallel={self.parallel_trials}, "
                  f"poll={self.poll_interval}s, pid={os.getpid()}")

        project = Project(self.jrun_dir.parent)

        while True:
            tracker = project.load_tracker()
            search = tracker.get("searches", {}).get(self.search_name)
            if not search:
                self._log(f"Search '{self.search_name}' not found in tracker, exiting")
                break

            queued_ids = []
            active_count = 0

            for jid in search["job_ids"]:
                job_info = tracker["jobs"].get(jid)
                if not job_info:
                    continue

                status = job_info["status"]

                if status in TERMINAL_STATUSES:
                    continue

                if status == "Queued":
                    queued_ids.append(jid)
                    continue

                if not jid.startswith("queued-"):
                    platform_info = self.platform.get_job_info(jid)
                    if isinstance(platform_info, dict):
                        new_status = project.sync_job_info(jid, job_info, platform_info)
                        if new_status and new_status != status:
                            self._log(f"Job {job_info['name']} ({jid[:8]}): {status} -> {new_status}")
                        status = new_status or status

                if status in ACTIVE_STATUSES:
                    active_count += 1

            if self.parallel_trials and self.parallel_trials > 0:
                slots = max(0, self.parallel_trials - active_count)
            else:
                slots = len(queued_ids)

            if queued_ids and slots:
                self._log(f"Active: {active_count}, Queued: {len(queued_ids)}, Slots: {slots}")

            consecutive_failures = 0
            started = 0
            budget_cache: dict = {}
            for queued_id in queued_ids:
                if started >= slots:
                    break
                with project._locked_tracker() as tr:
                    job_info = tr["jobs"].get(queued_id)
                    if not job_info or job_info["status"] != "Queued":
                        continue
                    job_data = job_info.get("job_data", {})
                    job_name = job_info["name"]
                    job_params = job_info.get("params", {})

                try:
                    pending_job = Job.from_dict(job_data)
                except ValueError as e:
                    self._log(f"Skipping {queued_id}: invalid job_data ({e})")
                    continue

                remaining = self._shared_slots(pending_job, budget_cache)
                if remaining is not None and remaining <= 0:
                    continue  # no public/idle submit slot; retry next poll

                self._log(f"Submitting: {job_name}")

                real_job_id = None
                for attempt in range(3):
                    try:
                        # The submit command persisted the effective queue
                        # category into job_data for exactly this call.
                        real_job_id = self.platform.run_config(
                            self.exp_id, job_name,
                            run_options=pending_job.run_options,
                            queue_category=pending_job.resource_config.queue_category,
                        )
                        break
                    except PlatformError as e:
                        self._log(f"  Submit error: {e}")
                        if attempt < 2:
                            wait = 5 * (attempt + 1)
                            self._log(f"  Retry {attempt + 1}/2 in {wait}s...")
                            time.sleep(wait)

                with project._locked_tracker() as tr:
                    if real_job_id:
                        consecutive_failures = 0
                        started += 1
                        self._use_shared_slot(pending_job, budget_cache)
                        self._log(f"Submitted: {job_name} -> {real_job_id[:8]}")
                        tr["jobs"][real_job_id] = {
                            "name": job_name,
                            "search_name": self.search_name,
                            "params": job_params,
                            "status": "Submitted",
                            "submitted_at": datetime.now().isoformat(timespec="seconds"),
                            "experiment_id": self.exp_id,
                            "experiment_name": self.exp_name,
                        }
                        tr["jobs"].pop(queued_id, None)
                        replacement_id = job_info.get("replace_job_id")
                        if replacement_id and replacement_id != real_job_id:
                            # The old run was already stopped at submit time;
                            # drop its local mapping now that the replacement
                            # has started.
                            tr["jobs"].pop(replacement_id, None)
                            for other_search in tr.get("searches", {}).values():
                                other_search["job_ids"] = [
                                    jid for jid in other_search.get("job_ids", [])
                                    if jid != replacement_id
                                ]
                        search_data = tr["searches"].get(self.search_name)
                        if search_data:
                            search_data["job_ids"] = [
                                real_job_id if jid == queued_id else jid
                                for jid in search_data["job_ids"]
                            ]
                    else:
                        consecutive_failures += 1
                        self._log(f"Failed to submit after retries: {job_name}, keeping as Queued")

                if consecutive_failures >= 3:
                    self._log("Too many consecutive failures, backing off until next poll")
                    break

                time.sleep(2)

            tracker = project.load_tracker()
            search = tracker.get("searches", {}).get(self.search_name)
            if not search:
                break

            has_queued = False
            has_active = False
            for jid in search["job_ids"]:
                job_info = tracker["jobs"].get(jid)
                if not job_info:
                    continue
                if job_info["status"] == "Queued":
                    has_queued = True
                elif job_info["status"] in ACTIVE_STATUSES:
                    has_active = True

            if not has_queued and not has_active:
                self._log("All jobs completed, scheduler exiting")
                break

            time.sleep(self.poll_interval)

        self._pid_file.unlink(missing_ok=True)
        self._args_file.unlink(missing_ok=True)
        self._log("Scheduler stopped")

    def _log(self, msg: str):
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{ts}] {msg}", flush=True)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <args_json_path>", file=sys.stderr)
        sys.exit(1)

    args_path = Path(sys.argv[1])
    if not args_path.exists():
        print(f"Args file not found: {args_path}", file=sys.stderr)
        sys.exit(1)

    args = json.loads(args_path.read_text())
    scheduler = Scheduler(
        jrun_dir=Path(args["jrun_dir"]),
        search_name=args["search_name"],
        exp_name=args.get("exp_name"),
        exp_id=args["exp_id"],
        parallel_trials=args["parallel_trials"],
        poll_interval=args["poll_interval"],
    )
    scheduler.run_loop()
