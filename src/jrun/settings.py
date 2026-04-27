import json
import re
from pathlib import Path
from urllib.parse import urlencode

JRUN_DIR = ".jrun"
SETTINGS_FILE = "settings.json"

PLATFORM_BASE_URL = "http://platform-cuihu.jingneng-inner.ac.cn/platform/modelTraining/jobList/jobDetail"


def find_jrun_dir(start: Path | None = None) -> Path:
    """Walk up from start directory to find .jrun/ directory."""
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


def load_settings(start: Path | None = None) -> tuple[dict, Path]:
    jrun_dir = find_jrun_dir(start)
    path = jrun_dir / SETTINGS_FILE
    return json.loads(path.read_text()), jrun_dir


def save_settings(data: dict, directory: Path | None = None) -> Path:
    directory = directory or Path.cwd()
    jrun_dir = directory.resolve() / JRUN_DIR
    jrun_dir.mkdir(exist_ok=True)
    path = jrun_dir / SETTINGS_FILE
    path.write_text(json.dumps(data, indent=2) + "\n")
    return jrun_dir


def extract_platform_ids(experiment_json: dict) -> dict | None:
    """Extract projsetId, projId, userId from airsctl experiment list output."""
    storage = experiment_json.get("storage_info", [])
    if not storage:
        return None
    entry = storage[0]
    user_id = entry.get("user_id")
    volumes_path = entry.get("volumes_path", "")
    # volumes_path looks like: /mnt/.../1449ba9c-..._102db38c-.../10172
    match = re.search(r"/([0-9a-f-]{36})_([0-9a-f-]{36})/", volumes_path)
    if not match or not user_id:
        return None
    return {
        "projsetId": match.group(1),
        "projId": match.group(2),
        "userId": str(user_id),
    }


def build_job_url(settings: dict, job_id: str) -> str | None:
    """Build a platform URL for a specific job."""
    platform = settings.get("platform")
    if not platform:
        return None
    params = {
        "id": job_id,
        "name": settings.get("experiment_name", ""),
        "projId": platform["projId"],
        "projsetId": platform["projsetId"],
        "userId": platform["userId"],
    }
    return f"{PLATFORM_BASE_URL}?{urlencode(params)}"
