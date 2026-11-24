import pytest

from jrun.config import ConfigLoader
from jrun.errors import ConfigError


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
        with pytest.raises(ConfigError, match=r"undefined variable \$UNDEFINED_XYZ_VAR"):
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

    def test_comments_with_undefined_vars_ignored(self, write_config, monkeypatch):
        monkeypatch.delenv("SHARED_OUTPUT_DIR", raising=False)
        cfg = write_config("""
# This is a comment with $SHARED_OUTPUT_DIR reference
# Another comment: $UNDEFINED_VAR should be ignored
job:
  name: test
  command: echo hello
  # Inline comment with $ANOTHER_UNDEFINED_VAR
""")
        loader = ConfigLoader(cfg)
        job = loader.build_single_job()
        assert job.command == "echo hello"


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
        with pytest.raises(ConfigError, match="unsupported sampling"):
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


class TestRemovedAndInvalidKeys:
    @pytest.mark.parametrize("section,key", [
        ("search", "experiment_id"),
        ("search", "experiment_name"),
        ("job", "experiment_id"),
        ("job", "experiment_name"),
        ("job", "submission"),
    ])
    def test_legacy_section_keys_rejected(self, write_config, section, key):
        body = ("job:\n  name: j\n  command: c\n" if section == "job" else
                "search:\n  job_template:\n    name: j-{x}\n    command: c\n"
                "  params:\n    - name: x\n      values: [1]\n")
        cfg = write_config(body + f"  {key}: value\n")
        with pytest.raises(ConfigError, match=f"{section}.{key} is no longer supported"):
            ConfigLoader(cfg)

    def test_legacy_key_in_job_template_rejected(self, write_config):
        cfg = write_config("""
search:
  job_template:
    name: j-{x}
    command: c
    submission: {duration_hours: 1}
  params:
    - name: x
      values: [1]
""")
        with pytest.raises(ConfigError, match="job_template.submission"):
            ConfigLoader(cfg)

    def test_worker_rejected(self, write_config):
        cfg = write_config("""
job:
  name: j
  command: c
  resource_config:
    worker: {replicas: 2}
""")
        with pytest.raises(ConfigError, match="worker was removed"):
            ConfigLoader(cfg).jobs()

    def test_unknown_job_field_rejected(self, write_config):
        cfg = write_config("""
job:
  name: j
  comand: typo
""")
        with pytest.raises(ConfigError, match="unknown field.*comand"):
            ConfigLoader(cfg).jobs()

    def test_platform_overrides_explicitly_allowed(self, write_config):
        cfg = write_config("""
job:
  name: j
  command: c
  platform_overrides: {slots_per_worker: 2}
""")
        assert ConfigLoader(cfg).jobs()[0].platform_overrides == {"slots_per_worker": 2}

    def test_invalid_yaml(self, write_config):
        cfg = write_config("job: [unclosed\n")
        with pytest.raises(ConfigError, match="invalid YAML"):
            ConfigLoader(cfg)

    def test_missing_file(self, tmp_path):
        with pytest.raises(ConfigError, match="cannot read config"):
            ConfigLoader(str(tmp_path / "missing.yaml"))

    def test_neither_search_nor_job(self, write_config):
        cfg = write_config("experiment:\n  name: e\n")
        with pytest.raises(ConfigError, match="neither 'search' nor 'job'"):
            ConfigLoader(cfg).jobs()


class TestJobsAndCommands:
    def test_jobs_single_and_search(self, write_config):
        single = ConfigLoader(write_config("job:\n  name: a\n  command: c\n", "a.yaml"))
        assert [j.name for j in single.jobs()] == ["a"]
        assert single.is_search is False
        search = ConfigLoader(write_config(
            "search:\n  job_template:\n    name: b-{x}\n    command: c\n"
            "  params:\n    - name: x\n      values: [1, 2]\n", "b.yaml"))
        assert [j.name for j in search.jobs()] == ["b-1", "b-2"]
        assert search.is_search is True

    def test_commands_joined_with_newlines(self, write_config):
        cfg = write_config("""
job:
  name: j
  commands:
    - cd /work
    - python train.py
""")
        assert ConfigLoader(cfg).jobs()[0].command == "cd /work\npython train.py"

    def test_grid_params_substitute_nested_fields(self, write_config):
        cfg = write_config("""
search:
  job_template:
    name: j-{n}
    commands: ["run --n {n}"]
    image: img:{n}
    resource_config:
      accelerator_count: 1
      roles:
        - {name: Worker, replicas: 2, accelerator_model: "M{n}"}
  params:
    - name: n
      values: [7]
""")
        job = ConfigLoader(cfg).jobs()[0]
        assert job.command == "run --n 7"
        assert job.image == "img:7"
        assert job.resource_config.roles[0].accelerator_model == "M7"
        assert job.params == {"n": 7}


