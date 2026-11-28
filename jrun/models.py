"""Core data model: config dataclasses, queue categories, and job statuses."""

from dataclasses import dataclass, field, fields

from jrun.jiuding import enums as platform_enums
from jrun.jiuding.enums import (DEFAULT_DIRECTORY_ORDER, AccessMode,
                                DirectoryType)


# --- job statuses ---------------------------------------------------------

TERMINAL_STATUSES = frozenset({"Completed", "Succeed", "Failed", "Stopped", "Cancelled"})
# "Approving" is a public-queue job waiting for approval; "Pending" is a
# platform job waiting to be scheduled. Local scheduler-held jobs are
# "Queued" with a queued-* ID.
ACTIVE_STATUSES = frozenset({"Submitted", "Approving", "Running", "Pending", "Queued",
                             "Starting", "Scheduling"})
# Statuses in which a job can be stopped on the platform (Queued includes the
# local queued-* entries, which are only marked Stopped).
STOPPABLE_STATUSES = ACTIVE_STATUSES


# --- queue categories -----------------------------------------------------

QUEUE_CATEGORY_ALIASES = {
    "dedicated": "QUEUE_CATEGORY_DEDICATED",
    "quota": "QUEUE_CATEGORY_DEDICATED",
    "private": "QUEUE_CATEGORY_DEDICATED",
    "public": "QUEUE_CATEGORY_PUBLIC",
    "idle": "QUEUE_CATEGORY_IDLE",
}

QUEUE_CATEGORY_VALUES = platform_enums.QUEUE_CATEGORY_VALUES

DEDICATED = platform_enums.QUEUE_CATEGORY_DEDICATED
PUBLIC = platform_enums.QUEUE_CATEGORY_PUBLIC
IDLE = platform_enums.QUEUE_CATEGORY_IDLE
SHARED_CATEGORIES = frozenset({PUBLIC, IDLE})
# Shared queue categories accept exactly one resource_config.priority; the
# platform rejects anything else (QueueCategoryPriorityMismatch). Dedicated
# queues accept any priority.
CATEGORY_PRIORITY = {PUBLIC: "high", IDLE: "low"}

# resource_config.queue_name values with special meaning.
LOCAL_QUEUE = "local"
AUTO_QUEUE = "auto"


def normalize_queue_category(value: str | None) -> str:
    """Return the platform queue-category enum for a user-facing alias."""
    if value is None:
        return DEDICATED
    key = str(value).strip()
    if key in QUEUE_CATEGORY_VALUES:
        return key
    normalized = QUEUE_CATEGORY_ALIASES.get(key.lower())
    if not normalized:
        raise ValueError(
            f"unknown queue_category '{value}'; expected dedicated, public, or idle"
        )
    return normalized


def normalize_queue_categories(value) -> tuple[str, ...]:
    """Normalize a category or a preference-ordered list of categories.

    None means dedicated. Duplicates are dropped, keeping the first position.
    """
    if isinstance(value, (list, tuple)):
        if not value:
            raise ValueError("queue_category list must not be empty")
        items = [normalize_queue_category(v) for v in value]
        return tuple(dict.fromkeys(items))
    return (normalize_queue_category(value),)


def queue_category_value(value: str | None) -> int:
    """Return the protobuf enum number used by airsctl modify JSON."""
    return QUEUE_CATEGORY_VALUES[normalize_queue_category(value)]


def queue_category_alias(value: str | None) -> str:
    """Short user-facing name (dedicated/public/idle) for a category."""
    return normalize_queue_category(value).replace("QUEUE_CATEGORY_", "").lower()


# --- image regions --------------------------------------------------------

DEFAULT_IMAGE_REGION = "PUBLIC"
# airs.v1.common.RepoTypes: the REST API accepts enum names, but the airsctl
# experiment-modify JSON file needs the integer value.
IMAGE_REGION_VALUES = platform_enums.IMAGE_REGION_VALUES


