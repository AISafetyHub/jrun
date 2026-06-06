import fcntl
import hashlib
import json
import re
import tempfile
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

JRUN_DIR = ".jrun"
SETTINGS_FILE = "settings.json"
TRACKER_FILE = "jobs.json"
PLATFORM_BASE_URL = "https://platform.baai.ac.cn/platform/modelTraining/jobList/jobDetail"


class Project:
    def __init__(self, start: Path | None = None):
        self.jrun_dir = self._find_jrun_dir(start)
        self.settings = self._load_settings()

    @classmethod
    def init(cls, experiment_name: str, experiment_id: str | None = None,
             platform: dict | None = None, directory: Path | None = None) -> "Project":
        directory = directory or Path.cwd()
        jrun_dir = directory.resolve() / JRUN_DIR
        jrun_dir.mkdir(exist_ok=True)

        data = {"experiment_name": experiment_name}
        if experiment_id:
            data["experiment_id"] = experiment_id
        if platform:
            data["platform"] = platform

        path = jrun_dir / SETTINGS_FILE
        path.write_text(json.dumps(data, indent=2) + "\n")

        obj = object.__new__(cls)
        obj.jrun_dir = jrun_dir
        obj.settings = data
        return obj

    @property
    def experiment_name(self) -> str:
        return self.settings.get("experiment_name", "")

    @property
    def experiment_id(self) -> str | None:
        return self.settings.get("experiment_id")

    @property
    def platform_ids(self) -> dict | None:
        return self.settings.get("platform")

    def build_job_url(self, job_id: str) -> str | None:
        platform = self.platform_ids
        if not platform:
            return None
        params = {
            "id": job_id,
            "name": self.experiment_name,
            "projId": platform["projId"],
            "projsetId": platform["projsetId"],
            "userId": platform["userId"],
        }
        url = f"{PLATFORM_BASE_URL}?{urlencode(params)}"
        fragment_parts = {}
        if platform.get("projsetName"):
            fragment_parts["projsetName"] = platform["projsetName"]
        if platform.get("projectName"):
            fragment_parts["projectName"] = platform["projectName"]
        if fragment_parts:
            url += f"#{urlencode(fragment_parts)}"
        return url

    # --- Tracker operations ---

    def load_tracker(self) -> dict:
        path = self.jrun_dir / TRACKER_FILE
        if path.is_file():
            content = path.read_text()
            if not content.strip():
                return {"searches": {}, "jobs": {}}
            return json.loads(content)
        return {"searches": {}, "jobs": {}}

    def _save_tracker(self, data: dict):
        path = self.jrun_dir / TRACKER_FILE
        fd = tempfile.NamedTemporaryFile(
            mode="w", dir=self.jrun_dir, prefix=".jobs_", suffix=".tmp", delete=False
        )
        try:
            fd.write(json.dumps(data, indent=2) + "\n")
            fd.flush()
            Path(fd.name).rename(path)
        finally:
            fd.close()

    def _locked_tracker(self) -> "_LockedTracker":
        return _LockedTracker(self)

    def record_search(self, search_name: str, config_file: str, job_entries: list[dict]):
        with self._locked_tracker() as tracker:
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

    def record_single_job(self, job_id: str, job_name: str):
        with self._locked_tracker() as tracker:
            now = datetime.now().isoformat(timespec="seconds")
            tracker["jobs"][job_id] = {
                "name": job_name,
                "search_name": None,
                "params": {},
                "status": "Submitted",
                "submitted_at": now,
            }

    def find_job_by_name(self, job_name: str) -> tuple[str, dict] | None:
        tracker = self.load_tracker()
        for jid, info in tracker["jobs"].items():
            if info["name"] == job_name:
                return jid, info
        return None

    def remove_job(self, job_id: str):
        with self._locked_tracker() as tracker:
            tracker["jobs"].pop(job_id, None)
            for search in tracker.get("searches", {}).values():
                if job_id in search.get("job_ids", []):
                    search["job_ids"].remove(job_id)

    def remove_search(self, search_name: str) -> list[str]:
        with self._locked_tracker() as tracker:
            search = tracker.get("searches", {}).pop(search_name, None)
            if not search:
                return []
            job_ids = search.get("job_ids", [])
            for jid in job_ids:
                tracker["jobs"].pop(jid, None)
        return job_ids

    def update_job_status(self, job_id: str, status: str):
        with self._locked_tracker() as tracker:
            if job_id in tracker["jobs"]:
                tracker["jobs"][job_id]["status"] = status

    def record_pending_jobs(self, search_name: str, config_file: str, jobs: list) -> list[str]:
        with self._locked_tracker() as tracker:
            now = datetime.now().isoformat(timespec="seconds")
            job_ids = []
            for j in jobs:
                job_dict = j.to_dict() if hasattr(j, "to_dict") else j
                queued_id = "queued-" + hashlib.md5(job_dict["name"].encode()).hexdigest()[:12]
                job_ids.append(queued_id)
                tracker["jobs"][queued_id] = {
                    "name": job_dict["name"],
                    "search_name": search_name,
                    "params": job_dict.get("params", {}),
                    "status": "Queued",
                    "submitted_at": now,
                    "job_data": job_dict,
                }

            existing_search = tracker["searches"].get(search_name)
            old_job_ids = existing_search["job_ids"] if existing_search else []
            new_names = {(j.name if hasattr(j, "name") else j["name"]) for j in jobs}
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

    @staticmethod
    def extract_platform_ids(experiment_json: dict) -> dict | None:
        storage = experiment_json.get("storage_info", [])
        if not storage:
            return None
        entry = storage[0]
        user_id = entry.get("user_id")
        volumes_path = entry.get("volumes_path", "")
        match = re.search(r"/([0-9a-f-]{36})_([0-9a-f-]{36})/", volumes_path)
        if not match or not user_id:
            return None
        result = {
            "projsetId": match.group(1),
            "projId": match.group(2),
            "userId": str(user_id),
        }
        if experiment_json.get("projset_name"):
            result["projsetName"] = experiment_json["projset_name"]
        if experiment_json.get("project_name"):
            result["projectName"] = experiment_json["project_name"]
        return result

    @staticmethod
    def _find_jrun_dir(start: Path | None = None) -> Path:
        current = (start or Path.cwd()).resolve()
        while True:
            candidate = current / JRUN_DIR
            if candidate.is_dir() and (candidate / SETTINGS_FILE).is_file():
                return candidate
            parent = current.parent
            if parent == current:
                break
            current = parent
        raise FileNotFoundError(
            f"No {JRUN_DIR}/ directory found. Run 'jrun init' first."
        )

    def _load_settings(self) -> dict:
        path = self.jrun_dir / SETTINGS_FILE
        return json.loads(path.read_text())


class _LockedTracker:
    def __init__(self, project: Project):
        self._project = project
        self._lock_fd = None
        self.data = None

    def __enter__(self) -> dict:
        lock_path = self._project.jrun_dir / ".jobs.lock"
        self._lock_fd = open(lock_path, "w")
        fcntl.flock(self._lock_fd, fcntl.LOCK_EX)
        self.data = self._project.load_tracker()
        return self.data

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is None:
            self._project._save_tracker(self.data)
        self._lock_fd.close()
        return False


def extract_platform_ids(experiment_json: dict) -> dict | None:
    return Project.extract_platform_ids(experiment_json)
