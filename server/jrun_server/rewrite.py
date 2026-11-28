"""Rewrite a caller's config before the service submits it.

The service submits as its own account, so a caller must not be able to
reach that account's private assets or settings. The config is therefore
rebuilt from an allowlist (unknown keys are rejected, because jrun forwards
unknown experiment/resource keys to the platform as raw overrides), and:

- the experiment is named ``<caller alias>-<name>`` (callers never share,
  and so never overwrite, each other's experiments);
- ``mounts.home`` is forced off (it would mount the service account's home);
- queue categories are narrowed to the ones the service submits;
- ``image_region`` must be PUBLIC (no access to the account's private images);
- ``remote:`` is dropped (the service submits locally).

``dump_config`` escapes every ``$`` as ``$$``: the caller's config arrives
already variable-substituted, so jrun on the service must not substitute
again (that would expand the service's own environment, e.g. ``$AIRS_SK``).
"""

import copy
import re

import yaml

TOP_LEVEL_KEYS = {"experiment", "job", "search"}
EXPERIMENT_KEYS = {
    "name", "image", "image_region", "description", "queue_name", "queue_category",
    "project_set_name", "project_name", "region_name", "zone_name", "train_frame",
    "restart_policy", "mounts", "native_cluster", "profiler_info",
}
SEARCH_KEYS = {"name", "job_template", "params", "sampling", "max_trials",
               "parallel_trials", "poll_interval"}
JOB_KEYS = {"name", "command", "commands", "envs", "image", "image_region",
            "resource_config", "run_options", "train_frame", "restart_policy",
            "selected_nodes"}
RESOURCE_KEYS = {
    "queue_name", "queue_category", "project_set_name", "project_name", "region_name",
    "zone_name", "priority", "accelerator_model", "accelerator_count", "cpu_cores",
    "mem_gib", "shared_mem_gib", "resource_region", "pod_restart_policy", "restart_scope",
    "rdma_shared_count", "accelerator_resource_name", "fb_mem_gib", "accelerator_mem_gib",
    "cpu_model", "selected_nodes", "roles",
}
ROLE_KEYS = {
    "name", "replicas", "accelerator_model", "accelerator_count", "cpu_cores", "mem_gib",
    "shared_mem_gib", "resource_region", "rdma_shared_count", "accelerator_resource_name",
    "fb_mem_gib", "accelerator_mem_gib", "cpu_model", "extra_label_info",
}
MOUNT_KEYS = {"home", "project", "projset"}

_CATEGORY_ALIASES = {
    "dedicated": "dedicated", "queue_category_dedicated": "dedicated",
    "public": "public", "queue_category_public": "public",
    "idle": "idle", "queue_category_idle": "idle",
}
_ALIAS_RE = re.compile(r"[^A-Za-z0-9_-]+")


class RewriteError(ValueError):
    """The config uses something the service does not allow."""


def _check_keys(where: str, data, allowed: set):
    if not isinstance(data, dict):
        raise RewriteError(f"{where} must be a mapping")
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise RewriteError(f"{where}: field(s) not allowed through the remote service: "
                           f"{', '.join(unknown)}")


def _category(value) -> str:
    name = _CATEGORY_ALIASES.get(str(value).strip().lower())
    if name is None:
        raise RewriteError(f"unknown queue_category '{value}'")
    return name


def _narrow(where: str, value, allowed: list[str]):
    """Keep only allowed categories (order kept); a scalar must be allowed."""
    if isinstance(value, list):
        kept = [c for c in value if _category(c) in allowed]
        if not kept:
            raise RewriteError(f"{where}: none of {value} is submitted by this service "
                               f"(allowed: {', '.join(allowed)})")
        return kept if len(kept) > 1 else kept[0]
    if _category(value) not in allowed:
        raise RewriteError(f"{where}: '{value}' is not submitted by this service "
                           f"(allowed: {', '.join(allowed)})")
    return value


def _check_image_region(where: str, data: dict):
    region = data.get("image_region")
    if region is not None and str(region).strip().upper() != "PUBLIC":
        raise RewriteError(f"{where}.image_region must be PUBLIC through the remote service")


def _rewrite_job(where: str, job: dict, allowed: list[str]) -> bool:
    """Validate one job (or job_template); returns whether it sets a category."""
    _check_keys(where, job, JOB_KEYS)
    _check_image_region(where, job)
    rc = job.get("resource_config")
    if rc is None:
        return False
    _check_keys(f"{where}.resource_config", rc, RESOURCE_KEYS)
    for i, role in enumerate(rc.get("roles") or []):
        _check_keys(f"{where}.resource_config.roles[{i}]", role, ROLE_KEYS)
    if rc.get("queue_category") is None:
        return False
    rc["queue_category"] = _narrow(f"{where}.resource_config.queue_category",
                                   rc["queue_category"], allowed)
    return True


def safe_alias(alias: str) -> str:
    cleaned = _ALIAS_RE.sub("-", alias or "").strip("-")
    if not cleaned:
        raise RewriteError("caller has no usable alias for the experiment name prefix")
    return cleaned


def rewrite_config(config: dict, alias: str, allowed_categories: list[str]) -> dict:
    """Validate and rewrite a caller's config; returns a new dict."""
    if not isinstance(config, dict):
        raise RewriteError("config must be a mapping")
    cfg = copy.deepcopy(config)
    cfg.pop("remote", None)
    _check_keys("config", cfg, TOP_LEVEL_KEYS)

    experiment = cfg.get("experiment")
    if not isinstance(experiment, dict) or not experiment.get("name"):
        raise RewriteError("config needs an 'experiment:' block with a name")
    _check_keys("experiment", experiment, EXPERIMENT_KEYS)
    _check_image_region("experiment", experiment)
    prefix = safe_alias(alias) + "-"
    name = str(experiment["name"])
    experiment["name"] = name if name.startswith(prefix) else prefix + name
    mounts = experiment.get("mounts") or {}
    _check_keys("experiment.mounts", mounts, MOUNT_KEYS)
    experiment["mounts"] = {**mounts, "home": False}

    if cfg.get("search"):
        _check_keys("search", cfg["search"], SEARCH_KEYS)
        sets_category = _rewrite_job("search.job_template",
                                     cfg["search"].get("job_template") or {},
                                     allowed_categories)
    elif cfg.get("job"):
        sets_category = _rewrite_job("job", cfg["job"], allowed_categories)
    else:
        raise RewriteError("config has neither 'search' nor 'job'")

    exp_category = experiment.get("queue_category")
    if exp_category is not None:
        experiment["queue_category"] = _narrow("experiment.queue_category", exp_category,
                                               allowed_categories)
    elif not sets_category and "dedicated" not in allowed_categories:
        raise RewriteError("no queue_category set (dedicated) and this service only submits "
                           f"{', '.join(allowed_categories)}")
    return cfg


def dump_config(cfg: dict) -> str:
    """YAML text for jrun, with ``$`` escaped so nothing is substituted again."""
    text = yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False, width=10 ** 6)
    return text.replace("$", "$$")
