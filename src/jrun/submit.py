import json
import re
import tempfile
from pathlib import Path

from jrun import airsctl


def submit_one_job(job: dict, exp_name: str | None, exp_id: str | None, log_fn=None) -> str | None:
    """Submit a single job to the platform. Returns job_id or None.

    log_fn: callable(msg, err=False) for output. Defaults to print.
    """
    if log_fn is None:
        log_fn = lambda msg, err=False: print(msg)

    if not exp_id:
        log_fn("  Error: experiment_id is required for submission. Run 'jrun init -e <id>' first.", err=True)
        return None

    output = airsctl.experiment_list(exp_id=exp_id)
    if not output:
        log_fn("  Error: could not fetch experiment config", err=True)
        return None

    try:
        config_data = json.loads(output)
    except json.JSONDecodeError:
        log_fn("  Error: invalid experiment config JSON", err=True)
        return None

    advance_configs = config_data.get("advance_config_infos", [])
    if not advance_configs:
        log_fn("  Error: experiment has no existing config to use as template", err=True)
        return None

    new_config = json.loads(json.dumps(advance_configs[0]))
    new_config["config_name"] = job["name"]
    new_config["command"] = job["command"]
    new_config.pop("conf_id", None)

    rc = job.get("resource_config", {})
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

    envs = job.get("envs", {})
    if envs:
        new_config["hyper_parameter"] = {str(k): str(v) for k, v in envs.items()}
    else:
        new_config.pop("hyper_parameter", None)

    advance_configs = [c for c in advance_configs if c.get("config_name") != job["name"]]
    advance_configs.append(new_config)
    config_data["advance_config_infos"] = advance_configs

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", prefix="jrun_", delete=False
    ) as f:
        json.dump(config_data, f, indent=2)
        tmp_path = f.name

    try:
        result = airsctl.experiment_modify(tmp_path)
        if result.returncode != 0:
            log_fn(f"  Warning: experiment modify failed (rc={result.returncode})", err=True)
            if result.stdout:
                log_fn(result.stdout.strip(), err=True)
            if result.stderr:
                log_fn(result.stderr.strip(), err=True)
            return None

        result = airsctl.job_run(
            exp_name=exp_name, exp_id=exp_id, config_name=job["name"]
        )
        if result.returncode != 0:
            log_fn(f"  Warning: job run failed (rc={result.returncode})", err=True)
            if result.stdout:
                log_fn(result.stdout.strip(), err=True)
            if result.stderr:
                log_fn(result.stderr.strip(), err=True)
            return None

        output = result.stdout
        match = re.search(r"[Jj]ob.*?([0-9a-f-]{36})", output)
        if match:
            return match.group(1)
        else:
            log_fn(f"  Warning: could not parse job ID from output", err=True)
            return job["name"]
    finally:
        Path(tmp_path).unlink(missing_ok=True)
