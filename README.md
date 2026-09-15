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

- **Project** — the current directory's `.jrun/` folder. It tracks every submission made from this directory (jobs, searches, settings) in `.jrun/jobs.json`. `jrun init` creates it; all other commands find it by walking up from the working directory.
- **Experiment** — an experiment on the jiuding platform (what `airsctl experiment` operates on). A config's `experiment:` block defines it; jrun creates it if missing. Managed via `jrun experiment create / list / jobs / delete`.
- **Job** — a single run submitted to the platform under an experiment. Each grid-search trial is one job. Managed via `jrun job status / stop / remove`.

The command layout mirrors this: `jrun submit` creates jobs, `jrun job ...` manages tracked jobs, `jrun experiment ...` manages platform experiments, and everything is recorded per project.

## Quick Start

### 1. Initialize & Log In

```bash
jrun init       # create .jrun/ in the current directory
jrun login      # store platform AK/SK in ~/.jrun/auth.json (needed for experiment creation and queue queries)
```

`jrun init` only creates the `.jrun/` directory. All `jrun` commands locate it by walking up from the current working directory. `jrun login` exchanges your AK/SK for a token and verifies it. Credentials resolve in order: `AIRS_AK`/`AIRS_SK` env vars → `~/.jrun/auth.json` → `/etc/accesskey/{user-ak,user-sk}` (base64-encoded, automatically mounted on jiuding platform pods) → airsctl's cached token (`/tmp/.airs/.token`). On platform pods, credentials are picked up automatically, so login is optional there.

### 2. Write a Config

Use `jrun template` to generate a fully annotated config template:

```bash
jrun template                    # print to stdout
jrun template -f config/my.yaml  # write to file
```

**Grid search** (`config/search.yaml`):

```yaml
description: Evaluate models on multiple datasets

# optional: the experiment to submit into (an experiment is just a container
# for job configs). jrun looks it up by name and creates it via the platform
# API if missing, using the job's image/queue for the placeholder config.
experiment:
  name: my_experiment

search:
  name: my_eval
  # experiment_id: "custom-uuid"    # optional: submit to an existing experiment by ID
  # experiment_name: other_exp      # optional
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
job:
  name: eval_llama3_news
  # experiment_id: "custom-uuid"    # optional: submit to an existing experiment by ID
  # experiment_name: other_exp      # optional
  commands:
    - "python evaluate.py --model llama3_8b --data news"
  envs:
    HOME: /home/user
  resource_config:
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
description: Multi-node training - 1 master + N workers
job:
  name: train_multinode
  commands:
    - cd /workspace
    - torchrun --nproc_per_node=1 --nnodes=2 train.py
  envs:
    NCCL_DEBUG: INFO
    MASTER_PORT: "29500"
  resource_config:
    queue_name: my-queue
    priority: high
    accelerator_model: J1_ADV_H1-SXM4-80GB
    accelerator_count: 1
    cpu_cores: 8
    mem_gib: 32
    shared_mem_gib: 16
    worker:
      replicas: 1
```

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

When `queue_name` is `local`, the command runs directly on your machine instead of submitting to the platform.

### 3. Submit Jobs

```bash
# Preview without submitting
jrun submit config/search.yaml --dry-run

# Submit
jrun submit config/search.yaml

# Auto-overwrite existing jobs
jrun submit config/search.yaml --overwrite

# Only overwrite jobs with specific status
jrun submit config/search.yaml -o -s Failed -s Stopped
```

When a job name already exists in the tracker, jrun prompts to overwrite or skip. Use `--overwrite` (`-o`) to auto-overwrite, optionally filtered by `--status` (`-s`).

Every non-dry-run submission treats the target experiment as the current
working set. jrun expands the YAML, applies the new/overwrite/skip decisions,
then makes one full `airsctl experiment modify` call containing exactly the
selected jobs. Skipped jobs and configs from an earlier submission are left
out of that list. The first existing config supplies the platform-specific
structure; it is not appended to the new list.

The modify is followed by one run-only call per selected config. An overwrite
does not stop or cancel the old platform job. After the replacement starts,
`--overwrite` updates only the local name-to-ID mapping in
`.jrun/jobs.json`; if modify or run fails, the old local mapping is retained.
That file is the project's source of job history and status, while experiment
configs describe only the latest submission. A shared experiment will be
fully replaced on every submit, so use a dedicated experiment unless that is
intentional. With `parallel_trials`, all selected configs are installed by
the initial modify and the background scheduler only runs pending configs.

If the config has an `experiment:` block and the experiment doesn't exist yet, jrun asks to create it (skip the prompt with `-y`). To create the experiment without submitting anything:

