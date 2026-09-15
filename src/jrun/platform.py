import json
import re
import subprocess
import tempfile
import time
from pathlib import Path

from jrun.errors import PlatformError
from jrun.models import ExperimentSpec, Job, effective_spec


class PlatformClient:
    def __init__(self):
        self._cmd = "airsctl"

        # A submit operation needs the complete experiment document both for
        # the config-count warning and for building the replacement payload.
        # Keep the parsed response around so those two steps do not issue two
        # identical `experiment list` calls.
        self._experiment_cache: dict[str, dict] = {}

    _UNAUTHENTICATED_PATTERN = "Unauthenticated"
    _RETRY_DELAY = 35

    @staticmethod
    def _prefers_camel(data: dict | None) -> bool:
        """Infer whether a platform object uses lowerCamelCase fields."""
        if not isinstance(data, dict):
            return False
        return any(re.search(r"[a-z][A-Z]", str(key)) for key in data)

    @classmethod
    def _field_key(cls, data: dict, snake: str, camel: str,
                   prefer_camel: bool | None = None) -> str:
        """Choose the field spelling already used by a platform document.

        Airsctl responses are usually snake_case while API-shaped documents
        use lowerCamelCase.  A field may be absent from a sparse template, so
        infer the spelling from neighboring fields in that object (or let the
        caller pass the parent object's style).
        """
        if snake in data:
            return snake
        if camel in data:
            return camel
        if prefer_camel is None:
            prefer_camel = cls._prefers_camel(data)
        return camel if prefer_camel else snake

    @classmethod
    def _set_shaped_field(cls, data: dict, snake: str, camel: str, value,
                          prefer_camel: bool | None = None):
        data[cls._field_key(data, snake, camel, prefer_camel)] = value

    def _run(self, args: list[str], check: bool = True) -> subprocess.CompletedProcess:
        result = subprocess.run(
            [self._cmd] + args,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0 and self._is_unauthenticated(result):
            time.sleep(self._RETRY_DELAY)
            result = subprocess.run(
                [self._cmd] + args,
                capture_output=True,
                text=True,
            )
        if check and result.returncode != 0:
            command = " ".join(args[:2]) if len(args) >= 2 else " ".join(args)
            raise PlatformError(command, result.returncode, result.stderr, result.stdout)
        return result

    @staticmethod
    def _is_unauthenticated(result: subprocess.CompletedProcess) -> bool:
        output = (result.stderr or "") + (result.stdout or "")
        return "Unauthenticated" in output

    def experiment_list(self, exp_name: str | None = None, exp_id: str | None = None, check: bool = True) -> subprocess.CompletedProcess:
        args = ["experiment", "list"]
        if exp_id:
            args += ["-e", exp_id]
        elif exp_name:
            args += ["-N", exp_name]
        return self._run(args, check=check)

    def experiment_modify(self, config_path: str) -> subprocess.CompletedProcess:
        result = self._run(["experiment", "modify", "-f", config_path])
        # The modified document may no longer match a cached snapshot.
        self._experiment_cache.clear()
        return result

    def job_list(self, job_id: str | None = None, check: bool = False) -> subprocess.CompletedProcess:
        args = ["job", "list"]
        if job_id:
            args += ["-j", job_id]
        return self._run(args, check=check)

    def job_run(
        self,
        exp_name: str | None = None,
        exp_id: str | None = None,
        config_name: str | None = None,
        config_id: str | None = None,
    ) -> subprocess.CompletedProcess:
        args = ["job", "run"]
        if exp_id:
            args += ["-e", exp_id]
        elif exp_name:
            args += ["-N", exp_name]
        if config_name:
            args += ["-n", config_name]
        elif config_id:
            args += ["-c", config_id]
        return self._run(args)

    def job_stop(self, job_id: str) -> subprocess.CompletedProcess:
        return self._run(["job", "stop", "-j", job_id])

    def job_cancel(self, job_id: str) -> subprocess.CompletedProcess:
        return self._run(["job", "cancel", "-j", job_id])

    def get_job_status(self, job_id: str) -> str | None:
        info = self.get_job_info(job_id)
        if not info:
            return None
        return info.get("status")

    def get_job_info(self, job_id: str) -> dict | None:
        result = self.job_list(job_id, check=False)
        if result.returncode != 0 or not result.stdout:
            return None
        status = self._parse_job_status(result.stdout)
        try:
            data = json.loads(result.stdout)
        except (json.JSONDecodeError, TypeError):
            return {"status": status} if status else None
        if not isinstance(data, dict):
            return {"status": status} if status else None
        if status:
            data["status"] = status
        return data

    def get_experiment(self, exp_id: str,
                       check: bool = False) -> dict | None:
        """Return one experiment's parsed detail document.

        The detail document contains the complete ``advance_config_infos``
        array used as the structural template for a submit.  Results are
        cached for the lifetime of this client so config counting and the
        subsequent batch modify share one platform read.
        """
        if exp_id in self._experiment_cache:
            return json.loads(json.dumps(self._experiment_cache[exp_id]))

        result = self.experiment_list(exp_id=exp_id, check=check)
        returncode = getattr(result, "returncode", 1)
        stdout = getattr(result, "stdout", "") or ""
        stderr = getattr(result, "stderr", "") or ""
        if returncode != 0 or not stdout:
            if check:
                raise PlatformError(
                    "experiment list", returncode,
                    stderr=stderr or f"empty response for experiment {exp_id}",
                    stdout=stdout,
                )
            return None
        try:
            data = json.loads(stdout)
        except (json.JSONDecodeError, TypeError) as exc:
            if check:
                raise PlatformError(
                    "experiment list", returncode,
                    stderr=f"invalid JSON: {exc}", stdout=stdout[:200],
                ) from exc
            return None
        if not isinstance(data, (dict, list)):
            if check:
                raise PlatformError(
                    "experiment list", returncode,
                    stderr="experiment response is not a JSON object or list",
                )
            return None
        # Some airsctl versions wrap the detail in ``data`` or
        # ``experiment``. Keep the actual document so the replacement payload
        # does not accidentally retain an unrelated response envelope.
        data = self._experiment_document(data)
        if data is None:
            if check:
                raise PlatformError(
                    "experiment list", returncode,
                    stderr="experiment response has no experiment document",
                )
            return None
        self._experiment_cache[exp_id] = data
        return json.loads(json.dumps(data))

    @classmethod
    def _experiment_document(cls, data: dict | list | None) -> dict | None:
        """Return the experiment object from common airsctl response shapes.

        ``airsctl`` versions in the wild have returned the object directly,
        under ``data``/``experiment``/``result``, and inside an ``items``
        array.  Walk those small envelopes recursively so a snapshot can be
        passed unchanged to the batch modifier regardless of the wrapper.
        """
        if isinstance(data, list):
            for item in data:
                document = cls._experiment_document(item)
                if document is not None:
                    return document
            return None
        if not isinstance(data, dict):
            return None
        if "advance_config_infos" in data or "advanceConfigInfos" in data:
            return data

        for key in ("data", "experiment", "result", "item", "items"):
            nested = data.get(key)
            if isinstance(nested, dict):
                document = cls._experiment_document(nested)
                if document is not None:
                    return document
            elif isinstance(nested, list):
                for item in nested:
                    if isinstance(item, dict):
                        document = cls._experiment_document(item)
                        if document is not None:
                            return document

        # An ID-only response cannot supply a structural template. Do not
        # cache it: a later modify call must be allowed to fetch the detail.
        return None

    @classmethod
    def _experiment_configs(cls, data: dict | list | None) -> list:
        document = cls._experiment_document(data)
        if not document:
            return []
        configs = document.get("advance_config_infos")
        if configs is None:
            configs = document.get("advanceConfigInfos")
        return configs if isinstance(configs, list) else []

    @classmethod
    def _experiment_id_from_data(cls, data: dict | list | None) -> str | None:
        """Extract an experiment ID from direct or wrapped list responses."""
        if isinstance(data, list):
            for item in data:
                experiment_id = cls._experiment_id_from_data(item)
                if experiment_id:
                    return experiment_id
            return None
        if not isinstance(data, dict):
            return None
        direct = data.get("experiment_id") or data.get("experimentId")
        if direct:
            return str(direct)

        # Some list endpoints call the primary key simply ``id``.  Only use
        # it when the object looks like an experiment, avoiding unrelated
        # request/envelope IDs.
        if data.get("id") and (
            data.get("name")
            or data.get("experiment_name")
            or data.get("experimentName")
            or "advance_config_infos" in data
            or "advanceConfigInfos" in data
        ):
            return str(data["id"])

        for key in ("data", "experiment", "result", "item", "items"):
            nested = data.get(key)
            if isinstance(nested, dict):
                nested_id = cls._experiment_id_from_data(nested)
                if nested_id:
                    return nested_id
            elif isinstance(nested, list):
                for item in nested:
                    nested_id = cls._experiment_id_from_data(item)
                    if nested_id:
                        return nested_id
        return None

    def count_experiment_configs(self, exp_id: str) -> int | None:
        data = self.get_experiment(exp_id, check=False)
        if not data:
            return None
        return len(self._experiment_configs(data))

    def get_cached_experiment(self, exp_id: str) -> dict | None:
        """Return a cached experiment snapshot without issuing a command."""
        data = self._experiment_cache.get(exp_id)
        return json.loads(json.dumps(data)) if isinstance(data, dict) else None

    _UUID_RE = re.compile(r"^[0-9a-fA-F-]{36}$")

    def find_experiment(self, name_or_id: str) -> dict | None:
        """Look up an experiment by name, or by ID when it looks like a UUID.

        Returns the parsed experiment dict (snake_case fields like
        experiment_id/experiment_name), or None when not found / query failed.
        """
        if self._UUID_RE.match(name_or_id):
            result = self.experiment_list(exp_id=name_or_id, check=False)
        else:
            result = self.experiment_list(exp_name=name_or_id, check=False)
        if result.returncode != 0 or not result.stdout:
            return None
        try:
            data = json.loads(result.stdout)
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(data, (dict, list)):
            return None

        # Normalize the two common response styles for callers such as
        # ``jrun experiment jobs`` while retaining all original fields.
        document = self._experiment_document(data)
        if document is None:
            if isinstance(data, list):
                document = next((item for item in data if isinstance(item, dict)), None)
                if document is None:
                    return None
            else:
                document = data
        normalized = dict(document)
        experiment_id = self._experiment_id_from_data(data)
        if experiment_id:
            normalized.setdefault("experiment_id", experiment_id)
        for key in ("experiment_name", "experimentName", "name"):
            value = document.get(key)
            if value:
                normalized.setdefault("experiment_name", value)
                break
        return normalized

    def modify_configs(
        self,
        jobs: list[Job],
        exp_id: str | None,
        spec: "ExperimentSpec | None" = None,
        queue_locations: dict | None = None,
        experiment_data: dict | list | None = None,
    ) -> list[str]:
        """Replace the experiment config working set in one modify call.

        The resulting ``advance_config_infos`` contains exactly ``jobs`` in
        their supplied order.  The first existing config is used only as a
        platform-specific structural template; old configs are not appended
        or retained.  This keeps the experiment bounded to the current
        submission while leaving already-created jobs to the platform.
        """
        if jobs is None:
            jobs = []
        elif not isinstance(jobs, list):
            jobs = list(jobs)
        if not exp_id:
            raise PlatformError(
                "experiment modify", 1,
                stderr="experiment_id is required. Run 'jrun init -e <id>' first.",
            )
        if not jobs:
            raise PlatformError(
                "experiment modify", 1,
                stderr="no jobs to put in the experiment config set",
            )

        names = []
        for job in jobs:
            name = getattr(job, "name", None)
            if not isinstance(name, str) or not name.strip():
                raise PlatformError(
                    "experiment modify", 1,
                    stderr="every job must have a non-empty name",
                )
            names.append(name)
        if len(names) != len(set(names)):
            raise PlatformError(
                "experiment modify", 1,
                stderr="duplicate job names cannot be represented as configs: "
                       + ", ".join(names),
            )

        if experiment_data is None:
            experiment_data = self.get_experiment(exp_id, check=True)
        if not experiment_data:
            raise PlatformError(
                "experiment list", 0,
                stderr=f"empty response for experiment {exp_id}",
            )

        document = self._experiment_document(experiment_data)
        advance_configs = self._experiment_configs(document)
        if not advance_configs:
            raise PlatformError(
                "experiment list", 0,
                stderr="experiment has no existing config to use as template",
            )

        template = advance_configs[0]
        if not isinstance(template, dict):
            raise PlatformError(
                "experiment list", 0,
                stderr="experiment config template is not an object",
            )
        locations = queue_locations or {}
        new_configs = []
        for job in jobs:
            effective = effective_spec(job, spec)
            location = (
                locations.get(effective.queue_name)
                if effective and effective.queue_name
                else None
            )
            new_configs.append(
                self._build_job_config(job, template, effective, location)
            )

        config_data = json.loads(json.dumps(document))
        config_key = (
            "advance_config_infos"
            if "advance_config_infos" in config_data
            else "advanceConfigInfos"
        )
        # Avoid sending two differently-cased copies if an API gateway merged
        # fields from more than one serialization layer.
        alternate_config_key = (
            "advanceConfigInfos"
            if config_key == "advance_config_infos"
            else "advance_config_infos"
        )
        config_data.pop(alternate_config_key, None)
        config_data[config_key] = new_configs

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", prefix="jrun_", delete=False
        ) as f:
            json.dump(config_data, f, indent=2)
            tmp_path = f.name

        try:
            result = self.experiment_modify(tmp_path)
            returncode = getattr(result, "returncode", 0)
            if isinstance(returncode, int) and returncode != 0:
                raise PlatformError(
                    "experiment modify", returncode,
                    stderr=getattr(result, "stderr", "") or "modify failed",
                    stdout=getattr(result, "stdout", "") or "",
                )
        finally:
            Path(tmp_path).unlink(missing_ok=True)
        return names

    @classmethod
    def _build_job_config(cls, job: Job, template: dict,
                          spec: "ExperimentSpec | None",
                          queue_location: dict | None) -> dict:
        """Clone a platform config template and apply one job's values."""
        new_config = json.loads(json.dumps(template))
        document_camel = cls._prefers_camel(new_config)
        config_name_key = cls._field_key(
            new_config, "config_name", "configName", document_camel
        )
        new_config[config_name_key] = job.name
        new_config["command"] = job.command
        for key in ("conf_id", "confId", "config_id", "configId"):
            new_config.pop(key, None)

        rc = job.resource_config.to_dict()
        resource_key = cls._field_key(
            new_config, "resource_config_list", "resourceConfigList",
            document_camel,
        )
        if new_config.get(resource_key):
            res = new_config[resource_key][0]
            resource_camel = cls._prefers_camel(res) or document_camel
            if spec:
                cls._apply_spec_overrides(
                    res, spec, queue_location or {}, resource_camel
                )
            if rc.get("priority"):
                res["priority"] = rc["priority"]
            role_key = cls._field_key(
                res, "role_info_list", "roleInfoList", resource_camel
            )
            role_list = res.get(role_key, [])
            master = role_list[0] if role_list else {}
            detail_camel = cls._prefers_camel(master) or resource_camel
            detail_key = cls._field_key(
                master, "resource_request_detail", "resourceRequestDetail",
                detail_camel,
            )
            detail = master.get(detail_key)
            if not isinstance(detail, dict):
                detail = {}
                if master:
                    master[detail_key] = detail
            detail_fields = {
                "accelerator_model": "acceleratorModel",
                "accelerator_count": "acceleratorCount",
                "cpu_cores": "cpuCores",
                "mem_gib": "memGib",
                "shared_mem_gib": "sharedMemGib",
            }
            # A queue lookup supplies the accelerator model for templates that
            # do not specify one explicitly.  It belongs in the master
            # resource-request detail (not on the resource-config wrapper).
            if (
                queue_location
                and queue_location.get("accelerator_model")
                and "accelerator_model" not in rc
            ):
                cls._set_shaped_field(
                    detail,
                    "accelerator_model",
                    "acceleratorModel",
                    queue_location["accelerator_model"],
                    detail_camel,
                )
            for snake, camel in detail_fields.items():
                if snake in rc:
                    cls._set_shaped_field(
                        detail, snake, camel, rc[snake], detail_camel
                    )

            worker_cfg = rc.get("worker")
            if worker_cfg:
                worker_replicas = worker_cfg.get("replicas", 1)
                worker_detail = dict(detail)
                for snake, camel in detail_fields.items():
                    if snake in worker_cfg:
                        cls._set_shaped_field(
                            worker_detail, snake, camel, worker_cfg[snake],
                            detail_camel,
                        )
                worker_role = {
                    "name": "Worker",
                    "replicas": worker_replicas,
                    detail_key: worker_detail,
                }
                region_key = cls._field_key(
                    master, "resource_region", "resourceRegion", detail_camel
                )
                if region_key in master:
                    worker_role[region_key] = master[region_key]
                role_list = [r for r in role_list if r.get("name") != "Worker"]
                role_list.append(worker_role)
                res[role_key] = role_list
            else:
                role_list = [r for r in role_list if r.get("name") != "Worker"]
                res[role_key] = role_list

            if master and detail_key not in master:
                master[detail_key] = detail

        env_key = cls._field_key(
            new_config, "hyper_parameter", "hyperParameter", document_camel
        )
        if job.envs:
            env_values = {str(k): str(v) for k, v in job.envs.items()}
            new_config[env_key] = env_values
            # Some experiment documents carry both a map and a present-list
            # representation. Keep them in sync when the template supplies
            # the latter; do not invent an extra field for sparse templates.
            present_key = cls._field_key(
                new_config, "hyper_parameter_present", "hyperParameterPresent",
                document_camel,
            )
            if (
                "hyper_parameter_present" in new_config
                or "hyperParameterPresent" in new_config
            ):
                new_config[present_key] = [
                    {"key": key, "value": value}
                    for key, value in env_values.items()
                ]
        else:
            new_config.pop("hyper_parameter", None)
            new_config.pop("hyperParameter", None)
            new_config.pop("hyper_parameter_present", None)
            new_config.pop("hyperParameterPresent", None)
        return new_config

    def run_config(self, exp_id: str | None, config_name: str) -> str:
        """Run an already-modified config without touching experiment data."""
        if not exp_id:
            raise PlatformError(
                "job run", 1,
                stderr="experiment_id is required. Run 'jrun init -e <id>' first.",
            )
        if not isinstance(config_name, str) or not config_name.strip():
            raise PlatformError(
                "job run", 1,
                stderr="config_name is required to run a single config",
            )
        result = self.job_run(exp_id=exp_id, config_name=config_name)
        returncode = getattr(result, "returncode", 0)
        if isinstance(returncode, int) and returncode != 0:
            raise PlatformError(
                "job run", returncode,
                stderr=getattr(result, "stderr", "") or "job run failed",
                stdout=getattr(result, "stdout", "") or "",
            )
        return self._parse_job_id(getattr(result, "stdout", ""), config_name)

    @staticmethod
    def _parse_job_id(output: str | None, fallback: str) -> str:
        """Extract a job ID from the several formats emitted by airsctl."""
        text = output if isinstance(output, str) else ""
        if text:
            try:
                data = json.loads(text)

                def find_id(value):
                    if isinstance(value, dict):
                        for key in ("job_id", "jobId", "id"):
                            candidate = value.get(key)
                            if isinstance(candidate, str) and candidate:
                                return candidate
                        for nested in value.values():
                            candidate = find_id(nested)
                            if candidate:
                                return candidate
                    elif isinstance(value, list):
                        for nested in value:
                            candidate = find_id(nested)
                            if candidate:
                                return candidate
                    return None

                candidate = find_id(data)
                if candidate:
                    return candidate
            except (json.JSONDecodeError, TypeError):
                pass

        # Normal text output is typically "Job <uuid> submitted".  Accept a
        # labelled non-UUID ID as well for test doubles and older airsctl
        # versions, while retaining the config name fallback for unlabelled
        # output (the historical submit_job contract).
        labelled = re.search(
            r"\bjob(?:[_ ]+id)?\s*[:=]\s*([A-Za-z0-9._-]+)",
            text, re.IGNORECASE,
        )
        if labelled:
            return labelled.group(1)
        match = re.search(r"\b[Jj]ob\b.*?([0-9a-fA-F-]{36})", text)
        return match.group(1) if match else fallback

    def submit_job(self, job: Job, exp_name: str | None, exp_id: str | None,
                   spec: "ExperimentSpec | None" = None,
                   queue_location: dict | None = None,
                   experiment_data: dict | list | None = None) -> str:
        """Backward-compatible one-job wrapper around batch modify + run."""
        effective = effective_spec(job, spec)
        locations = {}
        if queue_location and effective and effective.queue_name:
            locations[effective.queue_name] = queue_location
        self.modify_configs(
            [job], exp_id, spec=spec, queue_locations=locations,
            experiment_data=experiment_data,
        )
        return self.run_config(exp_id, job.name)

    def remove_config_from_experiment(self, exp_id: str, config_name: str):
        result = self.experiment_list(exp_id=exp_id, check=False)
        if result.returncode != 0 or not result.stdout:
            return
        try:
            config_data = json.loads(result.stdout)
        except json.JSONDecodeError:
            return

        advance_configs = config_data.get("advance_config_infos", [])
        filtered = [c for c in advance_configs if c.get("config_name") != config_name]
        if len(filtered) == len(advance_configs):
            return

        config_data["advance_config_infos"] = filtered
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", prefix="jrun_", delete=False
        ) as f:
            json.dump(config_data, f, indent=2)
            tmp_path = f.name
        try:
            self.experiment_modify(tmp_path)
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    def remove_configs(self, exp_id: str, config_names: set[str]):
        return self.remove_configs_from_experiment(exp_id, config_names)

    def remove_configs_from_experiment(self, exp_id: str, config_names: set[str]):
        result = self.experiment_list(exp_id=exp_id, check=False)
        if result.returncode != 0 or not result.stdout:
            return
        try:
            config_data = json.loads(result.stdout)
        except json.JSONDecodeError:
            return

        advance_configs = config_data.get("advance_config_infos", [])
        filtered = [c for c in advance_configs if c.get("config_name") not in config_names]
        if len(filtered) == len(advance_configs):
            return

        config_data["advance_config_infos"] = filtered
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", prefix="jrun_", delete=False
        ) as f:
            json.dump(config_data, f, indent=2)
            tmp_path = f.name
        try:
            self.experiment_modify(tmp_path)
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    @classmethod
    def _apply_spec_overrides(cls, res: dict, spec: ExperimentSpec,
                              queue_location: dict,
                              prefer_camel: bool | None = None):
        """Apply per-job overrides to a cloned resource config entry.

        image/queue come from the job's effective spec (job-level fields win
        over the deprecated experiment-block fields). queue_location comes from
        api.queue_location_info() (queue/select row); explicit cluster_id/
        zone_id in the spec win over the queue lookup.
        """
        if prefer_camel is None:
            prefer_camel = cls._prefers_camel(res)
        if spec.image:
            cls._set_shaped_field(
                res, "basic_image", "basicImage", spec.image, prefer_camel
            )
        # An explicitly selected repository region is meaningful even when
        # the image is inherited from the experiment template.  Otherwise a
        # sparse job spec should leave the template's region untouched.
        if spec.image and spec.image_region == ExperimentSpec.DEFAULT_IMAGE_REGION:
            set_image_region = True
        else:
            set_image_region = spec.image_region != ExperimentSpec.DEFAULT_IMAGE_REGION
        if set_image_region:
            # airsctl parses the modify JSON with plain encoding/json, so the
            # RepoTypes enum must be the integer value, not the name
            cls._set_shaped_field(
                res, "image_region", "imageRegion", spec.image_region_value,
                prefer_camel,
            )
        if spec.queue_name:
            # The queue lookup normally supplies the same field together with
            # IDs.  Keep the explicit name even when a caller has no lookup
            # map (useful for an already-scoped/template experiment).
            cls._set_shaped_field(
                res, "queue_name", "queueName", spec.queue_name,
                prefer_camel,
            )
        field_map = {
            "queue_id": ("queue_id", "queueId"),
            "queue_name": ("queue_name", "queueName"),
            "quota_id": ("quota_id", "quotaId"),
            "cluster_id": ("cluster_id", "clusterId"),
            "zone_id": ("zone_id", "zoneId"),
            "cluster_display_name": (
                "cluster_display_name", "clusterDisplayName"
            ),
            "zone_display_name": ("zone_display_name", "zoneDisplayName"),
        }
        for src, (snake, camel) in field_map.items():
            if queue_location.get(src):
                cls._set_shaped_field(
                    res, snake, camel, queue_location[src], prefer_camel
                )
        if spec.cluster_id:
            cls._set_shaped_field(
                res, "cluster_id", "clusterId", spec.cluster_id,
                prefer_camel,
            )
        if spec.zone_id:
            cls._set_shaped_field(
                res, "zone_id", "zoneId", spec.zone_id, prefer_camel
            )

    @staticmethod
    def _parse_job_status(output: str) -> str | None:
        try:
            data = json.loads(output)
            if isinstance(data, dict) and "status" in data:
                return data["status"].capitalize()
        except (json.JSONDecodeError, ValueError, AttributeError):
            pass
        for line in output.splitlines():
            lower = line.lower().strip()
            for s in ("cancelled", "failed", "stopped", "succeed", "completed",
                      "running", "starting", "scheduling", "queued", "submitted"):
                if s in lower:
                    return s.capitalize()
        return None
