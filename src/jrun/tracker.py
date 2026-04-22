import json
from pathlib import Path
from datetime import datetime

TRACKER_FILE = "jobs.json"


def _tracker_path(jrun_dir: Path) -> Path:
    return jrun_dir / TRACKER_FILE


def load_tracker(jrun_dir: Path) -> dict:
    path = _tracker_path(jrun_dir)
    if path.is_file():
        return json.loads(path.read_text())
    return {"searches": {}, "jobs": {}}


def save_tracker(jrun_dir: Path, data: dict):
    path = _tracker_path(jrun_dir)
    path.write_text(json.dumps(data, indent=2) + "\n")


def record_search(jrun_dir: Path, search_name: str, config_file: str, job_entries: list[dict]):
    """Record a search submission with its jobs.

    job_entries: [{"job_id": ..., "name": ..., "params": {...}}, ...]
    """
    tracker = load_tracker(jrun_dir)
    now = datetime.now().isoformat(timespec="seconds")

    tracker["searches"][search_name] = {
        "config_file": config_file,
        "submitted_at": now,
        "job_ids": [j["job_id"] for j in job_entries],
    }
    for j in job_entries:
        tracker["jobs"][j["job_id"]] = {
            "name": j["name"],
            "search_name": search_name,
            "params": j.get("params", {}),
            "status": "Submitted",
            "submitted_at": now,
        }
    save_tracker(jrun_dir, tracker)


def record_single_job(jrun_dir: Path, job_id: str, job_name: str):
    tracker = load_tracker(jrun_dir)
    now = datetime.now().isoformat(timespec="seconds")
    tracker["jobs"][job_id] = {
        "name": job_name,
        "search_name": None,
        "params": {},
        "status": "Submitted",
        "submitted_at": now,
    }
    save_tracker(jrun_dir, tracker)


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
    tracker = load_tracker(jrun_dir)
    tracker["jobs"].pop(job_id, None)
    for search in tracker.get("searches", {}).values():
        if job_id in search.get("job_ids", []):
            search["job_ids"].remove(job_id)
    save_tracker(jrun_dir, tracker)


def update_job_status(jrun_dir: Path, job_id: str, status: str):
    tracker = load_tracker(jrun_dir)
    if job_id in tracker["jobs"]:
        tracker["jobs"][job_id]["status"] = status
        save_tracker(jrun_dir, tracker)
