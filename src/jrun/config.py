import hashlib
import itertools
import os
import re
from pathlib import Path

import yaml

from jrun.models import Job, ResourceConfig


class ConfigLoader:
    def __init__(self, config_path: str):
        self.path = Path(config_path).resolve()
        self.config = self._load_and_resolve()

    def _load_and_resolve(self) -> dict:
        with open(self.path) as f:
            raw = f.read()

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
            raise ValueError(f"Undefined variable: ${name}")

        raw = re.sub(r"\$([A-Za-z_][A-Za-z0-9_]*)", _resolve, raw)
        raw = raw.replace(placeholder, "$")

        return yaml.safe_load(raw)

    def expand_grid(self) -> list[Job]:
        search = self.config.get("search")
        if not search:
            return []

        sampling = search.get("sampling", "grid")
        if sampling != "grid":
            raise ValueError(f"Unsupported sampling method: '{sampling}'. Only 'grid' is supported.")

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
            name = self._resolve_auto(name, mapping)

            command = self._resolve_command(
                template.get("commands", template.get("command", "")), mapping
            )

            envs = template.get("envs", {})
            if envs:
                resolved_envs = {}
                for ek, ev in envs.items():
                    val = str(ev)
                    for k, v in mapping.items():
                        val = val.replace(f"{{{k}}}", str(v))
                    resolved_envs[ek] = val
                envs = resolved_envs

            jobs.append(Job(
                name=name,
                command=command,
                params=mapping,
                resource_config=ResourceConfig.from_dict(template.get("resource_config", {})),
                envs=envs,
            ))

        return jobs

    def build_single_job(self) -> Job | None:
        job = self.config.get("job")
        if not job:
            return None
        command = self._resolve_command(job.get("commands", job.get("command", "")), {})
        return Job(
            name=job["name"],
            command=command,
            params={},
            resource_config=ResourceConfig.from_dict(job.get("resource_config", {})),
            envs=job.get("envs", {}),
        )

    def get_search_name(self) -> str | None:
        search = self.config.get("search")
        if not search:
            return None
        if "name" in search:
            return search["name"]
        name_tmpl = search["job_template"]["name"]
        clean = re.sub(r"\{[^}]+\}", "", name_tmpl)
        clean = re.sub(r"_+", "_", clean).strip("_")
        return clean

    def get_scheduler_params(self) -> dict:
        search = self.config.get("search", {})
        return {
            "parallel_trials": search.get("parallel_trials"),
            "poll_interval": search.get("poll_interval", 30),
        }

    @staticmethod
    def _resolve_auto(name_template: str, params: dict | None = None) -> str:
        def replacer(m):
            seed = str(sorted((params or {}).items())).encode()
            length = int(re.search(r"\d+", m.group()).group())
            return hashlib.md5(seed).hexdigest()[:length]
        return re.sub(r"\{auto:(\d+)s?\}", replacer, name_template)

    @staticmethod
    def _resolve_command(cmd_raw, mapping: dict) -> str:
        if isinstance(cmd_raw, list):
            parts = []
            for cmd in cmd_raw:
                resolved = str(cmd)
                for k, v in mapping.items():
                    resolved = resolved.replace(f"{{{k}}}", str(v))
                parts.append(resolved)
            return "\n".join(parts)
        else:
            resolved = str(cmd_raw)
            for k, v in mapping.items():
                resolved = resolved.replace(f"{{{k}}}", str(v))
            return resolved
