# jrun

A CLI tool for submitting and managing jobs on the airsctl platform. Supports grid search parameter expansion, multi-node jobs, local debugging, and job tracking.

[中文文档](README_zh.md)

## Installation

```bash
# Install directly from GitHub with uv (recommended)
uv tool install git+https://github.com/AISafetyHub/jrun.git
```

Upgrade to the latest version:

```bash
uv tool upgrade jrun
```

Run once without installing:

```bash
uvx --from git+https://github.com/AISafetyHub/jrun.git jrun --help
```

For development:

```bash
git clone https://github.com/AISafetyHub/jrun.git
cd jrun
uv sync
uv run jrun --help
```

> **Note:** Unset `http_proxy` and `https_proxy` before running jrun, or airsctl subprocess calls will fail.

## Concepts

jrun organizes work in three layers:

- **Project** — the current directory's `.jrun/` folder. It tracks every submission made from this directory (jobs and searches) in `.jrun/jobs.json`. `jrun init` creates it; all other commands find it by walking up from the working directory.
- **Experiment** — an experiment on the jiuding platform (what `airsctl experiment` operates on). A config's `experiment:` block names it; jrun creates it if missing. Managed via `jrun experiment create / list / jobs / delete`.
- **Job** — a single run submitted to the platform under an experiment. Each grid-search trial is one job. Managed via `jrun job status / stop / remove`.

The command layout mirrors this: `jrun submit` creates jobs, `jrun job ...` manages tracked jobs, `jrun experiment ...` manages platform experiments, and everything is recorded per project.

## Quick Start

### 1. Initialize & Log In

```bash
jrun init                     # create .jrun/ in the current directory
jrun login                    # store platform AK/SK in ~/.jrun/auth.json (prompts for both)
jrun login --ak <AK> --sk <SK>  # non-interactive
jrun whoami                   # account, identity type, and joined project sets (--json available)
```

`jrun init` only creates the `.jrun/` directory (its `settings.json` is an empty marker; it binds no experiment). All `jrun` commands locate it by walking up from the current working directory. `jrun login` exchanges your AK/SK for a token and verifies it; the REST API is needed for experiment creation, queue resolution, and queue queries. Credentials resolve in order: a ready token in `AIRS_TOKEN` → `AIRS_AK`/`AIRS_SK` env vars → `~/.jrun/auth.json` → `/etc/accesskey/{user-ak,user-sk}` (base64-encoded, automatically mounted on jiuding platform pods) → airsctl's cached token (`/tmp/.airs/.token`). On platform pods, credentials are picked up automatically, so login is optional there.

### 2. Write a Config

Use `jrun template` to generate a fully annotated config template:

```bash
jrun template                    # print to stdout
jrun template -f config/my.yaml  # write to file
```

