import json
from pathlib import Path

JRUN_DIR = ".jrun"
SETTINGS_FILE = "settings.json"


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
