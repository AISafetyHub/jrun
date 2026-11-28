"""PlatformClient: jrun's experiment/job logic on top of the airsctl CLI.

The raw subcommands live in ``jrun.jiuding.airsctl.Airsctl``; this module
handles experiment documents, the config working set, and job parsing.
airsctl prints and accepts snake_case JSON (verified against real
`experiment list -e` / `job list -j` output), so every document handled here
uses snake_case keys only.
"""

import copy
import json
import re
import subprocess
import tempfile
from pathlib import Path

from jrun.errors import PlatformError
from jrun.jiuding.airsctl import Airsctl
from jrun.jiuding.enums import DEFAULT_STORAGE_MEDIUM, DirectoryType
from jrun.models import (DETAIL_FIELDS, IMAGE_REGION_VALUES, JOB_CONFIG_FIELDS,
                         CATEGORY_PRIORITY, DEFAULT_IMAGE_REGION, ExperimentSpec,
                         Job, JobTarget, RunOptions, job_target, mount_entries,
                         queue_category_value)

_UUID_RE = re.compile(r"^[0-9a-fA-F-]{36}$")
_JOB_STATUSES = ("cancelled", "failed", "stopped", "succeed", "completed",
                 "running", "starting", "scheduling", "queued", "submitted")


class PlatformClient(Airsctl):
    def __init__(self, **airsctl_options):
        super().__init__(**airsctl_options)
        # One submit needs the experiment detail for the config-count warning
        # and as the modify template; cache it to avoid a second read.
        self._experiment_cache: dict[str, dict] = {}

    def set_context(self, proj_id: str | None, projset_id: str | None):
        super().set_context(proj_id, projset_id)
        # Experiment lookups are project-scoped: a new context may differ.
        self._experiment_cache.clear()

    def experiment_modify(self, config_path: str) -> subprocess.CompletedProcess:
        result = super().experiment_modify(config_path)
        self._experiment_cache.clear()
        return result

    def job_run(self, exp_id: str, config_name: str,
                run_options: RunOptions | None = None) -> subprocess.CompletedProcess:
        """``airsctl job run`` with the flags of the job's run_options."""
        options = run_options or RunOptions()
        return super().job_run(
            exp_id, config_name,
            duration_hours=options.duration_hours,
            enable_checkpoint=bool(options.enable_checkpoint),
            queue_priority_mode=options.queue_priority_mode,
            usage_desc=options.usage_desc,
        )

    # --- jobs ---

    def get_job_status(self, job_id: str) -> str | None:
        info = self.get_job_info(job_id)
        return info.get("status") if info else None

    def get_job_info(self, job_id: str) -> dict | None:
        """Parsed `job list -j` document (status capitalized), or None."""
        result = self.job_list(job_id, check=False)
        if result.returncode != 0 or not result.stdout:
            return None
        status = self._parse_job_status(result.stdout)
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            data = None
        if not isinstance(data, dict):
            return {"status": status} if status else None
        if status:
            data["status"] = status
        return data

    @staticmethod
    def _parse_job_status(output: str) -> str | None:
        try:
            data = json.loads(output)
            if isinstance(data, dict) and isinstance(data.get("status"), str):
                return data["status"].capitalize()
        except json.JSONDecodeError:
            pass
        for line in output.splitlines():
            lower = line.lower()
            for s in _JOB_STATUSES:
                if s in lower:
                    return s.capitalize()
        return None

    def run_config(self, exp_id: str, config_name: str,
                   run_options: RunOptions | None = None,
                   queue_category: str | None = None) -> str:
        """Start an already-installed config; returns the new job ID."""
        if not exp_id:
            raise PlatformError("job run", 1, stderr="experiment_id is required")
        if not config_name:
            raise PlatformError("job run", 1, stderr="config_name is required")
        options = run_options or RunOptions()
        try:
            options.validate_for_category(queue_category)
        except ValueError as e:
            raise PlatformError("job run", 1, stderr=str(e)) from e
        result = self.job_run(exp_id, config_name, options)
        job_id = self._parse_job_id(result.stdout)
        if not job_id:
            raise PlatformError(
                "job run", 1,
                stderr=f"could not find a job ID in airsctl output for {config_name}",
                stdout=result.stdout or "",
            )
        return job_id

    @staticmethod
    def _parse_job_id(output: str | None) -> str | None:
        """Extract a job ID from `airsctl job run` output (JSON or text)."""
        text = output or ""
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = None

        def find_id(value):
            if isinstance(value, dict):
                for key in ("job_id", "jobId", "id"):
                    if isinstance(value.get(key), str) and value[key]:
                        return value[key]
                values = value.values()
            elif isinstance(value, list):
                values = value
            else:
                return None
            return next((c for c in map(find_id, values) if c), None)

        if data is not None:
            return find_id(data)
        labelled = re.search(r"\bjob(?:[_ ]+id)?\s*[:=]\s*([A-Za-z0-9._-]+)",
                             text, re.IGNORECASE)
        if labelled:
            return labelled.group(1)
        match = re.search(r"\b[Jj]ob\b.*?([0-9a-fA-F-]{36})", text)
        return match.group(1) if match else None

    # --- experiments ---

    def _load_experiment(self, result: subprocess.CompletedProcess,
                         check: bool, what: str) -> dict | None:
        """Parse an `experiment list -e/-N` detail response."""
        def fail(message):
            if check:
                raise PlatformError("experiment list", result.returncode,
                                    stderr=message, stdout=(result.stdout or "")[:200])
            return None

        if result.returncode != 0 or not result.stdout:
            return fail(result.stderr or f"empty response for experiment {what}")
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError as e:
            return fail(f"invalid JSON for experiment {what}: {e}")
        if not isinstance(data, dict) or not data.get("experiment_id"):
            return fail(f"no experiment document for {what}")
        data.setdefault("advance_config_infos", [])
        self._experiment_cache[data["experiment_id"]] = data
        return copy.deepcopy(data)

    def get_experiment(self, exp_id: str, check: bool = False) -> dict | None:
        """Experiment detail document (cached for this client's lifetime)."""
        if exp_id in self._experiment_cache:
            return copy.deepcopy(self._experiment_cache[exp_id])
        result = self.experiment_list(exp_id=exp_id, check=check)
        return self._load_experiment(result, check, exp_id)

    def find_experiment(self, name_or_id: str) -> dict | None:
        """Look up an experiment by UUID or exact name; None if not found."""
        if _UUID_RE.match(name_or_id):
            return self.get_experiment(name_or_id)
        result = self.experiment_list(exp_name=name_or_id, check=False)
        data = self._load_experiment(result, False, name_or_id)
        if data and data.get("experiment_name") not in (None, name_or_id):
            return None
        return data

    @staticmethod
    def experiment_configs(data: dict | None) -> list:
        configs = (data or {}).get("advance_config_infos")
        return configs if isinstance(configs, list) else []

    def _write_experiment(self, document: dict):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", prefix="jrun_", delete=False
        ) as f:
            json.dump(document, f, indent=2)
            tmp_path = f.name
        try:
            self.experiment_modify(tmp_path)
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    def modify_configs(self, jobs: list[Job], exp_id: str,
                       spec: ExperimentSpec | None = None,
                       queue_locations: dict | None = None,
                       experiment_data: dict | None = None) -> list[str]:
        """Replace the experiment's config working set in one modify call.

        The resulting ``advance_config_infos`` contains exactly ``jobs`` in
        order. The first existing config is only a structural template; old
        configs are not retained. Already-started jobs are not affected.
        """
        jobs = list(jobs or [])
        if not exp_id:
            raise PlatformError("experiment modify", 1, stderr="experiment_id is required")
        if not jobs:
            raise PlatformError("experiment modify", 1,
                                stderr="no jobs to put in the experiment config set")
        names = [job.name for job in jobs]
        if not all(isinstance(n, str) and n.strip() for n in names):
            raise PlatformError("experiment modify", 1,
                                stderr="every job must have a non-empty name")
        duplicates = sorted({n for n in names if names.count(n) > 1})
        if duplicates:
            raise PlatformError("experiment modify", 1,
                                stderr="duplicate job names: " + ", ".join(duplicates))

        document = copy.deepcopy(experiment_data) if experiment_data else \
            self.get_experiment(exp_id, check=True)
        configs = self.experiment_configs(document)
        if not configs or not isinstance(configs[0], dict):
            raise PlatformError("experiment list", 0,
                                stderr="experiment has no existing config to use as template")
        template = configs[0]

        locations = queue_locations or {}
        new_configs = []
        for job in jobs:
            target = job_target(job, spec)
            location = locations.get(target.location_key) if target.queue_name else None
            new_configs.append(self._build_job_config(job, template, target, location))

        if spec:
            for key in ExperimentSpec.DOCUMENT_FIELDS:
                value = getattr(spec, key)
                if value is not None:
                    document[key] = value
            if spec.mounts is not None:
                document["storage_info"] = self._storage_info(document, spec.mounts)
            document.update(spec.platform_overrides)
        document["advance_config_infos"] = new_configs
        self._write_experiment(document)
        return names

    @staticmethod
    def _storage_info(document: dict, mounts: dict) -> list[dict]:
        """snake_case ``storage_info`` for ``experiment.mounts``.

        An existing entry of the same directory type is reused (it carries
        platform fields such as user_id/medium); a new home entry gets the
        experiment creator as its user_id.
        """
        existing = {}
        for entry in document.get("storage_info") or []:
            if isinstance(entry, dict) and "directory_type" in entry:
                existing.setdefault(entry["directory_type"], entry)
        result = []
        for directory_type, access_mode in mount_entries(mounts):
            entry = copy.deepcopy(existing.get(directory_type)) or {
                "directory_type": directory_type, "medium": DEFAULT_STORAGE_MEDIUM}
            if directory_type == DirectoryType.HOME and "user_id" not in entry \
                    and document.get("creator_id") is not None:
                entry["user_id"] = document["creator_id"]
            entry["access_mode"] = access_mode
            result.append(entry)
        return result

    def remove_configs(self, exp_id: str, config_names: set[str]):
        """Best-effort: drop the named configs from an experiment."""
        document = self.get_experiment(exp_id, check=False)
        if not document:
            return
        configs = self.experiment_configs(document)
        kept = [c for c in configs if c.get("config_name") not in config_names]
        if len(kept) == len(configs):
            return
        document["advance_config_infos"] = kept
        self._write_experiment(document)

    # --- advance config construction ---

    @classmethod
    def _build_job_config(cls, job: Job, template: dict, target: JobTarget,
                          location: dict | None) -> dict:
        """Clone the template config and apply one job's values."""
        config = copy.deepcopy(template)
        config["config_name"] = job.name
        config["command"] = job.command
        config.pop("conf_id", None)
        config.pop("config_id", None)

        resources = config.get("resource_config_list")
        if resources and isinstance(resources[0], dict):
            cls._apply_resource_config(resources[0], job, target, location or {})

        # Raw overrides first so the typed fields below win.
        config.update(job.platform_overrides)
        for key in JOB_CONFIG_FIELDS:
            value = getattr(job, key)
            if value is not None:
                config[key] = value

        if job.envs:
            envs = {str(k): str(v) for k, v in job.envs.items()}
            config["hyper_parameter"] = envs
            if "hyper_parameter_present" in config:
                config["hyper_parameter_present"] = [
                    {"key": k, "value": v} for k, v in envs.items()
                ]
        else:
            config.pop("hyper_parameter", None)
            config.pop("hyper_parameter_present", None)
        return config

    @staticmethod
    def _apply_resource_config(res: dict, job: Job, target: JobTarget,
                               location: dict):
        """Apply image, queue placement, and resources to resource_config_list[0]."""
        rc = job.resource_config

        # Image. airsctl parses the modify JSON with plain encoding/json, so
        # the RepoTypes enum must be the integer value, not the name.
        region = target.image_region or DEFAULT_IMAGE_REGION
        if target.image:
            res["basic_image"] = target.image
            res["image_region"] = IMAGE_REGION_VALUES[region]
        elif target.image_region and target.image_region != DEFAULT_IMAGE_REGION:
            res["image_region"] = IMAGE_REGION_VALUES[region]

        # Queue placement: IDs come only from the API-resolved location.
        for key in ("queue_id", "quota_id", "shared_quota_id", "cluster_id", "zone_id"):
            if location.get(key):
                res[key] = location[key]
        if target.queue_name:
            res["queue_name"] = target.queue_name
        if target.queue_name or target.queue_category:
            res["queue_category"] = queue_category_value(target.queue_category)
        region_name = target.region_name or location.get("region_name")
        zone_name = target.zone_name or location.get("zone_name")
        if region_name:
            res["cluster_display_name"] = region_name
        if zone_name:
            res["zone_display_name"] = zone_name

        res.update(rc.platform_overrides)
        for key in ("priority", "pod_restart_policy", "restart_scope", "selected_nodes"):
            value = getattr(rc, key)
            if value is not None:
                res[key] = value
        if rc.priority is None and target.category in CATEGORY_PRIORITY:
            # Public/idle accept exactly one priority; the template's value
            # (cloned from whatever config came first) may not match.
            res["priority"] = CATEGORY_PRIORITY[target.category]

        # Roles. The template's first role is the master; its request detail
        # is the base that resource_config fields and each role override.
        roles = res.get("role_info_list") or []
        master = copy.deepcopy(roles[0]) if roles else {"name": "Master", "replicas": 1}
        detail = dict(master.get("resource_request_detail") or {})
        if location.get("accelerator_model") and rc.accelerator_model is None:
            detail["accelerator_model"] = location["accelerator_model"]
        for key in DETAIL_FIELDS:
            if getattr(rc, key) is not None:
                detail[key] = getattr(rc, key)
        resource_region = (rc.resource_region if rc.resource_region is not None
                           else master.get("resource_region"))

        if not rc.roles:
            master["resource_request_detail"] = detail
            if resource_region is not None:
                master["resource_region"] = resource_region
            res["role_info_list"] = [master]
            return

        role_list = []
        for role in rc.roles:
            role_detail = dict(detail)
            for key in DETAIL_FIELDS:
                if getattr(role, key) is not None:
                    role_detail[key] = getattr(role, key)
            entry = {"name": role.name, "replicas": role.replicas,
                     "resource_request_detail": role_detail}
            role_region = (role.resource_region if role.resource_region is not None
                           else resource_region)
            if role_region is not None:
                entry["resource_region"] = role_region
            if role.extra_label_info:
                entry["extra_label_info"] = role.extra_label_info
            entry.update(role.platform_overrides)
            role_list.append(entry)
        res["role_info_list"] = role_list
