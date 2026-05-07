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

from jrun import airsctl
from jrun.submit import submit_one_job
from jrun.tracker import load_tracker, save_tracker, update_job_status, locked_tracker


TERMINAL_STATUSES = {"Completed", "Succeed", "Failed", "Stopped", "Cancelled"}
ACTIVE_STATUSES = {"Submitted", "Running", "Pending", "Queued", "Starting", "Scheduling"}


def _parse_job_status(output: str) -> str | None:
    # airsctl job list -j returns JSON with a top-level "status" field.
    # Avoid keyword scanning which can match historical states in the "states" field.
    try:
        data = json.loads(output)
        if isinstance(data, dict) and "status" in data:
            return data["status"]
    except (json.JSONDecodeError, TypeError):
        pass
    # Fallback: line-based keyword scan for non-JSON output (e.g. table format)
    for line in output.splitlines():
        lower = line.lower()
        for s in ("running", "completed", "succeed", "failed", "stopped", "cancelled", "queued", "starting"):
            if s in lower:
                return s.capitalize()
    return None


def _log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def _log_fn(msg, err=False):
    _log(msg)


def scheduler_dir(jrun_dir: Path) -> Path:
    return jrun_dir / "scheduler"


def pid_file(jrun_dir: Path, search_name: str) -> Path:
    return scheduler_dir(jrun_dir) / f"{search_name}.pid"


def log_file(jrun_dir: Path, search_name: str) -> Path:
    return scheduler_dir(jrun_dir) / f"{search_name}.log"


def args_file(jrun_dir: Path, search_name: str) -> Path:
    return scheduler_dir(jrun_dir) / f"{search_name}.args.json"


def is_scheduler_running(jrun_dir: Path, search_name: str) -> int | None:
    pf = pid_file(jrun_dir, search_name)
    if not pf.exists():
        return None
    try:
        pid = int(pf.read_text().strip())
        os.kill(pid, 0)
        return pid
    except (ValueError, ProcessLookupError, PermissionError):
        pf.unlink(missing_ok=True)
        return None


def stop_scheduler(jrun_dir: Path, search_name: str) -> bool:
    pid = is_scheduler_running(jrun_dir, search_name)
    if pid is None:
        return False
    try:
        os.kill(pid, signal.SIGTERM)
        pid_file(jrun_dir, search_name).unlink(missing_ok=True)
        return True
    except ProcessLookupError:
        pid_file(jrun_dir, search_name).unlink(missing_ok=True)
        return False