```bash
jrun experiment create config/search.yaml
```

### 4. Inspect Cluster Capacity

```bash
# Free GPUs per queue, grouped by cluster (helps pick where to submit)
jrun queues
jrun queues --ids      # also show queue/cluster/zone IDs

# GPU utilization of all jobs in the cluster (requires an admin token)
jrun gpu-usage
jrun gpu-usage -s Running -s Pending   # filter by status (repeatable, default: Running)
jrun gpu-usage --all                   # any status
jrun gpu-usage --include-cpu           # include CPU-only jobs (default: GPU jobs only)
jrun gpu-usage --sort gpus             # sort by GPU count (time/gpus/gpu/gmem/cpu/mem/owner)
jrun gpu-usage --owner my-workspace -n 20
```

### 5. Check Status

```bash
# All tracked jobs
jrun job status

# Filter by search name
jrun job status my_eval

# Filter by job name or ID
jrun job status <job-name-or-id>

# Only show jobs with specific status
jrun job status -s Running
jrun job status my_eval -s Failed -s Stopped
```

Status output includes colored status indicators and clickable platform links (when platform IDs are available).

### 6. Manage Jobs

```bash
# Stop a running job
jrun job stop <job-name-or-id>

# Remove a job or entire search (stops, deletes config, removes from tracker)
jrun job remove <job-name-or-id>
jrun job remove <search-name>

# List all experiments on the platform
jrun experiment list

# List platform-side jobs under an experiment (by name or ID, -s filters status).
# Unlike `jrun job status`, this also shows jobs not tracked by this project.
jrun experiment jobs <experiment-name-or-id>

# Delete (archive) an experiment; -y skips the confirmation prompt
jrun experiment delete <experiment-name-or-id>
```

## Config Variables

| Variable | Expands to |
|---|---|
| `$CONFIG_DIR` | Directory containing the config file (jrun built-in) |
| `$VAR` | Resolved at submit time from local environment variables. Errors if undefined |
| `$$VAR` | Becomes `$VAR` in the submitted command (remote env reference) |
| `{param}` | Parameter value from search grid |
| `{auto:Ns}` | Deterministic N-char MD5 hash based on parameters |

## Config Fields Reference

| Field | Location | Description |
|---|---|---|
| `description` | top-level | Human-readable description (not used by jrun) |
| `experiment` | top-level | Experiment definition: jrun looks it up by name and creates it if missing |
| `experiment.name` | nested | Experiment name (required) |
| `experiment.cluster_id` / `zone_id` | nested | Optional explicit IDs, override queue resolution |
| `experiment.proj_id` / `projset_id` | nested | Optional explicit project IDs, skip API resolution |
| `experiment.image` / `image_region` / `queue_name` | nested | Deprecated: job-level fallbacks; prefer the job fields |
| `image` | job / job_template | Docker image the job runs with (supports `{param}` in search) |
| `image_region` | job / job_template | Image repo type: `PUBLIC` (default) or `PRIVATE` |
| `name` | job / job_template | Job name (supports `{param}` and `{auto:Ns}`) |
| `commands` | job / job_template | List of commands (joined with `&&`) |
| `envs` | job / job_template | Environment variables (supports `{param}` and `$$`) |
| `resource_config` | job / job_template | Resource allocation |
| `resource_config.queue_name` | nested | Queue to submit into (resolved via API; `local` runs locally) |
| `resource_config.priority` | nested | `low`, `medium`, `high` |
| `resource_config.accelerator_model` | nested | GPU model |
| `resource_config.accelerator_count` | nested | Number of GPUs |
| `resource_config.cpu_cores` | nested | CPU cores |
| `resource_config.mem_gib` | nested | Memory in GiB |
| `resource_config.shared_mem_gib` | nested | Shared memory in GiB |
| `resource_config.worker` | nested | Multi-node worker config (`replicas`, resource overrides) |
| `search.name` | search | Search name (optional, auto-derived) |
| `search.experiment_id` | search | Submit to an existing experiment by ID |
| `search.experiment_name` | search | Submit to an existing experiment by name |
| `search.params` | search | List of `{name, values}` for grid expansion |
| `search.sampling` | search | `grid` |
| `search.max_trials` | search | Max number of parameter combinations |
| `search.parallel_trials` | search | Max concurrent jobs; pending configs are run by a scheduler |
| `job.experiment_id` | job | Submit to an existing experiment by ID |
| `job.experiment_name` | job | Submit to an existing experiment by name |

## Project Structure

```
your-project/
├── .jrun/
│   ├── settings.json    # Project settings (experiment defaults, platform IDs)
│   └── jobs.json        # Submitted job tracking
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
