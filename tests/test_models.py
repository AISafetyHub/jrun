import pytest
from jrun.models import Job, ResourceConfig, WorkerConfig


class TestWorkerConfig:
    def test_from_dict_defaults(self):
        w = WorkerConfig.from_dict({})
        assert w.replicas == 1
        assert w.accelerator_model is None

    def test_from_dict_full(self):
        w = WorkerConfig.from_dict({
            "replicas": 4,
            "accelerator_model": "A100",
            "accelerator_count": 8,
            "cpu_cores": 32,
            "mem_gib": 128,
            "shared_mem_gib": 64,
        })
        assert w.replicas == 4
        assert w.accelerator_model == "A100"
        assert w.accelerator_count == 8

    def test_to_dict_omits_none(self):
        w = WorkerConfig(replicas=2, accelerator_model="A100")
        d = w.to_dict()
        assert d == {"replicas": 2, "accelerator_model": "A100"}
        assert "cpu_cores" not in d

    def test_roundtrip(self):
        original = {"replicas": 3, "accelerator_count": 4, "mem_gib": 64}
        w = WorkerConfig.from_dict(original)
        assert w.to_dict() == original


class TestResourceConfig:
    def test_from_dict_empty(self):
        rc = ResourceConfig.from_dict({})
        assert rc.queue_name is None
        assert rc.worker is None

    def test_from_dict_with_worker(self):
        rc = ResourceConfig.from_dict({
            "queue_name": "gpu",
            "priority": "high",
            "worker": {"replicas": 2, "accelerator_count": 4},
        })
        assert rc.queue_name == "gpu"
        assert rc.priority == "high"
        assert rc.worker is not None
        assert rc.worker.replicas == 2

    def test_to_dict_includes_worker(self):
        rc = ResourceConfig(
            queue_name="gpu",
            worker=WorkerConfig(replicas=2),
        )
        d = rc.to_dict()
        assert d["queue_name"] == "gpu"
        assert d["worker"] == {"replicas": 2}

    def test_to_dict_no_worker(self):
        rc = ResourceConfig(queue_name="cpu")
        d = rc.to_dict()
        assert "worker" not in d

    def test_roundtrip(self):
        original = {
            "queue_name": "gpu",
            "accelerator_model": "A100",
            "accelerator_count": 8,
            "worker": {"replicas": 2, "accelerator_count": 4},
        }
        rc = ResourceConfig.from_dict(original)
        assert rc.to_dict() == original


class TestJob:
    def test_basic(self):
        j = Job(name="test", command="echo hi")
        assert j.name == "test"
        assert j.command == "echo hi"
        assert j.params == {}
        assert j.envs == {}

    def test_is_local_true(self):
        j = Job(
            name="local_job",
            command="echo hi",
            resource_config=ResourceConfig(queue_name="local"),
        )
        assert j.is_local is True

    def test_is_local_false(self):
        j = Job(name="remote_job", command="echo hi")
        assert j.is_local is False

    def test_is_local_other_queue(self):
        j = Job(
            name="gpu_job",
            command="echo hi",
            resource_config=ResourceConfig(queue_name="gpu"),
        )
        assert j.is_local is False

    def test_to_dict(self):
        j = Job(
            name="test",
            command="echo hi",
            params={"lr": 0.01},
            envs={"HOME": "/root"},
        )
        d = j.to_dict()
        assert d["name"] == "test"
        assert d["command"] == "echo hi"
        assert d["params"] == {"lr": 0.01}
        assert d["envs"] == {"HOME": "/root"}
        assert d["resource_config"] == {}

    def test_from_dict(self):
        d = {
            "name": "test",
            "command": "echo hi",
            "params": {"lr": 0.01},
            "resource_config": {"queue_name": "local"},
            "envs": {"K": "V"},
        }
        j = Job.from_dict(d)
        assert j.name == "test"
        assert j.is_local is True
        assert j.envs == {"K": "V"}

    def test_from_dict_minimal(self):
        j = Job.from_dict({"name": "x"})
        assert j.name == "x"
        assert j.command == ""
        assert j.params == {}

    def test_roundtrip(self):
        original = {
            "name": "job1",
            "command": "train.py",
            "params": {"lr": 0.001, "bs": 32},
            "resource_config": {
                "queue_name": "gpu",
                "accelerator_count": 8,
                "worker": {"replicas": 2, "accelerator_count": 4},
            },
            "envs": {"NCCL_DEBUG": "INFO"},
        }
        j = Job.from_dict(original)
        assert j.to_dict() == original
