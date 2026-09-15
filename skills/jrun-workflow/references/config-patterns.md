# jrun Configuration Patterns

Read this reference when creating or reviewing jrun YAML. It is intentionally
self-contained and uses placeholders. Never submit a placeholder image, queue,
path, experiment, or resource allocation.

## Configuration rules

- A file may contain one `job:` or one `search:` workload, not both.
- A top-level `description:` is optional and informational.
- A top-level `experiment:` is optional and carries only the experiment's
  identity: `name` is required; `description`, `proj_id`, `projset_id`,
  `cluster_id`, and `zone_id` are optional. On the platform an experiment is
  just a container for job configs, so image and queue do NOT belong here.
- The Docker image is a job-level field: `image:` (plus optional
  `image_region: PRIVATE`, default `PUBLIC`) on `job:` or
  `search.job_template:`. The queue is `resource_config.queue_name`, resolved
  to queue/cluster/zone IDs via the platform API at submit time. When the
  experiment must be created, the placeholder config uses the first job's
  image/queue. The old `experiment.image`/`image_region`/`queue_name` fields
  still work as fallbacks but are deprecated (jrun warns).
- Alternatively, put `experiment_id` or `experiment_name` in `job:` or
  `search:` to target an existing experiment.
- `commands:` lists become newline-separated shell input. Use one `command:`
  containing `&&`, or add `set -e`, when fail-fast sequencing is required.
- `sampling: grid` is the supported search method. Parameter values form a
  Cartesian product and `max_trials` truncates that product.
- `parallel_trials` limits active submissions and starts a background scheduler
  for the rest. `poll_interval` defaults to 30 seconds.
- Worker resource fields inherit the master's values when omitted.

## Submit working-set semantics

On every non-dry-run `jrun submit`, the target experiment's
`advance_config_infos` is replaced in one operation with exactly the selected
jobs from the current YAML: new jobs and jobs the user confirmed for
overwrite. Existing names that are skipped, and configs left by an earlier
submit, are not included in the replacement list. The first existing config
is used as the platform structure template for each new entry.

After the modify succeeds, jrun runs each selected config by name. It does not
stop or cancel the old platform job during an overwrite. The old name-to-ID
entry is removed from `.jrun/jobs.json` only after the replacement starts; a
modify or run failure leaves that local entry available. The tracker is the
history/status source, while experiment configs represent only the current
working set. A shared experiment is therefore replaced wholesale on submit.
With `parallel_trials`, all selected configs are installed by the initial
modify and the scheduler only runs pending configs.

## Single remote job

```yaml
description: Evaluate one model

experiment:
  name: replace-with-experiment-name

job:
  name: eval_one_model
  image: replace-with-registry/image:tag
  # image_region: PRIVATE  # optional, default PUBLIC
  commands:
    - set -e
    - cd /workspace
    - python evaluate.py --model model-a
  envs:
    HOME: /home/user
    DATA_DIR: "$CONFIG_DIR/data"
    PATH: "$$PATH:/workspace/bin"
  resource_config:
    queue_name: replace-with-queue-name
    priority: high
    accelerator_model: replace-with-accelerator-model
    accelerator_count: 1
    cpu_cores: 8
    mem_gib: 64
    shared_mem_gib: 16
```

If the experiment already exists and its ID is known, omit the top-level
`experiment:` block and add `experiment_id` under `job:`.

## Grid search

```yaml
description: Evaluate models across datasets

experiment:
  name: replace-with-experiment-name

search:
  name: model_dataset_eval
  job_template:
    name: eval_{model}_{dataset}_{auto:4s}
    image: replace-with-registry/image:tag  # supports {param} substitution
    commands:
      - set -e
      - cd /workspace
      - python evaluate.py --model {model} --dataset {dataset}
    envs:
      MODEL_NAME: "{model}"
      DATASET_NAME: "{dataset}"
    resource_config:
      queue_name: replace-with-queue-name
      priority: high
      accelerator_model: replace-with-accelerator-model
      accelerator_count: 1
      cpu_cores: 8
      mem_gib: 64
      shared_mem_gib: 16
  sampling: grid
  max_trials: 20
  parallel_trials: 4
  poll_interval: 30
  params:
    - name: model
      values: [model-a, model-b]
    - name: dataset
      values: [validation, test]
```