def normalize_image_region(value: str | None) -> str | None:
    if value is None:
        return None
    region = str(value).upper()
    if region not in IMAGE_REGION_VALUES:
        raise ValueError(
            f"unknown image_region '{value}'; expected one of {sorted(IMAGE_REGION_VALUES)}"
        )
    return region


# --- storage mounts -------------------------------------------------------

# experiment.mounts keys -> storage_info directory types.
MOUNT_DIRECTORIES = {
    "home": DirectoryType.HOME,        # /home/<user>
    "project": DirectoryType.PROJECT,  # /share/project
    "projset": DirectoryType.PROJSET,  # /share/projset
}
MOUNT_ACCESS = {"rw": AccessMode.READ_WRITE, "ro": AccessMode.READ_ONLY}


def normalize_mounts(value) -> dict | None:
    """Validate ``experiment.mounts``: ``{home|project|projset: rw|ro|false}``.

    Returns ``{name: "rw" | "ro" | False}``; None when unset.
    """
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("mounts must be a mapping like {home: rw, project: false, projset: ro}")
    result = {}
    for name, mode in value.items():
        if name not in MOUNT_DIRECTORIES:
            raise ValueError(
                f"unknown mount '{name}'; expected {', '.join(MOUNT_DIRECTORIES)}")
        if mode is True:
            mode = "rw"
        elif mode is False or mode is None or str(mode).lower() in ("false", "no", "off"):
            mode = False
        else:
            mode = str(mode).lower()
            if mode not in MOUNT_ACCESS:
                raise ValueError(f"mount '{name}': expected rw, ro, or false, got '{value[name]}'")
        result[name] = mode
    return result


def mount_entries(mounts: dict | None) -> list[tuple[int, int]]:
    """``(directory_type, access_mode)`` per mounted directory, platform order.

    A directory missing from ``mounts`` (or ``mounts`` None) is mounted rw;
    ``False`` leaves it out.
    """
    by_type = {t: name for name, t in MOUNT_DIRECTORIES.items()}
    entries = []
    for directory_type in DEFAULT_DIRECTORY_ORDER:
        mode = (mounts or {}).get(by_type[directory_type], "rw")
        if mode is False:
            continue
        entries.append((directory_type, MOUNT_ACCESS[mode]))
    return entries


def mount_entries_rest(mounts: dict | None) -> list[dict]:
    """``storageInfo`` for the REST experiment-create body (camelCase)."""
    return [{"directoryType": t, "accessMode": m} for t, m in mount_entries(mounts)]


# --- (de)serialization helpers --------------------------------------------

def _from_dict(cls, d: dict | None, **special):
    """Build a dataclass from a dict.

    Keys matching a field are passed through; ``special`` supplies already
    converted values; unknown keys are collected into ``platform_overrides``
    (an escape hatch for platform fields newer than jrun).
    """
    d = d or {}
    names = {f.name for f in fields(cls)}
    kwargs = {k: d[k] for k in names
              if k in d and k not in special and k != "platform_overrides"}
    kwargs.update(special)
    if "platform_overrides" in names:
        overrides = dict(d.get("platform_overrides") or {})
        overrides.update({k: v for k, v in d.items() if k not in names})
        kwargs["platform_overrides"] = overrides
    return cls(**kwargs)


def _to_dict(obj, always: tuple = (), skip: tuple = ()) -> dict:
    """Serialize a dataclass, omitting None/empty values (except ``always``)."""
    d = dict(getattr(obj, "platform_overrides", None) or {})
    for f in fields(obj):
        if f.name == "platform_overrides" or f.name in skip:
            continue
        value = getattr(obj, f.name)
        if f.name in always or (value is not None and value != {}):
            d[f.name] = value
    return d


def _reject(d: dict, keys: tuple, message: str):
    found = sorted(k for k in keys if k in (d or {}))
    if found:
        raise ValueError(f"{', '.join(found)}: {message}")


