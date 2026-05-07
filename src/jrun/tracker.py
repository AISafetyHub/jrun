import fcntl
import json
import tempfile
from pathlib import Path
from datetime import datetime

TRACKER_FILE = "jobs.json"


def _tracker_path(jrun_dir: Path) -> Path:
    return jrun_dir / TRACKER_FILE


def _lock_path(jrun_dir: Path) -> Path:
    return jrun_dir / ".jobs.lock"


def load_tracker(jrun_dir: Path) -> dict:
    path = _tracker_path(jrun_dir)
    if path.is_file():
        content = path.read_text()
        if not content.strip():
            return {"searches": {}, "jobs": {}}
        return json.loads(content)
    return {"searches": {}, "jobs": {}}


def save_tracker(jrun_dir: Path, data: dict):
    path = _tracker_path(jrun_dir)
    fd = tempfile.NamedTemporaryFile(
        mode="w", dir=jrun_dir, prefix=".jobs_", suffix=".tmp", delete=False
    )
    try:
        fd.write(json.dumps(data, indent=2) + "\n")
        fd.flush()
        Path(fd.name).rename(path)
    finally:
        fd.close()


class locked_tracker:
    """Context manager that holds an exclusive file lock around tracker read/write.

    Usage:
        with locked_tracker(jrun_dir) as tracker:
            tracker["jobs"][jid]["status"] = "Running"
            # save happens automatically on exit
    """

    def __init__(self, jrun_dir: Path):
        self.jrun_dir = jrun_dir
        self._lock_fd = None
        self.data = None

    def __enter__(self) -> dict:
        lock_path = _lock_path(self.jrun_dir)
        self._lock_fd = open(lock_path, "w")
        fcntl.flock(self._lock_fd, fcntl.LOCK_EX)
        self.data = load_tracker(self.jrun_dir)
        return self.data

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is None:
            save_tracker(self.jrun_dir, self.data)
        self._lock_fd.close()
        return False


def record_search(jrun_dir: Path, search_name: str, config_file: str, job_entries: list[dict]):
    """Record a search submission with its jobs.

    job_entries: [{"job_id": ..., "name": ..., "params": {...}}, ...]
    """
    with locked_tracker(jrun_dir) as tracker:
        now = datetime.now().isoformat(timespec="seconds")

        existing_search = tracker["searches"].get(search_name)
        old_job_ids = existing_search["job_ids"] if existing_search else []
        new_job_ids = [j["job_id"] for j in job_entries]
        new_names = {j["name"] for j in job_entries}
        kept_ids = [
            jid for jid in old_job_ids
            if jid in tracker.get("jobs", {}) and tracker["jobs"][jid].get("name") not in new_names
        ]
        tracker["searches"][search_name] = {
            "config_file": config_file,
            "submitted_at": now,
            "job_ids": kept_ids + new_job_ids,
        }
        for j in job_entries:
            tracker["jobs"][j["job_id"]] = {
                "name": j["name"],
                "search_name": search_name,
                "params": j.get("params", {}),
                "status": "Submitted",
                "submitted_at": now,
            }


def record_single_job(jrun_dir: Path, job_id: str, job_name: str):
    with locked_tracker(jrun_dir) as tracker:
        now = datetime.now().isoformat(timespec="seconds")
        tracker["jobs"][job_id] = {
            "name": job_name,
            "search_name": None,
            "params": {},
            "status": "Submitted",
            "submitted_at": now,
        }


def get_jobs_by_search(jrun_dir: Path, search_name: str) -> list[str]:
    tracker = load_tracker(jrun_dir)
    search = tracker.get("searches", {}).get(search_name)
    if not search:
        return []
    return search["job_ids"]


def get_all_jobs(jrun_dir: Path) -> dict:
    return load_tracker(jrun_dir).get("jobs", {})


def find_job_by_name(jrun_dir: Path, job_name: str) -> tuple[str, dict] | None:
    tracker = load_tracker(jrun_dir)
    for jid, info in tracker["jobs"].items():
        if info["name"] == job_name:
            return jid, info
    return None


def remove_job(jrun_dir: Path, job_id: str):
    with locked_tracker(jrun_dir) as tracker:
        tracker["jobs"].pop(job_id, None)
        for search in tracker.get("searches", {}).values():
            if job_id in search.get("job_ids", []):
                search["job_ids"].remove(job_id)


def remove_search(jrun_dir: Path, search_name: str) -> list[str]:
    """Remove a search and all its jobs. Returns the list of job IDs removed."""
    with locked_tracker(jrun_dir) as tracker:
        search = tracker.get("searches", {}).pop(search_name, None)
        if not search:
            return []
        job_ids = search.get("job_ids", [])
        for jid in job_ids:
            tracker["jobs"].pop(jid, None)
    return job_ids


def update_job_status(jrun_dir: Path, job_id: str, status: str):
    with locked_tracker(jrun_dir) as tracker:
        if job_id in tracker["jobs"]:
            tracker["jobs"][job_id]["status"] = status


def record_pending_jobs(jrun_dir: Path, search_name: str, config_file: str, jobs: list[dict]):
    """Record jobs as Queued in tracker. Each job gets a temporary ID 'queued-<hash>'."""
    import hashlib
    with locked_tracker(jrun_dir) as tracker:
        now = datetime.now().isoformat(timespec="seconds")

        job_ids = []
        for j in jobs:
            queued_id = "queued-" + hashlib.md5(j["name"].encode()).hexdigest()[:12]
            job_ids.append(queued_id)
            tracker["jobs"][queued_id] = {
                "name": j["name"],
                "search_name": search_name,
                "params": j.get("params", {}),
                "status": "Queued",
                "submitted_at": now,
                "job_data": {
                    "name": j["name"],
                    "command": j["command"],
                    "params": j.get("params", {}),
                    "resource_config": j.get("resource_config", {}),
                    "envs": j.get("envs", {}),
                },
            }

        existing_search = tracker["searches"].get(search_name)
        old_job_ids = existing_search["job_ids"] if existing_search else []
        new_names = {j["name"] for j in jobs}
        kept_ids = [
            jid for jid in old_job_ids
            if jid in tracker.get("jobs", {}) and tracker["jobs"][jid].get("name") not in new_names
        ]
        tracker["searches"][search_name] = {
            "config_file": config_file,
            "submitted_at": now,
            "job_ids": kept_ids + job_ids,
        }
    return job_ids


def replace_pending_with_real(jrun_dir: Path, queued_id: str, real_job_id: str):
    """Replace a queued job entry with the real job ID after submission."""
    with locked_tracker(jrun_dir) as tracker:
        if queued_id not in tracker["jobs"]:
            return
        job_info = tracker["jobs"].pop(queued_id)
        job_info["status"] = "Submitted"
        job_info.pop("job_data", None)
        tracker["jobs"][real_job_id] = job_info

        for search in tracker["searches"].values():
            ids = search.get("job_ids", [])
            search["job_ids"] = [real_job_id if jid == queued_id else jid for jid in ids]
