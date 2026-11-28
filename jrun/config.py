import hashlib
import itertools
import os
import re
from dataclasses import fields
from pathlib import Path

import yaml

from jrun.errors import ConfigError
from jrun.models import ExperimentSpec, Job, RemoteSpec

# Keys that used to be accepted on search:/job: sections. They are rejected
# with a pointer to the replacement instead of being silently forwarded to the
# platform as unknown fields.
_REMOVED_SECTION_KEYS = {
    "experiment_id": "bind the config with a top-level 'experiment:' block",
    "experiment_name": "bind the config with a top-level 'experiment:' block",
    "submission": "use 'run_options:'",
}


class ConfigLoader:
    """Load a jrun YAML config and expand it into Job objects.

    Every validation problem is raised as ConfigError.
    """

    def __init__(self, config_path: str):
        self.path = Path(config_path).resolve()
        self.config = self._load_and_resolve()

    def _load_and_resolve(self) -> dict:
        try:
            raw = self.path.read_text()
        except OSError as e:
            raise ConfigError(f"cannot read config {self.path}: {e}") from e

        jrun_vars = {"CONFIG_DIR": str(self.path.parent)}

        placeholder = "\x00DOLLAR\x00"
        raw = raw.replace("$$", placeholder)

        def _resolve(m):
            name = m.group(1)
            if name in jrun_vars:
                return jrun_vars[name]
            val = os.environ.get(name)
            if val is not None:
                return val
            raise ConfigError(
                f"undefined variable ${name} (use $${name} to pass it through "
                "to the remote job)"
            )

        # Substitute line by line, skipping comment-only lines.
        lines = [
            line if line.lstrip().startswith("#")
            else re.sub(r"\$([A-Za-z_][A-Za-z0-9_]*)", _resolve, line)
            for line in raw.split("\n")
        ]
        raw = "\n".join(lines).replace(placeholder, "$")

        try:
            data = yaml.safe_load(raw)
        except yaml.YAMLError as e:
            raise ConfigError(f"invalid YAML in {self.path}: {e}") from e
        if not isinstance(data, dict):
            raise ConfigError(f"{self.path}: top level must be a mapping")
        for section_name in ("search", "job"):
            section = data.get(section_name) or {}
            for key, hint in _REMOVED_SECTION_KEYS.items():
                if key in section:
                    raise ConfigError(
                        f"{section_name}.{key} is no longer supported; {hint}"
                    )
        template = (data.get("search") or {}).get("job_template") or {}
        for key, hint in _REMOVED_SECTION_KEYS.items():
            if key in template:
                raise ConfigError(
                    f"search.job_template.{key} is no longer supported; {hint}"
                )
        return data

    # --- jobs ---

    def jobs(self) -> list[Job]:
        """All jobs in the config: the expanded grid, or the single job."""
        if self.config.get("search"):
            return self.expand_grid()
        single = self.build_single_job()
        if not single:
            raise ConfigError("config has neither 'search' nor 'job' section")
        return [single]

    @property
    def is_search(self) -> bool:
        return bool(self.config.get("search"))

    def expand_grid(self) -> list[Job]:
        search = self.config.get("search")
        if not search:
            return []

        sampling = search.get("sampling", "grid")
        if sampling != "grid":
            raise ConfigError(
                f"unsupported sampling method '{sampling}'; only 'grid' is supported"
            )
        template = search.get("job_template")
        if not isinstance(template, dict) or not template.get("name"):
            raise ConfigError("search.job_template with a 'name' is required")

        params = search.get("params", [])
        max_trials = search.get("max_trials", 9999)
        try:
            param_names = [p["name"] for p in params]
            param_values = [p["values"] for p in params]
        except (KeyError, TypeError) as e:
            raise ConfigError("each search param needs 'name' and 'values'") from e

        jobs = []
        for combo in itertools.product(*param_values):
            if len(jobs) >= max_trials:
                break
            mapping = dict(zip(param_names, combo))
            data = self._substitute(template, mapping)
            data["name"] = self._resolve_auto(data["name"], mapping)
            jobs.append(self._make_job(data, params=mapping))
        return jobs

    def build_single_job(self) -> Job | None:
        job = self.config.get("job")
        if not job:
            return None
        return self._make_job(dict(job), params={})

    def _make_job(self, data: dict, params: dict) -> Job:
        data["command"] = self._join_command(
            data.pop("commands", None) if "commands" in data else data.get("command", "")
        )
        data["params"] = params
        data["envs"] = {str(k): str(v) for k, v in (data.get("envs") or {}).items()}
        # Unknown job keys are typos, not platform fields: raw platform fields
        # must go through `platform_overrides:` explicitly.
        unknown = sorted(set(data) - {f.name for f in fields(Job)})
        if unknown:
            raise ConfigError(
                f"job '{data.get('name')}': unknown field(s) {', '.join(unknown)} "
                "(put raw platform fields under platform_overrides:)"
            )
        try:
            return Job.from_dict(data)
        except ValueError as e:
            raise ConfigError(str(e)) from e

    # --- other sections ---

    def get_search_name(self) -> str | None:
        search = self.config.get("search")
        if not search:
            return None
        if "name" in search:
            return search["name"]
        name_tmpl = search["job_template"]["name"]
        clean = re.sub(r"\{[^}]+\}", "", name_tmpl)
        return re.sub(r"_+", "_", clean).strip("_")

    def get_experiment_spec(self) -> ExperimentSpec | None:
        """Parse the top-level `experiment:` block, or None if absent."""
        section = self.config.get("experiment")
        if not section:
            return None
        try:
            return ExperimentSpec.from_dict(section)
        except ValueError as e:
            raise ConfigError(str(e)) from e

    def get_remote(self) -> RemoteSpec | None:
        """Parse the top-level `remote:` block, or None if absent."""
        section = self.config.get("remote")
        if section is None:
            return None
        try:
            return RemoteSpec.from_dict(section)
        except ValueError as e:
            raise ConfigError(str(e)) from e

    def select(self, jobs: list[Job], only) -> list[Job]:
        """Keep only the named jobs (``submit --only``); unknown names fail."""
        if not only:
            return jobs
        wanted = list(dict.fromkeys(only))
        names = {j.name for j in jobs}
        missing = [n for n in wanted if n not in names]
        if missing:
            raise ConfigError(f"--only: no job named {', '.join(missing)} in {self.path.name}")
        keep = set(wanted)
        return [j for j in jobs if j.name in keep]

    def get_scheduler_params(self) -> dict:
        search = self.config.get("search") or {}
        return {
            "parallel_trials": search.get("parallel_trials"),
            "poll_interval": search.get("poll_interval", 30),
        }

    # --- substitution helpers ---

    @classmethod
    def _substitute(cls, value, mapping: dict):
        """Substitute {param} placeholders through nested config values."""
        if isinstance(value, dict):
            return {k: cls._substitute(v, mapping) for k, v in value.items()}
        if isinstance(value, list):
            return [cls._substitute(v, mapping) for v in value]
        if isinstance(value, str):
            for k, v in mapping.items():
                value = value.replace(f"{{{k}}}", str(v))
        return value

    @staticmethod
    def _resolve_auto(name_template: str, params: dict | None = None) -> str:
        def replacer(m):
            seed = str(sorted((params or {}).items())).encode()
            return hashlib.md5(seed).hexdigest()[:int(m.group(1))]
        return re.sub(r"\{auto:(\d+)s?\}", replacer, name_template)

    @staticmethod
    def _join_command(cmd_raw) -> str:
        """`commands:` lists are joined with newlines (one shell line each)."""
        if isinstance(cmd_raw, list):
            return "\n".join(str(c) for c in cmd_raw)
        return "" if cmd_raw is None else str(cmd_raw)
