import os
import re
import itertools
from pathlib import Path

import yaml


def load_config(config_path: str) -> dict:
    config_path = Path(config_path).resolve()
    with open(config_path) as f:
        raw = f.read()

    config_dir = str(config_path.parent)
    raw = raw.replace("$CONFIG_DIR", config_dir)
    raw = raw.replace("$HOME", os.environ.get("HOME", "~"))

    config = yaml.safe_load(raw)
    return config


def _resolve_auto(name_template: str, params: dict | None = None) -> str:
    """Replace {auto:5s} with a deterministic hash based on params."""
    import hashlib
    def replacer(m):
        seed = str(sorted((params or {}).items())).encode()
        length = int(re.search(r"\d+", m.group()).group())
        return hashlib.md5(seed).hexdigest()[:length]
    return re.sub(r"\{auto:(\d+)s?\}", replacer, name_template)


def _resolve_command(cmd_raw, mapping: dict) -> str:
    """Resolve a command string or list into a single string with params substituted."""
    if isinstance(cmd_raw, list):
        parts = []
        for cmd in cmd_raw:
            resolved = str(cmd)
            for k, v in mapping.items():
                resolved = resolved.replace(f"{{{k}}}", str(v))
            resolved = resolved.replace("$$", "$")
            parts.append(resolved)
        return " && ".join(parts)
    else:
        resolved = str(cmd_raw)
        for k, v in mapping.items():
            resolved = resolved.replace(f"{{{k}}}", str(v))
        resolved = resolved.replace("$$", "$")
        return resolved


def expand_grid(config: dict) -> list[dict]:
    """Expand search params into a list of jobs.

    Returns: [{"name": ..., "command": "...", "params": {...}, "resource_config": {...}}, ...]
    """
    search = config.get("search")
    if not search:
        return []

    template = search["job_template"]
    params = search.get("params", [])
    max_trials = search.get("max_trials", 9999)

    param_names = [p["name"] for p in params]
    param_values = [p["values"] for p in params]

    jobs = []
    for combo in itertools.product(*param_values):
        if len(jobs) >= max_trials:
            break

        mapping = dict(zip(param_names, combo))

        name = template["name"]
        for k, v in mapping.items():
            name = name.replace(f"{{{k}}}", str(v))
        name = _resolve_auto(name, mapping)

        command = _resolve_command(template.get("commands", template.get("command", "")), mapping)

        envs = template.get("envs", {})
        if envs:
            resolved_envs = {}
            for ek, ev in envs.items():
                val = str(ev)
                for k, v in mapping.items():
                    val = val.replace(f"{{{k}}}", str(v))
                val = val.replace("$$", "$")
                resolved_envs[ek] = val
            envs = resolved_envs

        jobs.append({
            "name": name,
            "command": command,
            "params": mapping,
            "resource_config": template.get("resource_config", {}),
            "envs": envs,
        })

    return jobs


def build_single_job(config: dict) -> dict | None:
    """Build a single job from the 'job' section."""
    job = config.get("job")
    if not job:
        return None
    cmd_raw = job.get("commands", job.get("command", ""))
    if isinstance(cmd_raw, list):
        command = " && ".join(str(c) for c in cmd_raw)
    else:
        command = str(cmd_raw)
    return {
        "name": job["name"],
        "command": command,
        "params": {},
        "resource_config": job.get("resource_config", {}),
        "envs": job.get("envs", {}),
    }


def get_search_name(config: dict) -> str | None:
    """Get search name from explicit field, or derive from job template name."""
    search = config.get("search")
    if not search:
        return None
    if "name" in search:
        return search["name"]
    name_tmpl = search["job_template"]["name"]
    clean = re.sub(r"\{[^}]+\}", "", name_tmpl)
    clean = re.sub(r"_+", "_", clean).strip("_")
    return clean
