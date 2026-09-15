from dataclasses import dataclass, field


@dataclass
class WorkerConfig:
    replicas: int = 1
    accelerator_model: str | None = None
    accelerator_count: int | None = None
    cpu_cores: int | None = None
    mem_gib: int | None = None
    shared_mem_gib: int | None = None

    @classmethod
    def from_dict(cls, d: dict) -> "WorkerConfig":
        return cls(
            replicas=d.get("replicas", 1),
            accelerator_model=d.get("accelerator_model"),
            accelerator_count=d.get("accelerator_count"),
            cpu_cores=d.get("cpu_cores"),
            mem_gib=d.get("mem_gib"),
            shared_mem_gib=d.get("shared_mem_gib"),
        )

    def to_dict(self) -> dict:
        d = {"replicas": self.replicas}
        for key in ("accelerator_model", "accelerator_count", "cpu_cores", "mem_gib", "shared_mem_gib"):
            val = getattr(self, key)
            if val is not None:
                d[key] = val
        return d


@dataclass
class ResourceConfig:
    queue_name: str | None = None
    priority: str | None = None
    accelerator_model: str | None = None
    accelerator_count: int | None = None
    cpu_cores: int | None = None
    mem_gib: int | None = None
    shared_mem_gib: int | None = None
    worker: WorkerConfig | None = None

    @classmethod
    def from_dict(cls, d: dict) -> "ResourceConfig":
        worker_data = d.get("worker")
        worker = WorkerConfig.from_dict(worker_data) if worker_data else None
        return cls(
            queue_name=d.get("queue_name"),
            priority=d.get("priority"),
            accelerator_model=d.get("accelerator_model"),
            accelerator_count=d.get("accelerator_count"),
            cpu_cores=d.get("cpu_cores"),
            mem_gib=d.get("mem_gib"),
            shared_mem_gib=d.get("shared_mem_gib"),
            worker=worker,
        )

    def to_dict(self) -> dict:
        d = {}
        for key in ("queue_name", "priority", "accelerator_model", "accelerator_count",
                    "cpu_cores", "mem_gib", "shared_mem_gib"):
            val = getattr(self, key)
            if val is not None:
                d[key] = val
        if self.worker:
            d["worker"] = self.worker.to_dict()
        return d


@dataclass
class ExperimentSpec:
    """Top-level `experiment:` block: identity of the experiment to submit into.

    Only `name` is required. jrun looks the experiment up by name and creates
    it via the platform REST API if missing (using the first job's image/queue
    for the placeholder config).

    Deprecated fallback fields: `image`/`image_region`/`queue_name` used to be
    required here; they now live at job level (`image:` and
    `resource_config.queue_name:`) and are only used for jobs that don't set
    them. `cluster_id`/`zone_id` explicitly override queue resolution.
    """
    DEFAULT_IMAGE_REGION = "PUBLIC"
    # airs.v1.common.RepoTypes: the REST API accepts enum names, but the
    # airsctl experiment-modify JSON file needs the integer value.
    IMAGE_REGION_VALUES = {"PUBLIC": 1, "PRIVATE": 2}

    name: str
    image: str | None = None
    queue_name: str | None = None
    description: str | None = None
    image_region: str = DEFAULT_IMAGE_REGION
    cluster_id: str | None = None
    zone_id: str | None = None
    proj_id: str | None = None
    projset_id: str | None = None

    @classmethod
    def from_dict(cls, d: dict) -> "ExperimentSpec":
        image_region = (d.get("image_region") or cls.DEFAULT_IMAGE_REGION).upper()
        if image_region not in cls.IMAGE_REGION_VALUES:
            raise ValueError(
                f"unknown image_region '{d.get('image_region')}'; "
                f"expected one of {sorted(cls.IMAGE_REGION_VALUES)}"
            )
        return cls(
            name=d["name"],
            image=d.get("image"),
            queue_name=d.get("queue_name"),
            description=d.get("description"),
            image_region=image_region,
            cluster_id=d.get("cluster_id"),
            zone_id=d.get("zone_id"),
            proj_id=d.get("proj_id"),
            projset_id=d.get("projset_id"),
        )

    @property
    def image_region_value(self) -> int:
        """Integer enum value for the airsctl experiment-modify JSON."""
        return self.IMAGE_REGION_VALUES[self.image_region]

    def to_dict(self) -> dict:
        d = {"name": self.name}
        if self.image_region != self.DEFAULT_IMAGE_REGION:
            d["image_region"] = self.image_region
        for key in ("image", "queue_name", "description",
                    "cluster_id", "zone_id", "proj_id", "projset_id"):
            val = getattr(self, key)
            if val is not None:
                d[key] = val
        return d


@dataclass
class Job:
    name: str
    command: str
    params: dict = field(default_factory=dict)
    resource_config: ResourceConfig = field(default_factory=ResourceConfig)
    envs: dict = field(default_factory=dict)
    image: str | None = None
    image_region: str | None = None

    @property
    def is_local(self) -> bool:
        return self.resource_config.queue_name == "local"

    def to_dict(self) -> dict:
        d = {
            "name": self.name,
            "command": self.command,
            "params": self.params,
            "resource_config": self.resource_config.to_dict(),
            "envs": self.envs,
        }
        if self.image is not None:
            d["image"] = self.image
        if self.image_region is not None:
            d["image_region"] = self.image_region
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Job":
        return cls(
            name=d["name"],
            command=d.get("command", ""),
            params=d.get("params", {}),
            resource_config=ResourceConfig.from_dict(d.get("resource_config", {})),
            envs=d.get("envs", {}),
            image=d.get("image"),
            image_region=d.get("image_region"),
        )


def job_runtime(job: Job, spec: "ExperimentSpec | None") -> tuple[str | None, str | None, str | None]:
    """Effective (image, image_region, queue_name) for a job.

    Job-level fields win; the deprecated experiment-block fields are the
    fallback. `queue_name: local` means local execution, not a platform queue.
    """
    queue_name = job.resource_config.queue_name
    if queue_name == "local":
        queue_name = None
    return (
        job.image or (spec.image if spec else None),
        job.image_region or (spec.image_region if spec else None),
        queue_name or (spec.queue_name if spec else None),
    )


def effective_spec(job: Job, spec: "ExperimentSpec | None") -> "ExperimentSpec | None":
    """Per-job override spec for PlatformClient.submit_job.

    Returns None when nothing overrides the experiment template (legacy path:
    no experiment: block and no job-level image/queue).
    """
    image, region, queue_name = job_runtime(job, spec)
    if not spec and not (image or queue_name):
        return None
    return ExperimentSpec(
        name=spec.name if spec else "",
        image=image,
        queue_name=queue_name,
        image_region=(region or ExperimentSpec.DEFAULT_IMAGE_REGION),
        cluster_id=spec.cluster_id if spec else None,
        zone_id=spec.zone_id if spec else None,
    )