# Accelerator/CPU/memory request fields shared by resource_config and roles.
DETAIL_FIELDS = (
    "accelerator_model", "accelerator_count", "cpu_cores", "mem_gib",
    "shared_mem_gib", "rdma_shared_count", "accelerator_resource_name",
    "fb_mem_gib", "accelerator_mem_gib", "cpu_model",
)


# --- config dataclasses ---------------------------------------------------

@dataclass
class RunOptions:
    """Options passed to ``airsctl job run`` for shared queues."""

    duration_hours: float | None = None
    enable_checkpoint: bool | None = None
    queue_priority_mode: str | None = None
    usage_desc: str | None = None

    @classmethod
    def from_dict(cls, d: dict | None) -> "RunOptions":
        d = d or {}
        unknown = sorted(set(d) - {f.name for f in fields(cls)})
        if unknown:
            raise ValueError(f"unknown run_options field(s): {', '.join(unknown)}")
        mode = d.get("queue_priority_mode")
        if mode is not None:
            mode = str(mode).lower()
            if mode not in {"normal", "high"}:
                raise ValueError("queue_priority_mode must be normal or high")
        duration = d.get("duration_hours")
        if duration is not None:
            duration = float(duration)
            if duration <= 0:
                raise ValueError("duration_hours must be greater than zero")
        return cls(
            duration_hours=duration,
            enable_checkpoint=d.get("enable_checkpoint"),
            queue_priority_mode=mode,
            usage_desc=d.get("usage_desc"),
        )

    def to_dict(self) -> dict:
        return _to_dict(self)

    def validate_for_category(self, category: str | None):
        if normalize_queue_category(category) != "QUEUE_CATEGORY_PUBLIC":
            return
        missing = [
            name for name in ("duration_hours", "usage_desc", "queue_priority_mode")
            if not getattr(self, name)
        ]
        if missing:
            raise ValueError("public queue requires run_options: " + ", ".join(missing))


@dataclass
class RoleConfig:
    """A platform node role (Master, Worker, or a custom role).

    Unset resource fields inherit from the enclosing resource_config.
    """

    name: str = "Master"
    replicas: int = 1
    accelerator_model: str | None = None
    accelerator_count: int | None = None
    cpu_cores: int | None = None
    mem_gib: int | None = None
    shared_mem_gib: int | None = None
    resource_region: str | int | None = None
    rdma_shared_count: int | None = None
    accelerator_resource_name: str | None = None
    fb_mem_gib: int | str | None = None
    accelerator_mem_gib: int | None = None
    cpu_model: str | None = None
    extra_label_info: dict = field(default_factory=dict)
    platform_overrides: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict) -> "RoleConfig":
        return _from_dict(cls, d, extra_label_info=(d or {}).get("extra_label_info") or {})

    def to_dict(self) -> dict:
        return _to_dict(self, always=("name", "replicas"))


