# jrun

A CLI tool for submitting and managing jobs on the airsctl platform. Supports grid search parameter expansion, multi-node jobs, local debugging, and job tracking.

[中文文档](README_zh.md)

## Installation

```bash
# Install with uv (recommended)
uv tool install .

# Or install in development mode
uv sync
uv run jrun --help
```

> **Note:** Unset `http_proxy` and `https_proxy` before running jrun, or airsctl subprocess calls will fail.

## Quick Start

### 1. Initialize

```bash
jrun init -N <experiment-name>
# or with experiment ID
jrun init -N <experiment-name> -e <experiment-id>
```

Creates `.jrun/settings.json` with experiment info and platform IDs (auto-extracted for status links). All `jrun` commands locate this directory by walking up from the current working directory.

### 2. Write a Config

Use `jrun template` to generate a fully annotated config template:

```bash
jrun template                    # print to stdout
jrun template -f config/my.yaml  # write to file
```

**Grid search** (`config/search.yaml`):

```yaml
description: Evaluate models on multiple datasets
search:
  name: my_eval
  job_template:
    name: eval_{model}_{data}_{auto:4s}
    commands:
      - "python evaluate.py --model {model} --data {data}"
    envs:
      CUDA_LAUNCH_BLOCKING: "1"
      MODEL_NAME: "{model}"
    resource_config:
      queue_name: my-queue
      priority: high
      accelerator_model: J1_ADV_H1-SXM4-80GB
      accelerator_count: 1
      cpu_cores: 8
      mem_gib: 128
      shared_mem_gib: 64
  sampling: grid
  max_trials: 100
  parallel_trials: 4    # TODO: not yet implemented
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

### 4. Check Status

```bash
# All tracked jobs
jrun status

# Filter by search name
jrun status my_eval

# Filter by job name or ID
jrun status <job-name-or-id>

# Only show jobs with specific status
jrun status -s Running
jrun status my_eval -s Failed -s Stopped
```

Status output includes colored status indicators and clickable platform links (when platform IDs are available).

### 5. Manage Jobs

```bash
# Stop a running job
jrun stop <job-name-or-id>

# Remove a job or entire search (stops, deletes config, removes from tracker)
jrun remove <job-name-or-id>
jrun remove <search-name>

# List all experiments
jrun list
```

## Config Variables

| Variable | Expands to |
|---|---|
| `$CONFIG_DIR` | Directory containing the config file |
| `$HOME` | User home directory |
| `$$VAR` | Literal `$VAR` (for runtime environment variables) |
| `{param}` | Parameter value from search grid |
| `{auto:Ns}` | Deterministic N-char MD5 hash based on parameters |

## Config Fields Reference

| Field | Location | Description |
|---|---|---|
| `description` | top-level | Human-readable description (not used by jrun) |
| `name` | job / job_template | Job name (supports `{param}` and `{auto:Ns}`) |
| `commands` | job / job_template | List of commands (joined with `&&`) |
| `envs` | job / job_template | Environment variables (supports `{param}` and `$$`) |
| `resource_config` | job / job_template | Resource allocation |
| `resource_config.queue_name` | nested | Queue name (`local` for local execution) |
| `resource_config.priority` | nested | `low`, `medium`, `high` |
| `resource_config.accelerator_model` | nested | GPU model |
| `resource_config.accelerator_count` | nested | Number of GPUs |
| `resource_config.cpu_cores` | nested | CPU cores |
| `resource_config.mem_gib` | nested | Memory in GiB |
| `resource_config.shared_mem_gib` | nested | Shared memory in GiB |
| `resource_config.worker` | nested | Multi-node worker config (`replicas`, resource overrides) |
| `search.name` | search | Search name (optional, auto-derived) |
| `search.params` | search | List of `{name, values}` for grid expansion |
| `search.sampling` | search | `grid` |
| `search.max_trials` | search | Max number of parameter combinations |
| `search.parallel_trials` | search | Max concurrent jobs (TODO: not yet implemented) |

## Project Structure

```
your-project/
├── .jrun/
│   ├── settings.json    # Experiment name/ID/platform IDs
│   └── jobs.json        # Submitted job tracking
└── config/
    └── search.yaml      # Job config files
```
