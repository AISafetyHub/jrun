import json
import re
import subprocess
import tempfile
import time
from pathlib import Path

from jrun.errors import PlatformError
from jrun.models import Job


class PlatformClient:
    def __init__(self):
        self._cmd = "airsctl"

    _UNAUTHENTICATED_PATTERN = "Unauthenticated"
    _RETRY_DELAY = 35

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
        return self._run(["experiment", "modify", "-f", config_path])

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
        if exp_name:
            args += ["-N", exp_name]
        elif exp_id:
            args += ["-e", exp_id]
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
        result = self.job_list(job_id, check=False)
        if result.returncode != 0 or not result.stdout:
            return None
        return self._parse_job_status(result.stdout)

    def submit_job(self, job: Job, exp_name: str | None, exp_id: str | None) -> str:
        if not exp_id:
            raise PlatformError("experiment list", 1, stderr="experiment_id is required. Run 'jrun init -e <id>' first.")

        result = self.experiment_list(exp_id=exp_id)
        output = result.stdout
        if not output:
            raise PlatformError("experiment list", 0, stderr=f"empty response for experiment {exp_id}")

        try:
            config_data = json.loads(output)
        except json.JSONDecodeError as e:
            raise PlatformError("experiment list", 0, stderr=f"invalid JSON: {e}", stdout=output[:200])

        advance_configs = config_data.get("advance_config_infos", [])
        if not advance_configs:
            raise PlatformError("experiment list", 0, stderr="experiment has no existing config to use as template")

        new_config = json.loads(json.dumps(advance_configs[0]))
        new_config["config_name"] = job.name
        new_config["command"] = job.command
        new_config.pop("conf_id", None)

        rc = job.resource_config.to_dict()
        if rc and new_config.get("resource_config_list"):
            res = new_config["resource_config_list"][0]
            if rc.get("priority"):
                res["priority"] = rc["priority"]
            role_list = res.get("role_info_list", [])
            master = role_list[0] if role_list else {}
            detail = master.get("resource_request_detail", {})
            for key in ("accelerator_model", "accelerator_count", "cpu_cores", "mem_gib", "shared_mem_gib"):
                if key in rc:
                    detail[key] = rc[key]

            worker_cfg = rc.get("worker")
            if worker_cfg:
                worker_replicas = worker_cfg.get("replicas", 1)
                worker_detail = dict(detail)
                for key in ("accelerator_model", "accelerator_count", "cpu_cores", "mem_gib", "shared_mem_gib"):
                    if key in worker_cfg:
                        worker_detail[key] = worker_cfg[key]
                worker_role = {
                    "name": "Worker",
                    "replicas": worker_replicas,
                    "resource_request_detail": worker_detail,
                }
                if "resource_region" in master:
                    worker_role["resource_region"] = master["resource_region"]
                role_list = [r for r in role_list if r.get("name") != "Worker"]
                role_list.append(worker_role)
                res["role_info_list"] = role_list
            else:
                role_list = [r for r in role_list if r.get("name") != "Worker"]
                res["role_info_list"] = role_list

        if job.envs:
            new_config["hyper_parameter"] = {str(k): str(v) for k, v in job.envs.items()}
        else:
            new_config.pop("hyper_parameter", None)

        advance_configs = [c for c in advance_configs if c.get("config_name") != job.name]
        advance_configs.append(new_config)
        config_data["advance_config_infos"] = advance_configs

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", prefix="jrun_", delete=False
        ) as f:
            json.dump(config_data, f, indent=2)
            tmp_path = f.name

        try:
            self.experiment_modify(tmp_path)
            result = self.job_run(exp_name=exp_name, exp_id=exp_id, config_name=job.name)

            output = result.stdout
            match = re.search(r"[Jj]ob.*?([0-9a-f-]{36})", output)
            if match:
                return match.group(1)
            return job.name
        finally:
            Path(tmp_path).unlink(missing_ok=True)

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