@dataclass
class ResourceConfig:
    queue_name: str | None = None
    # A category, or a preference-ordered list of categories with queue_name: auto.
    queue_category: str | list[str] | None = None
    project_set_name: str | None = None
    project_name: str | None = None
    region_name: str | None = None
    zone_name: str | None = None
    priority: str | None = None
    # With queue_name: auto these may be lists, paired by position
    # (see accelerator_choices).
    accelerator_model: str | list[str] | None = None
    accelerator_count: int | list[int] | None = None
    cpu_cores: int | None = None
    mem_gib: int | None = None
    shared_mem_gib: int | None = None
    resource_region: str | int | None = None
    pod_restart_policy: str | None = None
    restart_scope: str | None = None
    rdma_shared_count: int | None = None
    accelerator_resource_name: str | None = None
    fb_mem_gib: int | str | None = None
    accelerator_mem_gib: int | None = None
    cpu_model: str | None = None
    selected_nodes: list[str] | None = None
    roles: list[RoleConfig] | None = None
    platform_overrides: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict | None) -> "ResourceConfig":
        d = d or {}
        _reject(d, (
            "queue_id", "quota_id", "shared_quota_id", "cluster_id", "zone_id",
            "cluster_display_name", "zone_display_name", "cluster_name",
        ), "resource_config IDs/display names were removed; use project_set_name, "
           "project_name, region_name, and zone_name")
        _reject(d, ("worker",),
                "resource_config.worker was removed; declare nodes with "
                "roles: [{name: Master}, {name: Worker, replicas: N}]")
        if d.get("queue_category") is not None:
            normalize_queue_categories(d["queue_category"])
        roles = [RoleConfig.from_dict(item) for item in (d.get("roles") or [])]
        rc = _from_dict(cls, d, roles=roles or None)
        rc.accelerator_choices()  # validates list shapes early
        return rc

    def to_dict(self) -> dict:
        d = _to_dict(self, skip=("roles",))
        if self.roles:
            d["roles"] = [role.to_dict() for role in self.roles]
        return d

    @property
    def has_choice_lists(self) -> bool:
        return any(isinstance(v, (list, tuple)) for v in (
            self.accelerator_model, self.accelerator_count, self.queue_category))

    def accelerator_choices(self) -> list[tuple[str | None, int | None]]:
        """(model, count) pairs in preference order.

        Lists are paired by position; a scalar on either side is repeated
        for every entry of the other list.
        """
        models = self.accelerator_model
        counts = self.accelerator_count
        models = list(models) if isinstance(models, (list, tuple)) else [models]
        counts = list(counts) if isinstance(counts, (list, tuple)) else [counts]
        if not models or not counts:
            raise ValueError("accelerator_model/accelerator_count lists must not be empty")
        if len(models) == 1 and len(counts) > 1:
            models = models * len(counts)
        elif len(counts) == 1 and len(models) > 1:
            counts = counts * len(models)
        if len(models) != len(counts):
            raise ValueError(
                f"accelerator_model ({len(models)}) and accelerator_count "
                f"({len(counts)}) lists differ in length"
            )
        for count in counts:
            if count is not None and (not isinstance(count, int) or count < 0):
                raise ValueError(f"accelerator_count must be a non-negative integer, got {count!r}")
        return list(zip(models, counts))


@dataclass
class ExperimentSpec:
    """Top-level ``experiment:`` block.

    Only ``name`` is required. jrun looks the experiment up by name and creates
    it via the platform REST API if missing (using the first job's image and
    queue for the placeholder config).

    Image and resource-location fields are defaults for jobs that do not set
    their own; a job-level value always wins. The remaining fields are
    experiment-level settings applied to the experiment document on submit.
    """

    name: str
    image: str | None = None
    image_region: str | None = None
    description: str | None = None
    queue_name: str | None = None
    queue_category: str | list[str] | None = None
    project_set_name: str | None = None
    project_name: str | None = None
    region_name: str | None = None
    zone_name: str | None = None
    train_frame: str | None = None
    restart_policy: str | None = None
    mounts: dict | None = None
    native_cluster: str | None = None
    profiler_info: dict | None = None
    model_storage_info: dict | None = None
    platform_overrides: dict = field(default_factory=dict)

    # Fields written to the experiment document itself on modify.
    DOCUMENT_FIELDS = (
        "train_frame", "restart_policy", "native_cluster",
        "profiler_info", "model_storage_info",
    )

    @classmethod
    def from_dict(cls, d: dict) -> "ExperimentSpec":
        _reject(d, (
            "proj_id", "projset_id", "cluster_id", "zone_id",
            "cluster_display_name", "zone_display_name", "cluster_name",
        ), "experiment IDs/display names were removed; use project_set_name, "
           "project_name, region_name, and zone_name")
        _reject(d, ("storage_info",),
                "use mounts: {home: rw|ro|false, project: ..., projset: ...}; "
                "a raw list can go under platform_overrides")
        if not d.get("name"):
            raise ValueError("experiment section missing required field: name")
        if d.get("queue_category") is not None:
            normalize_queue_categories(d["queue_category"])
        return _from_dict(cls, d, image_region=normalize_image_region(d.get("image_region")),
                          mounts=normalize_mounts(d.get("mounts")))

    def to_dict(self) -> dict:
        return _to_dict(self, always=("name",))


