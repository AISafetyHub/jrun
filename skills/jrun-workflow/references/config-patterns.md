# jrun Configuration Patterns

Read this reference when creating or reviewing jrun YAML. It is intentionally
self-contained and uses placeholders. Never submit a placeholder image, queue,
path, experiment, or resource allocation.

## Configuration rules

- A file may contain one `job:` or one `search:` workload, not both (if both
  are present, `search:` wins).
- A top-level `description:` is optional and informational.
- Resource placement uses readable names; platform UUID fields such as
  `queue_id`, `quota_id`, `cluster_id`, and `zone_id` are removed from YAML.
- A top-level `experiment:` block with at least `name:` is required for a real
  submit (`--dry-run` only warns; local jobs do not need it). jrun finds the
  experiment by exact name or creates it. The block may also hold job defaults
  (`image`, `image_region`, `project_set_name`, `project_name`, `region_name`,
  `zone_name`, `queue_name`, `queue_category`) and experiment-level settings
  (`description`, `train_frame`, `restart_policy`, `storage_info`, ...).
  Job-level values always override these defaults.
- `experiment_id`/`experiment_name` under `job:` or `search:`, the
  `submission:` alias, and `resource_config.worker` are removed and rejected.
  There is no project-level default experiment.
- The Docker image is normally a job-level field: `image:` (plus optional
  `image_region: PUBLIC|PRIVATE`, case-insensitive, default `PUBLIC`) on
  `job:` or `search.job_template:`, falling back to `experiment.image`.
  Resource names are resolved to platform IDs at submit time. When the
  experiment must be created, the placeholder config uses the first job's
  image and resolved resource names.
- Unknown keys in `job:` / `search.job_template:` are rejected as typos; raw
  platform fields go under `platform_overrides:`. Unknown keys inside
  `resource_config`, roles, and `experiment` pass through as overrides.
- `commands:` lists become newline-separated shell input. Use one `command:`
  containing `&&`, or add `set -e`, when fail-fast sequencing is required.
- `sampling: grid` is the supported search method. Parameter values form a
  Cartesian product and `max_trials` truncates that product.
- `parallel_trials` limits active submissions and starts a background scheduler
  for the rest. `poll_interval` defaults to 30 seconds.
- Multi-node jobs list node roles in `resource_config.roles`. Each role inherits
  the `resource_config` resource fields and `resource_region`; the role's own
  values override them. Without `roles` the job is single-node (one Master).
- `resource_config.queue_name: auto` lets jrun choose the queue from current
  free cards. Lists in `accelerator_model`/`accelerator_count`/`queue_category`
  are only valid with `queue_name: auto`; see "Automatic queue placement".

## Submit working-set semantics

On every non-dry-run `jrun submit`, the target experiment's
`advance_config_infos` is replaced in one operation with exactly the selected
jobs from the current YAML: new jobs and jobs the user confirmed for
overwrite. Existing names that are skipped, and configs left by an earlier
submit, are not included in the replacement list. The first existing config
is used as the platform structure template for each new entry.

Before the modify, jrun stops the old platform job of every overwritten name
if it is still active (scheduler-held `queued-*` entries are only marked
`Stopped` locally; platform `Pending`/`Approving` jobs are stopped like running
ones; finished jobs are untouched). In a grid, a job whose
old run cannot be stopped is skipped with an error and the rest proceed; for a
single job the stop failure aborts the submit. After the modify succeeds, jrun
runs each selected config by name; a run whose output has no job ID is an
error for that job. The old name-to-ID entry is removed from `.jrun/jobs.json`
only after the replacement starts; a modify or run failure leaves that local
entry available. The tracker is the
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

To target an existing experiment, put its exact name in `experiment.name`;
jrun reuses it instead of creating a new one.

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
    roles:
      - name: Master
        replicas: 1
      - name: Worker
        replicas: 2
        # accelerator_count: 2   # optional per-role override
```

Each role inherits the resource fields of `resource_config` (accelerator,
CPU, memory, shared memory, `resource_region`, ...) and may override them;
roles also accept `extra_label_info` and `platform_overrides`. Ensure the
application's node count agrees with the sum of all role `replicas` (here
`1 + 2 = 3`).

## Shared public or idle queue

```yaml
experiment:
  name: replace-with-experiment-name

job:
  name: shared_smoke
  image: replace-with-registry/image:tag
  command: python train.py
  resource_config:
    queue_category: public       # dedicated, public, or idle
    project_set_name: replace-with-project-set
    project_name: replace-with-project
    region_name: replace-with-region
    zone_name: replace-with-zone
    queue_name: replace-with-shared-queue-name
    accelerator_model: replace-with-accelerator-model
    accelerator_count: 1
  run_options:
    duration_hours: 12
    queue_priority_mode: normal
    usage_desc: evaluation benchmark
