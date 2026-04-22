import subprocess
import json


def _run(args: list[str], capture: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["airsctl"] + args,
        capture_output=capture,
        text=True,
    )


def experiment_list(exp_name: str | None = None, exp_id: str | None = None) -> str:
    args = ["experiment", "list"]
    if exp_id:
        args += ["-e", exp_id]
    elif exp_name:
        args += ["-N", exp_name]
    return _run(args).stdout


def experiment_modify(config_path: str) -> subprocess.CompletedProcess:
    return _run(["experiment", "modify", "-f", config_path])


def job_list(job_id: str | None = None) -> str:
    args = ["job", "list"]
    if job_id:
        args += ["-j", job_id]
    return _run(args).stdout


def job_run(
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
    return _run(args)


def job_stop(job_id: str) -> subprocess.CompletedProcess:
    return _run(["job", "stop", "-j", job_id])


def job_cancel(job_id: str) -> subprocess.CompletedProcess:
    return _run(["job", "cancel", "-j", job_id])