# Job fields copied verbatim into the experiment's advance config.
JOB_CONFIG_FIELDS = (
    "train_frame", "restart_policy", "code", "code_path", "git_prefix",
    "git_branch", "git_commit", "gitee", "dataset", "model_storage_info",
    "model_storage_info_items", "selected_nodes",
)


@dataclass
class Job:
    name: str
    command: str = ""
    params: dict = field(default_factory=dict)
    resource_config: ResourceConfig = field(default_factory=ResourceConfig)
    envs: dict = field(default_factory=dict)
    image: str | None = None
    image_region: str | None = None
    run_options: RunOptions = field(default_factory=RunOptions)
    train_frame: str | None = None
    restart_policy: str | None = None
    code: str | None = None
    code_path: str | None = None
    git_prefix: str | None = None
    git_branch: str | None = None
    git_commit: str | None = None
    gitee: str | None = None
    dataset: dict | None = None
    model_storage_info: dict | None = None
    model_storage_info_items: list | None = None
    selected_nodes: list[str] | None = None
    platform_overrides: dict = field(default_factory=dict)

    @property
    def is_local(self) -> bool:
        return self.resource_config.queue_name == LOCAL_QUEUE

    def to_dict(self) -> dict:
        d = _to_dict(self, always=("name", "command", "params", "envs"),
                     skip=("resource_config", "run_options"))
        d["resource_config"] = self.resource_config.to_dict()
        run = self.run_options.to_dict()
        if run:
            d["run_options"] = run
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Job":
        if not d.get("name"):
            raise ValueError("job is missing required field: name")
        return _from_dict(
            cls, d,
            params=d.get("params") or {},
            envs=d.get("envs") or {},
            resource_config=ResourceConfig.from_dict(d.get("resource_config")),
            run_options=RunOptions.from_dict(d.get("run_options")),
            image_region=normalize_image_region(d.get("image_region")),
        )


# --- effective per-job target ---------------------------------------------

@dataclass(frozen=True)
class JobTarget:
    """Where and with what image a job runs, after applying experiment defaults.

    Job-level values win; ``experiment:`` values are the fallback.
    ``queue_name`` is None for local jobs and for jobs with no queue.
    ``queue_category`` is None when neither level sets it (dedicated).
    """

    image: str | None = None
    image_region: str | None = None
    queue_name: str | None = None
    # Normalized category, or a tuple of categories (preference order) for
    # an auto job whose queue_category is a list.
    queue_category: str | tuple | None = None
    project_set_name: str | None = None
    project_name: str | None = None
    region_name: str | None = None
    zone_name: str | None = None

    @property
    def is_auto(self) -> bool:
        return self.queue_name == AUTO_QUEUE

    @property
    def categories(self) -> tuple[str, ...]:
        """Normalized categories in preference order (dedicated when unset)."""
        return normalize_queue_categories(self.queue_category)

    @property
    def category(self) -> str:
        """Normalized queue category enum (dedicated when unset).

        For a category list this is the first (preferred) entry; concrete
        jobs always have a single category.
        """
        return self.categories[0]

    @property
    def location_key(self) -> str:
        """Stable key identifying the resolved queue location."""
        return "|".join((
            self.category, self.project_set_name or "", self.project_name or "",
            self.queue_name or "", self.region_name or "", self.zone_name or "",
        ))