class TestExperimentSpec:
    def test_parses_experiment_block(self, write_config):
        cfg = write_config("""
experiment:
  name: my-exp
  image: harbor.x/pytorch:23.08
  queue_name: jiuding_airs1_h100
  project_set_name: ps
  project_name: project
  region_name: region
  zone_name: zone
job:
  name: my-job
  command: echo hello
""")
        spec = ConfigLoader(cfg).get_experiment_spec()
        assert spec.name == "my-exp"
        assert spec.image == "harbor.x/pytorch:23.08"
        assert spec.queue_name == "jiuding_airs1_h100"
        assert spec.project_set_name == "ps"
        assert spec.project_name == "project"
        assert spec.region_name == "region"
        assert spec.zone_name == "zone"

    def test_absent_block_returns_none(self, write_config):
        cfg = write_config("""
job:
  name: my-job
  command: echo hello
""")
        assert ConfigLoader(cfg).get_experiment_spec() is None

    def test_missing_required_fields_raises(self, write_config):
        from jrun.errors import ConfigError
        # Only `name` is required; image/queue live at job level now.
        cfg = write_config("""
experiment:
  image: img:1
job:
  name: my-job
  command: echo hello
""")
        with pytest.raises(ConfigError, match="name"):
            ConfigLoader(cfg).get_experiment_spec()

    def test_name_only_spec_is_valid(self, write_config):
        cfg = write_config("""
experiment:
  name: my-exp
job:
  name: my-job
  command: echo hello
""")
        spec = ConfigLoader(cfg).get_experiment_spec()
        assert spec.name == "my-exp"
        assert spec.image is None
        assert spec.queue_name is None

    def test_job_level_image_fields(self, write_config):
        cfg = write_config("""
job:
  name: my-job
  image: harbor.x/pytorch:23.08
  image_region: PRIVATE
  command: echo hello
""")
        job = ConfigLoader(cfg).build_single_job()
        assert job.image == "harbor.x/pytorch:23.08"
        assert job.image_region == "PRIVATE"

    def test_grid_image_param_substitution(self, write_config):
        cfg = write_config("""
search:
  job_template:
    name: job_{model}
    image: harbor.x/{model}:latest
    command: echo {model}
  sampling: grid
  params:
    - name: model
      values: [m1, m2]
""")
        jobs = ConfigLoader(cfg).expand_grid()
        assert [j.image for j in jobs] == ["harbor.x/m1:latest", "harbor.x/m2:latest"]

    def test_variable_substitution_applies(self, write_config, monkeypatch):
        monkeypatch.setenv("EXP_QUEUE", "queue-from-env")
        cfg = write_config("""
experiment:
  name: my-exp
  image: img:1
  queue_name: $EXP_QUEUE
job:
  name: my-job
  command: echo hello
""")
        assert ConfigLoader(cfg).get_experiment_spec().queue_name == "queue-from-env"

    def test_image_region_optional(self, write_config):
        cfg = write_config("""
experiment:
  name: my-exp
  image: img:1
  queue_name: q1
job:
  name: my-job
  command: echo hello
""")
        assert ConfigLoader(cfg).get_experiment_spec().image_region is None

        cfg = write_config("""
experiment:
  name: my-exp
  image: img:1
  image_region: PRIVATE
  queue_name: q1
job:
  name: my-job
  command: echo hello
""")
        assert ConfigLoader(cfg).get_experiment_spec().image_region == "PRIVATE"

    def test_invalid_image_region_raises(self, write_config):
        from jrun.errors import ConfigError
        cfg = write_config("""
experiment:
  name: my-exp
  image: img:1
  image_region: INTERNAL
  queue_name: q1
job:
  name: my-job
  command: echo hello
""")
        with pytest.raises(ConfigError, match="unknown image_region"):
            ConfigLoader(cfg).get_experiment_spec()