```

Public queues require all three approval fields in `run_options` and a
submitted job may sit in `Approving` until approved. Idle queues use the same
queue and resource fields and may be reclaimed when capacity is needed.
`priority` is fixed per shared category: public only accepts `high`, idle only
`low`. Leave `priority` unset for shared queues; jrun writes the required value
and rejects a conflicting one. `run_options` accepts only `duration_hours`, `queue_priority_mode`
(`normal`/`high`), `usage_desc`, and `enable_checkpoint`. `jrun queues` lists
each shared pool under both `public` and `idle`.

## Automatic queue placement

Use this when the user accepts several accelerator models or categories, or
asks to "use whatever has free cards". It matches `examples/auto.yaml`:

```yaml
experiment:
  name: replace-with-experiment-name

search:
  name: auto_eval
  job_template:
    name: auto_eval_{data}
    image: replace-with-registry/image:tag
    commands:
      - set -e
      # Plain torchrun uses every visible GPU (platform PET_NPROC_PER_NODE=auto).
      - torchrun eval.py --data {data}
    resource_config:
      queue_name: auto
      queue_category: [public, idle]   # preference order; add idle only if preemption is OK
      project_set_name: replace-with-project-set
      project_name: replace-with-project
      accelerator_model: [replace-with-model-a, replace-with-model-b]
      accelerator_count: [4, 8]        # paired by position; a scalar applies to every model
      cpu_cores: 32                    # shared by all pairs: size for the largest one
      mem_gib: 256
    run_options:                       # required because public is listed
      duration_hours: 12
      queue_priority_mode: normal
      usage_desc: evaluation benchmark
  params:
    - name: data
      values: [replace-with-dataset-a, replace-with-dataset-b]
  sampling: grid
