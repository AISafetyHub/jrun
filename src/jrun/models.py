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
class Job:
    name: str
    command: str
    params: dict = field(default_factory=dict)
    resource_config: ResourceConfig = field(default_factory=ResourceConfig)
    envs: dict = field(default_factory=dict)

    @property
    def is_local(self) -> bool:
        return self.resource_config.queue_name == "local"

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "command": self.command,
            "params": self.params,
            "resource_config": self.resource_config.to_dict(),
            "envs": self.envs,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Job":
        return cls(
            name=d["name"],
            command=d.get("command", ""),
            params=d.get("params", {}),
            resource_config=ResourceConfig.from_dict(d.get("resource_config", {})),
            envs=d.get("envs", {}),
        )
