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

from jrun.errors import PlatformError
from jrun.models import Job
from jrun.platform import PlatformClient
from jrun.project import Project


TERMINAL_STATUSES = {"Completed", "Succeed", "Failed", "Stopped", "Cancelled"}
ACTIVE_STATUSES = {"Submitted", "Running", "Pending", "Queued", "Starting", "Scheduling"}


class Scheduler:
    def __init__(self, jrun_dir: Path, search_name: str, exp_name: str | None,
                 exp_id: str, parallel_trials: int, poll_interval: int):
        self.jrun_dir = jrun_dir
        self.search_name = search_name
        self.exp_name = exp_name
        self.exp_id = exp_id
        self.parallel_trials = parallel_trials
        self.poll_interval = poll_interval
        self.platform = PlatformClient()

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
                    new_status = self.platform.get_job_status(jid)
                    if new_status and new_status != status:
                        self._log(f"Job {job_info['name']} ({jid[:8]}): {status} -> {new_status}")
                        project.update_job_status(jid, new_status)
                        status = new_status

                if status in ACTIVE_STATUSES:
                    active_count += 1

            slots = max(0, self.parallel_trials - active_count)
            to_submit = queued_ids[:slots]

            if to_submit:
                self._log(f"Active: {active_count}, Queued: {len(queued_ids)}, Submitting: {len(to_submit)}")

            consecutive_failures = 0
            for queued_id in to_submit:
                with project._locked_tracker() as tr:
                    job_info = tr["jobs"].get(queued_id)
                    if not job_info or job_info["status"] != "Queued":
                        continue
                    job_data = job_info.get("job_data", {})
                    job_name = job_info["name"]
                    job_params = job_info.get("params", {})

                if not job_data.get("name"):
                    self._log(f"Skipping {queued_id}: missing job_data")
                    continue

                self._log(f"Submitting: {job_name}")

                job = Job.from_dict(job_data)
                real_job_id = None
                for attempt in range(3):
                    try:
                        real_job_id = self.platform.submit_job(job, self.exp_name, self.exp_id)
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
                        self._log(f"Submitted: {job_name} -> {real_job_id[:8]}")
                        tr["jobs"][real_job_id] = {
                            "name": job_name,
                            "search_name": self.search_name,
                            "params": job_params,
                            "status": "Submitted",
                            "submitted_at": datetime.now().isoformat(timespec="seconds"),
                        }
                        tr["jobs"].pop(queued_id, None)
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