```

Placement rules (computed at submit time for the jobs being submitted):

1. A job **starts now** on the first candidate, ordered by category preference,
   then pair preference, then most free cards, that has enough free cards
   (`accelerator_count` x total role replicas) and, for public/idle, a remaining
   per-project-set submit slot (limit minus running and queueing jobs).
2. A job that fits nowhere is **queued on the platform**: on a dedicated queue
   if `dedicated` is listed, otherwise on a public/idle queue that still has a
   submit slot.
3. With no public/idle slot left the job **waits**: its config is installed on
   the preferred queue and the background scheduler starts it once a slot
   frees up. A single `job:` that would wait is refused with an error.

Public and idle list the same physical pools: free cards are counted once,
submit slots per mode. Model names match exactly (case-insensitive). Location
names filter the candidate queues. Roles of an auto job must not override
`accelerator_model`/`accelerator_count`. `--dry-run` prints the placement table
using read-only platform queries; the real submit recomputes it.

Each placed job receives these environment variables (verified on a real
job):

| Variable | Value |
|---|---|
| `JRUN_QUEUE_NAME` | Chosen queue name. |
| `JRUN_ACCELERATOR_MODEL` | Chosen accelerator model (platform spelling). |
| `JRUN_ACCELERATOR_COUNT` | Chosen accelerator count per replica. |

Reference them as `$$JRUN_...` in commands so the remote shell expands them.

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
remote experiment; `jrun submit` exits with the command's exit code. Always dry-run and confirm before execution. A local
`search:` executes only the first expanded job, so use a single `job:` for an
unambiguous local smoke test.

## Field reference

| Field | Location | Meaning |
|---|---|---|
| `name` | `experiment` | Experiment name (required; the block is required for a real submit). |
| `image` / `image_region` | experiment | Default image and repo type for jobs that omit their own. |
| `description` / `train_frame` / `restart_policy` / `storage_info` | experiment | Experiment-level settings (description is used on creation). |
| `project_set_name` / `project_name` | experiment / resource | Project path used to resolve queue names. |
| `region_name` / `zone_name` | experiment / resource | Region and availability-zone names used to resolve queue names. |
| `queue_name` / `queue_category` | experiment / resource | Queue name and type (`dedicated`, `public`, or `idle`); `queue_name: auto` allows a category list. |
| `name` | `job` / `job_template` | Job name; search templates support parameter and auto substitutions. |
| `image` | `job` / `job_template` | Docker image the job runs with; supports `{param}` in search. |
| `image_region` | `job` / `job_template` | Image repo type: `PUBLIC` (default) or `PRIVATE`, case-insensitive. |
| `command` / `commands` | workload | One command string or a list joined with newlines. |
| `envs` | workload | Environment variables; values support grid and dollar substitutions. |
| `train_frame` / `restart_policy` | workload | Per-config framework and restart policy. |
| `code` / `code_path` / `git_prefix` / `git_branch` / `git_commit` / `gitee` | workload | Code source fields. |
| `dataset` / `model_storage_info` / `model_storage_info_items` | workload | Dataset and model-storage mounts. |
| `selected_nodes` | workload / resource | Pin the job to specific nodes. |
| `queue_name` | `resource_config` | Submission queue, resolved via the platform API; `local` executes on the current machine; `auto` picks a queue from free cards. |
| `queue_category` | `resource_config` | `dedicated` (default), `public`, or `idle`; public/idle use shared queues. With `queue_name: auto`, a preference-ordered list. |
| `project_set_name` / `project_name` | resource / experiment | Project path used to resolve the queue. |
| `region_name` / `zone_name` | resource / experiment | Region and availability-zone names used to resolve the queue. |
| `priority` | `resource_config` | Common values are `low`, `medium`, and `high`. |
| `accelerator_model` | resource / role | Accelerator type supplied by the platform. With `queue_name: auto`, a list of acceptable models (resource only). |
| `accelerator_count` | resource / role | Accelerator count per replica. With `queue_name: auto`, a list paired by position with `accelerator_model`, or one count for all (resource only). |
| `cpu_cores` | resource / role | CPU cores per replica. |
| `mem_gib` | resource / role | Memory in GiB per replica. |
| `shared_mem_gib` | resource / role | Shared memory in GiB per replica. |
| `rdma_shared_count` / `accelerator_resource_name` / `fb_mem_gib` / `accelerator_mem_gib` / `cpu_model` | resource / role | Further per-replica resource request fields. |
| `resource_region` | resource / role | Resource region; the `resource_config` value applies to the master and is inherited by roles. |
| `pod_restart_policy` / `restart_scope` | `resource_config` | Pod restart behaviour. |
| `roles` | `resource_config` | Node roles: list of `{name, replicas, <resource fields>, resource_region, extra_label_info, platform_overrides}`. Omit for single-node. |
| `extra_label_info` | role | Extra platform labels for the role. |
| `run_options.duration_hours` | `job` / `job_template` | Estimated runtime for public queue submission. |
| `run_options.queue_priority_mode` | `job` / `job_template` | Public queue mode: `normal` or `high`. |
| `run_options.usage_desc` | `job` / `job_template` | Public queue usage description. |
| `run_options.enable_checkpoint` | `job` / `job_template` | Enable public queue checkpointing. |
| `platform_overrides` | experiment / job / resource / role | Raw passthrough for newer platform fields; the only way to pass unmodelled job fields. |
| `name` / `params` / `sampling` / `max_trials` | `search` | Search name, `{name, values}` grid, `grid`, and the combination cap (default 9999). |
| `parallel_trials` / `poll_interval` | `search` | Concurrency limit and scheduler poll interval in seconds (default 30). |

## Variable reference

| Syntax | Expansion |
|---|---|
| `$CONFIG_DIR` | Directory containing the YAML file. |
| `$VAR` | Local environment variable, resolved before YAML parsing. |
| `$$VAR` | Literal remote `$VAR` reference after jrun unescapes it. |
| `{param}` | Current grid value in job names, commands, and environment values. |
| `{auto:Ns}` | Deterministic `N`-character MD5-derived suffix for job names. |

Remote job environment (verified on a real single-node job; reference as
`$$VAR`):

| Variable | Source | Meaning |
|---|---|---|
| `AIRS_WORKLOAD_TYPE` / `AIRS_EXPERIMENT_ID` | platform | `experiment` / experiment UUID |
| `HOSTNAME` | platform | `job-<job UUID>-master-0` (only source of the job ID) |
| `MASTER_ADDR` / `MASTER_PORT` / `RANK` / `WORLD_SIZE` | platform | Rendezvous; single node: rank `0`, world size `1` |
| `PET_NNODES` / `PET_NODE_RANK` / `PET_NPROC_PER_NODE` / `PET_WORLD_SIZE` / `PET_MASTER_ADDR` / `PET_MASTER_PORT` | platform | `torchrun` defaults; `PET_NPROC_PER_NODE=auto` |
| `NVIDIA_VISIBLE_DEVICES` | platform | Visible GPUs |
| keys under `envs:` | jrun | Set as environment variables |
| `JRUN_QUEUE_NAME` / `JRUN_ACCELERATOR_MODEL` / `JRUN_ACCELERATOR_COUNT` | jrun | `queue_name: auto` jobs only |

Image variables (`CUDA_VERSION`, `HOME`, ...) depend on the image.

Comment-only lines are not environment-expanded. An undefined `$VAR` in
active YAML causes configuration loading to fail.

## Safe preview checklist

1. Run `jrun submit <config> --dry-run`.
2. Check experiment, image, queue, priority, accelerator count, CPU, memory,
   shared memory, roles and replicas, and total expanded jobs.
3. Check every rendered command and only the names, not secret values, of
   environment variables.
4. For `queue_name: auto`, check the placement table: queue, model x count,
   and how many jobs start now, queue, or wait for a public/idle slot.
5. Confirm that no placeholder text remains.
6. Obtain explicit user approval immediately before any actual submit,
   overwrite, stop, removal, experiment creation, or local execution.