def job_target(job: Job, spec: ExperimentSpec | None) -> JobTarget:
    rc = job.resource_config

    def pick(job_value, spec_attr):
        if job_value is not None:
            return job_value
        return getattr(spec, spec_attr) if spec else None

    queue_name = pick(rc.queue_name, "queue_name")
    if queue_name == LOCAL_QUEUE:
        queue_name = None
    category = pick(rc.queue_category, "queue_category")
    if isinstance(category, (list, tuple)):
        categories = normalize_queue_categories(category)
        category = categories if len(categories) > 1 else categories[0]
    elif category:
        category = normalize_queue_category(category)
    return JobTarget(
        image=pick(job.image, "image"),
        image_region=pick(job.image_region, "image_region"),
        queue_name=queue_name,
        queue_category=category or None,
        project_set_name=pick(rc.project_set_name, "project_set_name"),
        project_name=pick(rc.project_name, "project_name"),
        region_name=pick(rc.region_name, "region_name"),
        zone_name=pick(rc.zone_name, "zone_name"),
    )


def validate_job(job: Job, spec: ExperimentSpec | None):
    """Check a job's effective queue settings before any platform call.

    Raises ValueError. Lists of accelerator models/counts or categories are
    only meaningful with ``queue_name: auto``; an auto job needs at least one
    accelerator model and count, and roles may not override them. run_options
    must satisfy every public category the job may land in.
    """
    target = job_target(job, spec)
    rc = job.resource_config
    if target.is_auto:
        choices = rc.accelerator_choices()
        if any(model is None for model, _ in choices):
            raise ValueError("queue_name: auto requires resource_config.accelerator_model")
        if any(count is None for _, count in choices):
            raise ValueError("queue_name: auto requires resource_config.accelerator_count")
        for role in rc.roles or []:
            if role.accelerator_model is not None or role.accelerator_count is not None:
                raise ValueError(
                    f"queue_name: auto: role '{role.name}' must not override "
                    "accelerator_model/accelerator_count (they are chosen per job)"
                )
    elif rc.has_choice_lists or len(target.categories) > 1:
        raise ValueError(
            "accelerator_model/accelerator_count/queue_category lists require "
            "resource_config.queue_name: auto"
        )
    if PUBLIC in target.categories:
        job.run_options.validate_for_category(PUBLIC)
    if rc.priority is not None:
        for category in target.categories:
            required = CATEGORY_PRIORITY.get(category)
            if required and str(rc.priority).lower() != required:
                raise ValueError(
                    f"{queue_category_alias(category)} queues only accept priority "
                    f"{required}, got '{rc.priority}' (omit priority and jrun sets "
                    "the required one per category)"
                )


# --- remote submission ---------------------------------------------------------

@dataclass(frozen=True)
class RemoteSpec:
    """Top-level ``remote:`` block: submit some jobs through a jrun service.

    Jobs whose effective (placed) category is in ``categories`` are sent to the
    service at ``url``, which submits them with its own account; the rest are
    submitted locally. Default: only public jobs.
    """

    url: str
    categories: tuple[str, ...] = (PUBLIC,)

    FIELDS = ("url", "categories")

    @classmethod
    def from_dict(cls, data) -> "RemoteSpec":
        if not isinstance(data, dict):
            raise ValueError("remote: must be a mapping with at least 'url'")
        unknown = sorted(set(data) - set(cls.FIELDS))
        if unknown:
            raise ValueError(f"remote: unknown field(s) {', '.join(unknown)} "
                             f"(allowed: {', '.join(cls.FIELDS)})")
        url = str(data.get("url") or "").strip().rstrip("/")
        if not url.startswith(("http://", "https://")):
            raise ValueError("remote.url must be an http:// or https:// URL")
        categories = data.get("categories")
        categories = (normalize_queue_categories(categories)
                      if categories is not None else (PUBLIC,))
        return cls(url=url, categories=categories)

    def routes(self, job: "Job", spec: "ExperimentSpec | None") -> bool:
        """True when the (placed) job goes through the remote service."""
        target = job_target(job, spec)
        if target.queue_name is None:  # local runs never go remote
            return False
        return target.category in self.categories


def total_replicas(rc: ResourceConfig) -> int:
    """Number of nodes a job requests (1 without roles)."""
    if not rc.roles:
        return 1
    return sum(max(int(role.replicas or 0), 0) for role in rc.roles)