Every config that submits to the platform needs a top-level `experiment:` block with at least `name:`. jrun looks the experiment up by exact name and creates it via the platform API if missing (using the first job's image and queue for the placeholder config). The block can also carry defaults for jobs (`image`, `image_region`, `queue_name`, `queue_category`, `project_set_name`, `project_name`, `region_name`, `zone_name`) and experiment-level settings (`description`, `train_frame`, `restart_policy`, `mounts`, ...). A job-level value always wins over the experiment default. `--dry-run` only warns when the block is missing; a real submit fails.

`mounts` controls the storage mounted into every job of the experiment: `home` (`/home/<user>`), `project` (`/share/project`), and `projset` (`/share/projset`), each `rw`, `ro`, or `false` (not mounted). A directory you leave out is mounted `rw`, and without `mounts` jrun keeps the platform default (all three `rw`). The setting applies when the experiment is created and on every submit, so changing it in the YAML changes later jobs. The old raw `storage_info` list is rejected; a raw list can still go under `platform_overrides`.

```yaml
experiment:
  name: my-exp
  mounts:
    project: false   # don't mount /share/project
    projset: ro      # /share/projset read-only
```

**Grid search** (`config/search.yaml`):

```yaml
description: Evaluate models on multiple datasets

# required: the experiment to submit into. jrun looks it up by name and
# creates it via the platform API if missing.
experiment:
  name: my_experiment
  # image: ...                     # optional default for jobs without image:

search:
  name: my_eval
  job_template:
    name: eval_{model}_{data}_{auto:4s}
    image: harbor.platform.baai-inner.ac.cn/library/pytorch-sshd:23.08
    # image_region: PRIVATE         # optional: PUBLIC (default) or PRIVATE
    commands:
      - "python evaluate.py --model {model} --data {data}"
    envs:
      CUDA_LAUNCH_BLOCKING: "1"
      MODEL_NAME: "{model}"
    resource_config:
      queue_category: dedicated
      project_set_name: baai-safety
      project_name: baai-safety_research
      region_name: 北京-大兴
      zone_name: 可用区A
      queue_name: jiuding_airs1_h100  # see `jrun queues` for options
      priority: high
      accelerator_model: J1_ADV_H1-SXM4-80GB
      accelerator_count: 1
      cpu_cores: 8
      mem_gib: 128
      shared_mem_gib: 64
  sampling: grid
  max_trials: 100
  parallel_trials: 4    # max concurrent jobs; pending jobs use a scheduler
  params:
    - name: model
      values: ["llama3_8b", "gpt35"]
    - name: data
      values: ["news", "paper"]
```

**Single job** (`config/single.yaml`):

```yaml
description: Single evaluation job

experiment:
  name: my_experiment

job:
  name: eval_llama3_news
  image: harbor.platform.baai-inner.ac.cn/library/pytorch-sshd:23.08
  commands:
    - "python evaluate.py --model llama3_8b --data news"
  envs:
    HOME: /home/user
  resource_config:
    queue_category: dedicated
    project_set_name: baai-safety
    project_name: baai-safety_research
    region_name: 北京-大兴
    zone_name: 可用区A
    queue_name: my-queue
    priority: high
    accelerator_model: J1_ADV_H1-SXM4-80GB
    accelerator_count: 1
    cpu_cores: 8
    mem_gib: 128
    shared_mem_gib: 64
```

**Multi-node job** (`config/multinode.yaml`):

```yaml
description: Multi-node training - 1 master + 3 workers

experiment:
  name: my_experiment

job:
  name: train_multinode
  image: harbor.platform.baai-inner.ac.cn/library/pytorch-sshd:23.08
  commands:
    - cd /workspace
    - torchrun --nproc_per_node=1 --nnodes=4 train.py
  envs:
    NCCL_DEBUG: INFO
    MASTER_PORT: "29500"
  resource_config:
    queue_category: dedicated
    project_set_name: baai-safety
    project_name: baai-safety_research
    region_name: 北京-大兴
    zone_name: 可用区A
    queue_name: my-queue
    priority: high
    accelerator_model: J1_ADV_H1-SXM4-80GB
    accelerator_count: 1
    cpu_cores: 8
    mem_gib: 32
    shared_mem_gib: 16
    roles:
      - name: Master
        replicas: 1
      - name: Worker
        replicas: 3
```

Each entry in `roles` is one platform node role (`name`, `replicas`, resource fields, `resource_region`, `extra_label_info`, `platform_overrides`). A role inherits the resource fields of the enclosing `resource_config` (`accelerator_model`, `accelerator_count`, `cpu_cores`, `mem_gib`, `shared_mem_gib`, `rdma_shared_count`, `accelerator_resource_name`, `fb_mem_gib`, `accelerator_mem_gib`, `cpu_model`) and its `resource_region`; values set on the role override them. Without `roles` the job is single-node (one Master), and `resource_config.resource_region` applies to that master role. The old `resource_config.worker` field is removed and rejected.

**Local debug** (`config/local.yaml`):

```yaml
description: Run locally for testing
job:
  name: test_local
  commands:
    - echo "Hello from local"
  resource_config:
    queue_name: local
```

When `queue_name` is `local`, the command runs directly on your machine instead of submitting to the platform (no experiment block or credentials needed), and `jrun submit` exits with the command's exit code. For a local `search:`, only the first expanded job runs.

`commands:` list items are joined with newlines, one shell line each; they are not fail-fast, so start with `set -e` (or chain with `&&` in a single line) when later lines must not run after a failure. `command:` takes a single string.

Queue names may repeat across projects or regions. Use the full name path when
needed: `project_set_name` → `project_name` → `region_name` → `zone_name` →
`queue_name`. jrun resolves this path to platform IDs automatically.
`queue_category` is `dedicated`, `public`, or `idle`; it defaults to
`dedicated` when omitted.
Do not copy platform UUIDs into the YAML; the old ID/display-name fields are
removed and are rejected with a configuration error.

Shared public and idle queues use the same queue names shown by `jrun queues`:

```yaml
resource_config:
  queue_category: public       # dedicated, public, or idle
  project_set_name: baai-safety
  project_name: baai-safety_research
  region_name: 北京-大兴
  zone_name: 可用区A
  queue_name: Public-Pool-Nvidia-A100
  accelerator_model: NVIDIA_A100-SXM4-40GB
  accelerator_count: 1
run_options:
  duration_hours: 12
  queue_priority_mode: normal
  usage_desc: evaluation benchmark
```

`run_options` sits at job level (next to `resource_config`) and accepts only `duration_hours`, `queue_priority_mode` (`normal` or `high`), `usage_desc`, and `enable_checkpoint`; unknown keys are rejected. Public jobs require `duration_hours`, `queue_priority_mode`, and `usage_desc`; idle jobs use the same resource fields without public-queue approval metadata. Any `run_options` that are set are passed to `airsctl job run`.

Use `queue_category: idle` for reclaimable off-peak capacity. Shared queues fix
`priority`: public only accepts `high` and idle only `low`. Leave `priority`
unset there: jrun writes the required value and rejects a conflicting one.
Dedicated queues use `queue_category: dedicated` (the default, any priority):

```yaml
resource_config:
  queue_category: idle       # or dedicated
  project_set_name: baai-safety
  project_name: baai-safety_research
  region_name: 北京-大兴
  zone_name: 可用区A
  queue_name: Public-Pool-Nvidia-A100
```

**Automatic queue placement** (`examples/auto.yaml`). Free cards are often spread over several queues and accelerator models. Set `queue_name: auto` and list the acceptable `(accelerator_model, accelerator_count)` pairs instead of pinning one queue:

```yaml
resource_config:
  queue_name: auto
  queue_category: [public, idle]      # preference order; a single value also works
  project_set_name: baai-safety       # location names filter the candidate queues
  project_name: baai-safety_research
  accelerator_model: [NVIDIA_A100-SXM4-40GB, NVIDIA_V100-PCIE-32GB]
  accelerator_count: [4, 8]           # paired by position; a scalar applies to every model
  cpu_cores: 32                       # shared by every pair: size for the largest one
  mem_gib: 256
run_options:                          # required because public is allowed
  duration_hours: 12
  queue_priority_mode: normal
  usage_desc: evaluation benchmark
```

On submit, jrun reads every candidate queue's free cards and the project set's remaining public/idle submit slots (the per-project-set limit minus running and queueing jobs, the RUN/LIMIT column of `jrun queues`), then places the jobs being submitted in order:

1. **Starts now**: the first candidate, by category preference, then pair preference, then most free cards, that has enough free cards (`accelerator_count` × total role replicas), no jobs waiting ahead of it (see below), and, for public/idle, a remaining submit slot.
2. **Queued on the platform**: a job that fits nowhere goes to a dedicated queue if `dedicated` is listed (dedicated queues accept queued submissions), otherwise to a public/idle queue that still has a submit slot. Among those, the queue with the fewest jobs waiting ahead wins, then the one with the most free cards.
3. **Waits for a slot**: with no public/idle slot left, the job's config is installed on its preferred queue and the background scheduler runs it once a slot frees up (with or without `parallel_trials`). A single `job:` that would have to wait fails with an error instead.

**FIFO queues.** A queue can show free cards while jobs already wait in it (e.g. a waiting job needs more cards than are free). Under first-in-first-out scheduling nothing behind that job starts, so jrun does not count such a queue as "starts now". Dedicated queues count as FIFO unless their schedule strategy is Traverse (Smart is treated as FIFO). Shared pools are always FIFO: a public job (high priority) waits behind waiting public jobs, an idle job (low priority) behind waiting public and idle jobs, counted across all project sets. If the waiting count cannot be read, jrun falls back to free cards only. The WAITING column of `jrun queues` shows these counts, and the placement preview prints `N waiting ahead`.

Public and idle are the same physical pools, so free cards are counted once while submit slots are counted per mode. Model names match exactly (case-insensitive); queues whose total capacity is below the job's cards are never chosen. Each placed job becomes an ordinary job on the chosen queue and gets `JRUN_QUEUE_NAME`, `JRUN_ACCELERATOR_MODEL`, and `JRUN_ACCELERATOR_COUNT` environment variables, so commands can adapt (e.g. `echo $$JRUN_ACCELERATOR_MODEL`); a plain `torchrun` without `--nproc_per_node` already starts one process per visible GPU (see [Remote Job Environment Variables](#remote-job-environment-variables)). `--dry-run` previews the placement with read-only platform queries; the real submit places again with the capacity at that moment. Lists in `accelerator_model`/`accelerator_count`/`queue_category` require `queue_name: auto` (which can also be set on the `experiment:` block), and roles must not override the accelerator fields of an auto job. The queue of a waiting job is fixed at submit time.

Unknown keys in a job (`job:` or `search.job_template:`) are rejected as typos. Put raw platform fields that jrun does not model under `platform_overrides:` (available on the job, `resource_config`, each role, and `experiment`; unknown keys inside `resource_config`, roles, and `experiment` are also passed through as overrides). Configuration errors (undefined `$VAR`, invalid YAML, removed fields such as `worker`, `submission`, or `experiment_id`, a bad `queue_category`, mismatched accelerator lists) print a single `Error: ...` line and exit 1.

### 3. Submit Jobs

```bash
# Preview without submitting
jrun submit config/search.yaml --dry-run

# Submit
jrun submit config/search.yaml

# Create the experiment without the confirmation prompt
jrun submit config/search.yaml -y

# Auto-overwrite existing jobs
jrun submit config/search.yaml --overwrite

# Only overwrite jobs with specific status
jrun submit config/search.yaml -o -s Failed -s Stopped

# Only submit some jobs of a grid (repeatable)
jrun submit config/search.yaml --only my_eval_lr0.01

# Machine-readable result on stdout (no prompts; messages go to stderr)
jrun submit config/search.yaml --dry-run --json
```

When a job name already exists in the tracker, jrun prompts to overwrite or skip. Use `--overwrite` (`-o`) to auto-overwrite, optionally filtered by `--status` (`-s`).

Overwriting stops the old run first: before the replacement is installed, jrun stops the old platform job if it has not finished (scheduler-held `queued-*` entries are only marked `Stopped` locally; platform jobs that are still `Pending` or `Approving` are stopped like running ones; finished jobs are left alone). In a grid search, if one old job cannot be stopped, that job alone is skipped with an error and the rest proceed; for a single job, a stop failure aborts the submit. The old local name-to-ID mapping is removed only after the replacement run has started; if modify or run fails, it is retained.

Every non-dry-run submission treats the target experiment as the current
working set. jrun expands the YAML, applies the new/overwrite/skip decisions,
then makes one full `airsctl experiment modify` call containing exactly the
selected jobs. Skipped jobs and configs from an earlier submission are left
out of that list. The first existing config supplies the platform-specific
structure; it is not appended to the new list. The modify is followed by one
run-only `airsctl job run` call per selected config; a run failure (including
output without a job ID) is reported per job while the other jobs proceed.

`.jrun/jobs.json` is the project's source of job history and status, while
experiment configs describe only the latest submission. A shared experiment
will be fully replaced on every submit, so use a dedicated experiment unless
that is intentional. With `parallel_trials`, all selected configs are
installed by the initial modify and the background scheduler only runs
pending configs. If the experiment already has 100 or more configs, jrun
warns and asks before continuing.

#### Submitting public jobs through a remote service

If your account may submit to dedicated and idle queues but not to the public pool, a jrun remote-submission service (see [Remote Submission Service](#remote-submission-service)) can submit the public jobs for you. Opt in per config with a top-level `remote:` block:

```yaml
remote:
  url: https://jrun-svc.example.com   # or $JRUN_REMOTE_URL
  categories: [public]                # default: only public jobs go remote
```

After auto placement, each job whose final queue category is listed goes to the service, and every other job is submitted locally as usual. `--dry-run` shows a `Route:` line per job. Jobs sent to the service need `project_set_name`. The service verifies your identity with your own credentials, and checks that you have joined the project set and can see the target pool (as idle). It then submits under its own account into the experiment `<your alias>-<name>`, with `mounts.home` off. Those jobs are tracked locally with the service URL, so `jrun job status / stop / remove` reach them through the service. See `examples/remote-public.yaml` and `docs/remote-service.md`.

If the experiment doesn't exist yet, jrun asks to create it (skip the prompt with `-y`). To create the experiment without submitting anything:

```bash
jrun experiment create config/search.yaml
```

### 4. Inspect Cluster Capacity

```bash
# Free GPUs per dedicated, public, and idle queue, grouped by cluster
jrun queues
jrun queues --category public                     # dedicated / public / idle (repeatable)
jrun queues --category public --category idle
jrun queues --ids                                 # also show queue/cluster/zone IDs
jrun queues --projset-id <ID> --proj-id <ID>      # scope to one project set / project

# GPU utilization of all jobs in the cluster (requires an admin token)
jrun gpu-usage
jrun gpu-usage -s Running -s Pending   # filter by status (repeatable, default: Running)
jrun gpu-usage --all                   # any status
jrun gpu-usage --include-cpu           # include CPU-only jobs (default: GPU jobs only)
jrun gpu-usage --sort gpus             # sort by GPU count (time/gpus/gpu/gmem/cpu/mem/owner)
jrun gpu-usage --sort gpu --asc        # ascending GPU utilization (default: descending)
jrun gpu-usage --cluster-id <ID> --zone-id <ID>   # filter by cluster / zone (repeatable)
jrun gpu-usage --owner my-workspace -n 20         # filter by owner, show 20 rows (default 50)
```

Public and idle are two submission modes on the same shared pools, so `jrun queues` lists each shared pool once under `public` and once under `idle`, each with its own RUN/LIMIT (running jobs vs. per-project-set limit). WAITING is the number of jobs waiting ahead of a new one: for dedicated queues, Pending/Scheduling jobs in the queue (`-` for Traverse queues, where waiting jobs don't block); for shared pools, waiting jobs across all project sets (public: public jobs; idle: public + idle jobs). A queue with free cards and WAITING > 0 will not start a new job right away. `--proj-id` / `--projset-id` (also on `gpu-usage`, `experiment jobs`, and `experiment delete`) set the project / project-set headers; by default no project is sent and the project set is auto-detected from the account's joined project sets.

### 5. Check Status

```bash
# Tracked jobs and searches (10 entries by default)
jrun job status
jrun job status -n 50      # show up to 50 entries
jrun job status -n 0       # show all

# Filter by search name
jrun job status my_eval

# Filter by job name or ID
jrun job status <job-name-or-id>

# Only show jobs with specific status
jrun job status -s Running
jrun job status my_eval -s Failed -s Stopped

# Refreshed jobs as JSON (all matches)
jrun job status my_eval --json
```

Status output includes colored status indicators and clickable platform links (when platform IDs are available). Jobs submitted through a remote service are refreshed from that service (one call per service).

`jrun queues`, `jrun job stop`, and `jrun job remove` accept `--json` too; the JSON shapes are documented in `docs/remote-service.md`.

### 6. Manage Jobs

```bash
# Stop a running job
jrun job stop <job-name-or-id>

# Stop every stoppable job in a search, and its scheduler
jrun job stop <search-name>

# Stop all tracked jobs with a given status (only stoppable statuses are stopped)
jrun job stop -s Running -s Queued

# Remove a job or entire search (stops, archives, deletes config, removes from tracker)
jrun job remove <job-name-or-id>
jrun job remove <search-name>
jrun job remove <search-name> -s Failed   # only the search's jobs with this status

# List all experiments on the platform
jrun experiment list

# List platform-side jobs under an experiment (by name or ID, -s filters status).
# Unlike `jrun job status`, this also shows jobs not tracked by this project.
jrun experiment jobs <experiment-name-or-id>

# Delete (archive) an experiment; -y skips the confirmation prompt
jrun experiment delete <experiment-name-or-id>
```

`jrun job remove` first stops every job that is still active (Approving/Pending/Running) and waits up to 60s for it to end, because `airsctl job cancel` only archives jobs that have already ended. It then archives (cancels) each job, removes its config from the job's own experiment, and drops it from `.jrun/jobs.json`. A job that is still active after the wait stays tracked and is reported; run `jrun job remove` again once it has stopped (a search is kept until all its jobs are removed). Experiment names are matched exactly; a UUID is looked up as an ID.

## Config Variables

| Variable | Expands to |
|---|---|
| `$CONFIG_DIR` | Directory containing the config file (jrun built-in) |
| `$VAR` | Resolved at submit time from local environment variables. Errors if undefined (comment-only lines are not expanded) |
| `$$VAR` | Becomes `$VAR` in the submitted command (remote env reference) |
| `{param}` | Parameter value from search grid |
| `{auto:Ns}` | Deterministic N-char MD5 hash based on parameters |

Jobs placed by `queue_name: auto` also get `JRUN_QUEUE_NAME`, `JRUN_ACCELERATOR_MODEL`, and `JRUN_ACCELERATOR_COUNT` in `envs` (reference them as `$$JRUN_ACCELERATOR_COUNT`).

## Remote Job Environment Variables

Variables a Jiuding experiment job sees at runtime (verified on 2026-11-24 with a single-node probe job; reference them in commands as `$$VAR`):

| Variable | Source | Value |
|---|---|---|
| `AIRS_WORKLOAD_TYPE` | platform | `experiment` for jobs (`workspace` in a dev workspace) |
| `AIRS_EXPERIMENT_ID` | platform | The experiment's UUID (`AIRS_EXPERIMENT` is `job`) |
| `HOSTNAME` | platform | `job-<job UUID>-master-0`; the only place the job ID appears |
| `MASTER_ADDR` / `MASTER_PORT` | platform | Master pod host name / `23456` |
| `RANK` / `WORLD_SIZE` | platform | `0` / `1` on a single node |
| `PET_NNODES` / `PET_NODE_RANK` / `PET_WORLD_SIZE` / `PET_MASTER_ADDR` / `PET_MASTER_PORT` | platform | Read by `torchrun` as its `--nnodes`/`--node_rank`/... defaults |
| `PET_NPROC_PER_NODE` | platform | `auto`: a plain `torchrun` (no `--nproc_per_node`) starts one process per visible GPU |
| `NVIDIA_VISIBLE_DEVICES` | platform | Visible GPUs |
| `PATH` | platform | Starts with `/client-tools` (contains `airsctl`) |
| your `envs:` | jrun | Every key under `envs:` is set as an environment variable |
| `JRUN_QUEUE_NAME` / `JRUN_ACCELERATOR_MODEL` / `JRUN_ACCELERATOR_COUNT` | jrun | Set on jobs placed by `queue_name: auto` |

The platform provides no job-ID, queue, or accelerator variables, which is why jrun injects `JRUN_*` for auto jobs. Variables such as `CUDA_VERSION`, `HOME`, or `PIP_INDEX_URL` come from the image and change with it. Multi-node values of `RANK`/`PET_*` have not been verified yet.

## Config Fields Reference

| Field | Location | Description |
|---|---|---|
| `description` | top-level | Human-readable description (not used by jrun) |
| `experiment` | top-level | Experiment definition (required to submit): looked up by exact name, created if missing |
| `experiment.name` | experiment | Experiment name (required) |
| `experiment.image` / `image_region` | experiment | Default image and repo type for jobs that omit their own |
| `experiment.project_set_name` / `project_name` | experiment | Default project path for queue resolution |
| `experiment.region_name` / `zone_name` / `queue_name` | experiment | Default region, zone, and queue names |
| `experiment.queue_category` | experiment | Default queue type: `dedicated`, `public`, or `idle` (a preference list with `queue_name: auto`) |
| `experiment.description` | experiment | Description used when jrun creates the experiment |
| `experiment.train_frame` / `restart_policy` / `native_cluster` / `profiler_info` / `model_storage_info` | experiment | Experiment-level settings written to the experiment on submit |
| `experiment.mounts` | experiment | `{home, project, projset}`: `rw` / `ro` / `false`; storage mounted into jobs (default: all `rw`) |
| `name` | job / job_template | Job name (supports `{param}` and `{auto:Ns}`) |
| `image` | job / job_template | Docker image the job runs with (supports `{param}` in search); overrides `experiment.image` |
| `image_region` | job / job_template | Image repo type: `PUBLIC` (default) or `PRIVATE`, case-insensitive |
| `command` | job / job_template | A single command string |
| `commands` | job / job_template | List of commands (joined with newlines) |
| `envs` | job / job_template | Environment variables (supports `{param}` and `$$`) |
| `train_frame` / `restart_policy` | job / job_template | Per-config framework and restart policy |
| `code` / `code_path` / `git_prefix` / `git_branch` / `git_commit` / `gitee` | job / job_template | Code source fields copied into the platform config |
| `dataset` / `model_storage_info` / `model_storage_info_items` | job / job_template | Dataset and model-storage mounts copied into the platform config |
| `selected_nodes` | job / job_template, resource_config | Pin the job to specific nodes |
| `platform_overrides` | experiment / job / resource_config / role | Raw fields for newer Jiuding API versions (the only way to pass unmodelled job fields) |
| `resource_config` | job / job_template | Resource allocation; overrides experiment defaults |
| `resource_config.queue_name` | resource_config | Queue to submit into (resolved via API; `local` runs locally; `auto` picks a queue by free cards and waiting jobs) |
| `resource_config.queue_category` | resource_config | `dedicated` (default), `public`, or `idle` shared queue category; with `queue_name: auto` a preference-ordered list |
| `resource_config.project_set_name` / `project_name` | resource_config | Project path used to resolve the queue |
| `resource_config.region_name` / `zone_name` | resource_config | Region and availability-zone names used to resolve the queue |
| `resource_config.priority` | resource_config | `low`, `medium`, `high` |
| `resource_config.accelerator_model` | resource_config | GPU model; with `queue_name: auto` a list of acceptable models |
| `resource_config.accelerator_count` | resource_config | Number of GPUs per node; with `queue_name: auto` a list paired with `accelerator_model` (or one count for all) |
| `resource_config.cpu_cores` | resource_config | CPU cores per node |
| `resource_config.mem_gib` | resource_config | Memory in GiB per node |
| `resource_config.shared_mem_gib` | resource_config | Shared memory in GiB per node |
| `resource_config.rdma_shared_count` / `accelerator_resource_name` / `fb_mem_gib` / `accelerator_mem_gib` / `cpu_model` | resource_config | Further per-node resource request fields |
| `resource_config.resource_region` | resource_config | Resource region of the master role (inherited by roles) |
| `resource_config.pod_restart_policy` / `restart_scope` | resource_config | Pod restart behaviour |
| `resource_config.roles` | resource_config | Node roles: list of `{name, replicas, <resource fields>, resource_region, extra_label_info, platform_overrides}`; each inherits the resource fields above. Omit for single-node |
| `resource_config.roles[].extra_label_info` | role | Extra platform labels for that role |
| `run_options.duration_hours` | job / job_template | Estimated runtime for public queue jobs |
| `run_options.queue_priority_mode` | job / job_template | Public queue mode: `normal` or `high` |
| `run_options.usage_desc` | job / job_template | Public queue usage description |
| `run_options.enable_checkpoint` | job / job_template | Enable public queue checkpointing |
| `search.name` | search | Search name (optional, auto-derived) |
| `search.job_template` | search | Job definition with `{param}` placeholders |
| `search.params` | search | List of `{name, values}` for grid expansion |
| `search.sampling` | search | `grid` |
| `search.max_trials` | search | Max number of parameter combinations (default 9999) |
| `search.parallel_trials` | search | Max concurrent jobs; pending configs are run by a scheduler |
| `search.poll_interval` | search | Scheduler polling interval in seconds (default 30) |
| `remote.url` | remote | jrun remote-submission service URL; jobs routed there are submitted by the service |
| `remote.categories` | remote | Queue categories routed to the service, judged after auto placement (default `[public]`) |

## Remote Submission Service

`server/` contains `jrun-server`, a separate package with its own Dockerfile and `docker-compose.yml`. It runs with one account's AK/SK, for example a key that may use the public pool. It submits jobs for verified jiuding users by driving the `jrun` CLI (`--json`) in a private workspace per caller.

```bash
cd server
cp /client-tools/airsctl ./airsctl   # from a jiuding pod
cp .env.example .env                 # AIRS_AK, AIRS_SK, JRUN_SERVE_PROJECT_SETS, optional JRUN_SERVE_USERS
docker compose up -d --build
```

Access checks:
- **Identity:** the caller's token must belong to an active account (and match `JRUN_SERVE_USERS` when that is set).
- **Project set:** it must be allowlisted and joined by the caller.
- **Pool:** the caller must see the target pool as idle.
- **Config:** the config is rebuilt from an allowlist (no raw platform overrides, code/dataset/model-storage fields, or private images), and `$` is re-escaped so the service's own environment can never be substituted.

Callers send their 2-hour token, so serve it behind TLS. Details: `docs/remote-service.md`.

jrun itself can also run outside jiuding pods. Set `JRUN_AIRSCTL_GATEWAY=http://platform-multi.baai.ac.cn` and every airsctl call gets `-u <gateway>` plus the project context (`-P/-p`) of the job's queue. See `docs/airsctl.md`.

## Project Structure

```
your-project/
├── .jrun/
│   ├── settings.json    # Empty marker for the project root
│   ├── jobs.json        # Submitted job tracking
│   └── scheduler/       # parallel_trials scheduler PID files and logs
└── config/
    └── search.yaml      # Job config files
```

Credentials for the platform REST API live in `~/.jrun/auth.json` (user-level, written by `jrun login`), not in the project `.jrun/`.

## jrun Agent Skill

This repository includes a self-contained `jrun-workflow` Skill at
`skills/jrun-workflow/`. It teaches Codex and Claude Code to create and review
jrun YAML, run safe dry-runs, submit and monitor single/grid/multi-node jobs,
and manage experiments and tracked jobs. The Skill does not change the jrun
CLI or install jrun; install jrun first using the Installation section above.
All configuration guidance needed at runtime is bundled inside the Skill, so
an installed copy does not depend on this repository or external documents.

### Install for Codex

Copy the canonical Skill directory into the Codex Skill root (the default is
`~/.codex/skills`; replace the destination if your Codex setup uses a custom
`CODEX_HOME`):

```bash
dest="${CODEX_HOME:-$HOME/.codex}/skills/jrun-workflow"
mkdir -p "$dest"
cp -a skills/jrun-workflow/. "$dest/"
```

Codex can use repository-checked-in Skills across its app, CLI, and IDE surfaces; see the [Codex Skill overview](https://openai.com/index/introducing-the-codex-app/).

Use it explicitly with a prompt such as:

```text
Use $jrun-workflow to prepare and preview a grid-search job.
```

### Install for Claude Code

For a project-shared Skill, copy it into the current project's `.claude/skills`
directory:

```bash
dest=".claude/skills/jrun-workflow"
mkdir -p "$dest"
cp -a skills/jrun-workflow/. "$dest/"
```

For a personal Skill available across projects, use
`~/.claude/skills/jrun-workflow` as `dest` instead. Invoke it with
`/jrun-workflow` or describe the jrun task in natural language. Claude Code
supports both project and user Skill directories; see the [Claude Code
configuration directory documentation](https://code.claude.com/docs/en/claude-directory).

Restart the Codex or Claude Code session after installing a new Skill when the
host does not discover newly created Skill directories immediately. When the
Skill changes, repeat the copy command to update the installed copy.
