import os
import pytest
from pathlib import Path

from jrun.config import ConfigLoader


@pytest.fixture
def write_config(tmp_path):
    def _write(content: str, name: str = "config.yaml") -> str:
        p = tmp_path / name
        p.write_text(content)
        return str(p)
    return _write


class TestConfigLoaderBasic:
    def test_load_single_job(self, write_config):
        cfg = write_config("""
job:
  name: my-job
  command: echo hello
""")
        loader = ConfigLoader(cfg)
        job = loader.build_single_job()
        assert job is not None
        assert job.name == "my-job"
        assert job.command == "echo hello"

    def test_load_single_job_with_commands_list(self, write_config):
        cfg = write_config("""
job:
  name: multi-cmd
  commands:
    - cd /workspace
    - python train.py
""")
        loader = ConfigLoader(cfg)
        job = loader.build_single_job()
        assert "cd /workspace" in job.command
        assert "python train.py" in job.command

    def test_build_single_job_returns_none_for_search(self, write_config):
        cfg = write_config("""
search:
  job_template:
    name: test-{lr}
    command: train.py --lr {lr}
  params:
    - name: lr
      values: [0.01]
""")
        loader = ConfigLoader(cfg)
        assert loader.build_single_job() is None


class TestVariableResolution:
    def test_config_dir_substitution(self, write_config, tmp_path):
        cfg = write_config("""
job:
  name: test
  command: cat $CONFIG_DIR/data.txt
""")
        loader = ConfigLoader(cfg)
        job = loader.build_single_job()
        assert str(tmp_path) in job.command

    def test_env_var_substitution(self, write_config, monkeypatch):
        monkeypatch.setenv("MY_TEST_VAR", "/data/models")
        cfg = write_config("""
job:
  name: test
  command: ls $MY_TEST_VAR
""")
        loader = ConfigLoader(cfg)
        job = loader.build_single_job()
        assert job.command == "ls /data/models"

    def test_undefined_var_raises(self, write_config, monkeypatch):
        monkeypatch.delenv("UNDEFINED_XYZ_VAR", raising=False)
        cfg = write_config("""
job:
  name: test
  command: echo $UNDEFINED_XYZ_VAR
""")
        with pytest.raises(ValueError, match="Undefined variable"):
            ConfigLoader(cfg)

    def test_dollar_dollar_escaping(self, write_config):
        cfg = write_config("""
job:
  name: test
  command: echo $$HOME
""")
        loader = ConfigLoader(cfg)
        job = loader.build_single_job()
        assert job.command == "echo $HOME"


class TestGridExpansion:
    def test_single_param(self, write_config):
        cfg = write_config("""
search:
  job_template:
    name: job-{lr}
    command: train.py --lr {lr}
  params:
    - name: lr
      values: [0.01, 0.001, 0.0001]
""")
        loader = ConfigLoader(cfg)
        jobs = loader.expand_grid()
        assert len(jobs) == 3
        assert jobs[0].name == "job-0.01"
        assert jobs[0].params == {"lr": 0.01}

    def test_multi_param_cartesian(self, write_config):
        cfg = write_config("""
search:
  job_template:
    name: job-{lr}-{bs}
    command: train.py --lr {lr} --bs {bs}
  params:
    - name: lr
      values: [0.01, 0.001]
    - name: bs
      values: [16, 32]
""")
        loader = ConfigLoader(cfg)
        jobs = loader.expand_grid()
        assert len(jobs) == 4
        names = {j.name for j in jobs}
        assert "job-0.01-16" in names
        assert "job-0.001-32" in names

    def test_max_trials(self, write_config):
        cfg = write_config("""
search:
  max_trials: 2
  job_template:
    name: job-{x}
    command: echo {x}
  params:
    - name: x
      values: [1, 2, 3, 4, 5]
""")
        loader = ConfigLoader(cfg)
        jobs = loader.expand_grid()
        assert len(jobs) == 2

    def test_expand_grid_returns_empty_for_single_job(self, write_config):
        cfg = write_config("""
job:
  name: single
  command: echo hi
""")
        loader = ConfigLoader(cfg)
        assert loader.expand_grid() == []

    def test_resource_config_in_template(self, write_config):
        cfg = write_config("""
search:
  job_template:
    name: gpu-{lr}
    command: train.py
    resource_config:
      queue_name: gpu
      accelerator_count: 8
  params:
    - name: lr
      values: [0.01]
""")
        loader = ConfigLoader(cfg)
        jobs = loader.expand_grid()
        assert jobs[0].resource_config.queue_name == "gpu"
        assert jobs[0].resource_config.accelerator_count == 8

    def test_envs_in_template(self, write_config):
        cfg = write_config("""
search:
  job_template:
    name: job-{lr}
    command: train.py
    envs:
      LR_VALUE: "{lr}"
      STATIC: hello
  params:
    - name: lr
      values: [0.01]
""")
        loader = ConfigLoader(cfg)
        jobs = loader.expand_grid()
        assert jobs[0].envs["LR_VALUE"] == "0.01"
        assert jobs[0].envs["STATIC"] == "hello"

    def test_unsupported_sampling_raises(self, write_config):
        cfg = write_config("""
search:
  sampling: random
  job_template:
    name: job-{x}
    command: echo {x}
  params:
    - name: x
      values: [1]
""")
        loader = ConfigLoader(cfg)
        with pytest.raises(ValueError, match="Unsupported sampling"):
            loader.expand_grid()


class TestSearchName:
    def test_explicit_name(self, write_config):
        cfg = write_config("""
search:
  name: my-search
  job_template:
    name: job-{x}
    command: echo
  params:
    - name: x
      values: [1]
""")
        loader = ConfigLoader(cfg)
        assert loader.get_search_name() == "my-search"

    def test_inferred_name(self, write_config):
        cfg = write_config("""
search:
  job_template:
    name: train_{lr}_{bs}_run
    command: echo
  params:
    - name: lr
      values: [0.01]
    - name: bs
      values: [32]
""")
        loader = ConfigLoader(cfg)
        name = loader.get_search_name()
        assert "lr" not in name
        assert "bs" not in name
        assert name  # non-empty

    def test_no_search_returns_none(self, write_config):
        cfg = write_config("""
job:
  name: single
  command: echo
""")
        loader = ConfigLoader(cfg)
        assert loader.get_search_name() is None


class TestSchedulerParams:
    def test_defaults(self, write_config):
        cfg = write_config("""
search:
  job_template:
    name: j-{x}
    command: echo
  params:
    - name: x
      values: [1]
""")
        loader = ConfigLoader(cfg)
        sp = loader.get_scheduler_params()
        assert sp["parallel_trials"] is None
        assert sp["poll_interval"] == 30

    def test_custom_values(self, write_config):
        cfg = write_config("""
search:
  parallel_trials: 4
  poll_interval: 60
  job_template:
    name: j-{x}
    command: echo
  params:
    - name: x
      values: [1]
""")
        loader = ConfigLoader(cfg)
        sp = loader.get_scheduler_params()
        assert sp["parallel_trials"] == 4
        assert sp["poll_interval"] == 60


class TestAutoResolve:
    def test_auto_hash(self, write_config):
        cfg = write_config("""
search:
  job_template:
    name: job-{auto:6s}
    command: echo {x}
  params:
    - name: x
      values: [1, 2]
""")
        loader = ConfigLoader(cfg)
        jobs = loader.expand_grid()
        assert len(jobs) == 2
        assert len(jobs[0].name) == len("job-") + 6
        # Different params produce different hashes
        assert jobs[0].name != jobs[1].name