def start_scheduler(jrun_dir: Path, search_name: str, exp_name: str | None,
                    exp_id: str, parallel_trials: int, poll_interval: int,
                    pending_jobs: list[dict]) -> int:
    """Start a background scheduler daemon. Returns PID."""
    import subprocess

    sdir = scheduler_dir(jrun_dir)
    sdir.mkdir(exist_ok=True)

    af = args_file(jrun_dir, search_name)
    af.write_text(json.dumps({
        "jrun_dir": str(jrun_dir),
        "search_name": search_name,
        "exp_name": exp_name,
        "exp_id": exp_id,
        "parallel_trials": parallel_trials,
        "poll_interval": poll_interval,
        "pending_jobs": pending_jobs,
    }, indent=2))

    lf = log_file(jrun_dir, search_name)
    log_handle = open(lf, "a")

    proc = subprocess.Popen(
        [sys.executable, "-m", "jrun.scheduler", str(af)],
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    log_handle.close()

    pid_file(jrun_dir, search_name).write_text(str(proc.pid))
    return proc.pid


def run_scheduler(args: dict):
    """Main scheduler loop. Runs in the daemon process."""
    jrun_dir = Path(args["jrun_dir"])
    search_name = args["search_name"]
    exp_name = args.get("exp_name")
    exp_id = args["exp_id"]
    parallel_trials = args["parallel_trials"]
    poll_interval = args["poll_interval"]

    pf = pid_file(jrun_dir, search_name)
    pf.write_text(str(os.getpid()))

    _log(f"Scheduler started: search={search_name}, parallel={parallel_trials}, poll={poll_interval}s, pid={os.getpid()}")

    while True:
        tracker = load_tracker(jrun_dir)
        search = tracker.get("searches", {}).get(search_name)
        if not search:
            _log(f"Search '{search_name}' not found in tracker, exiting")
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

            # Active job — refresh status from platform
            if not jid.startswith("queued-"):
                result = airsctl.job_list(jid)
                if result.returncode == 0 and result.stdout:
                    new_status = _parse_job_status(result.stdout)
                    if new_status and new_status != status:
                        _log(f"Job {job_info['name']} ({jid[:8]}): {status} -> {new_status}")
                        update_job_status(jrun_dir, jid, new_status)
                        status = new_status

            if status in ACTIVE_STATUSES:
                active_count += 1

        slots = max(0, parallel_trials - active_count)
        to_submit = queued_ids[:slots]

        if to_submit:
            _log(f"Active: {active_count}, Queued: {len(queued_ids)}, Submitting: {len(to_submit)}")

        consecutive_failures = 0
        for queued_id in to_submit:
            with locked_tracker(jrun_dir) as tr:
                job_info = tr["jobs"].get(queued_id)
                if not job_info or job_info["status"] != "Queued":
                    continue
                job_data = job_info.get("job_data", {})
                job_name = job_info["name"]
                job_params = job_info.get("params", {})

            if not job_data.get("name"):
                _log(f"Skipping {queued_id}: missing job_data")
                continue

            _log(f"Submitting: {job_name}")

            # Retry with backoff on transient failures
            real_job_id = None
            for attempt in range(3):
                real_job_id = submit_one_job(job_data, exp_name, exp_id, log_fn=_log_fn)
                if real_job_id:
                    break
                if attempt < 2:
                    wait = 5 * (attempt + 1)
                    _log(f"  Retry {attempt + 1}/2 in {wait}s...")
                    time.sleep(wait)

            # Update tracker under lock
            with locked_tracker(jrun_dir) as tr:
                if real_job_id:
                    consecutive_failures = 0
                    _log(f"Submitted: {job_name} -> {real_job_id[:8]}")
                    tr["jobs"][real_job_id] = {
                        "name": job_name,
                        "search_name": search_name,
                        "params": job_params,
                        "status": "Submitted",
                        "submitted_at": datetime.now().isoformat(timespec="seconds"),
                    }
                    tr["jobs"].pop(queued_id, None)
                    search_data = tr["searches"].get(search_name)
                    if search_data:
                        search_data["job_ids"] = [
                            real_job_id if jid == queued_id else jid
                            for jid in search_data["job_ids"]
                        ]
                else:
                    consecutive_failures += 1
                    _log(f"Failed to submit after retries: {job_name}, keeping as Queued")

            # If too many consecutive failures, back off this entire round
            if consecutive_failures >= 3:
                _log(f"Too many consecutive failures, backing off until next poll")
                break

            # Throttle between submissions
            time.sleep(2)

        # Check if done
        tracker = load_tracker(jrun_dir)
        search = tracker.get("searches", {}).get(search_name)
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
            _log("All jobs completed, scheduler exiting")
            break

        time.sleep(poll_interval)

    # Cleanup
    pf = pid_file(jrun_dir, search_name)
    pf.unlink(missing_ok=True)
    af = args_file(jrun_dir, search_name)
    af.unlink(missing_ok=True)
    _log("Scheduler stopped")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <args_json_path>", file=sys.stderr)
        sys.exit(1)

    args_path = Path(sys.argv[1])
    if not args_path.exists():
        print(f"Args file not found: {args_path}", file=sys.stderr)
        sys.exit(1)

    args = json.loads(args_path.read_text())
    run_scheduler(args)