This example expands to four jobs. Verify that `max_trials` and
`parallel_trials` match the user's cost and capacity intent before submit.

## Multi-node job

```yaml
description: Train with one master and two workers

experiment:
  name: replace-with-experiment-name

job:
  name: distributed_train
  image: replace-with-registry/image:tag
  commands:
    - set -e
    - cd /workspace
    - torchrun --nproc_per_node=1 --nnodes=3 train.py
  envs:
    NCCL_DEBUG: INFO
    MASTER_PORT: "29500"
  resource_config:
    queue_name: replace-with-queue-name
    priority: high
    accelerator_model: replace-with-accelerator-model
    accelerator_count: 1
    cpu_cores: 8
    mem_gib: 32
    shared_mem_gib: 16
    worker:
      replicas: 2
      accelerator_count: 1
      cpu_cores: 8
      mem_gib: 32
      shared_mem_gib: 16
```

`worker.replicas` counts workers in addition to the master. Ensure the
application's node count agrees with `1 + replicas`.

## Local debug job

```yaml
description: Verify the command locally before remote submission

job:
  name: local_smoke_test
  command: python -c "print('jrun local smoke test')"
  resource_config:
    queue_name: local
```

Local jobs execute directly in the current shell environment and do not need a
remote experiment. Always dry-run and confirm before execution. A local
`search:` executes only the first expanded job, so use a single `job:` for an
unambiguous local smoke test.

## Field reference

| Field | Location | Meaning |
|---|---|---|
| `name` | `experiment` | Experiment name (required when the block is present). |
| `name` | `job` / `job_template` | Job name; search templates support parameter and auto substitutions. |
| `image` | `job` / `job_template` | Docker image the job runs with; supports `{param}` in search. |
| `image_region` | `job` / `job_template` | Image repo type: `PUBLIC` (default) or `PRIVATE`. |
| `command` / `commands` | workload | One command string or a list joined with newlines. |
| `envs` | workload | Environment variables; values support grid and dollar substitutions. |
| `queue_name` | `resource_config` | Submission queue, resolved via the platform API; `local` executes on the current machine. |
| `priority` | `resource_config` | Common values are `low`, `medium`, and `high`. |
| `accelerator_model` | resource / worker | Accelerator type supplied by the platform. |
| `accelerator_count` | resource / worker | Accelerator count per replica. |
| `cpu_cores` | resource / worker | CPU cores per replica. |
| `mem_gib` | resource / worker | Memory in GiB per replica. |
| `shared_mem_gib` | resource / worker | Shared memory in GiB per replica. |
| `worker.replicas` | `resource_config` | Number of worker replicas in addition to the master. |

## Variable reference

| Syntax | Expansion |
|---|---|
| `$CONFIG_DIR` | Directory containing the YAML file. |
| `$VAR` | Local environment variable, resolved before YAML parsing. |
| `$$VAR` | Literal remote `$VAR` reference after jrun unescapes it. |
| `{param}` | Current grid value in job names, commands, and environment values. |
| `{auto:Ns}` | Deterministic `N`-character MD5-derived suffix for job names. |

Comment-only lines are not environment-expanded. An undefined `$VAR` in
active YAML causes configuration loading to fail.

## Safe preview checklist

1. Run `jrun submit <config> --dry-run`.
2. Check experiment, image, queue, priority, accelerator count, CPU, memory,
   shared memory, worker replicas, and total expanded jobs.
3. Check every rendered command and only the names, not secret values, of
   environment variables.
4. Confirm that no placeholder text remains.
5. Obtain explicit user approval immediately before any actual submit,
   overwrite, stop, removal, experiment creation, or local execution.
