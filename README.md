# jrun

A CLI tool for submitting and managing jobs on the airsctl platform. Supports grid search parameter expansion and job tracking.

[中文文档](README_zh.md)

## Installation

```bash
# Install with uv (recommended)
uv tool install .

# Or install in development mode
uv sync
uv run jrun --help
```

## Quick Start

### 1. Initialize

Run `jrun init` in your project directory to create a `.jrun/` configuration directory:

```bash
jrun init -N <experiment-name>
# or with experiment ID
jrun init -N <experiment-name> -e <experiment-id>
```

This creates `.jrun/settings.json` with your experiment info. All `jrun` commands look for this directory by walking up from the current working directory.

### 2. Write a Config

**Grid search** (`config/search.yaml`):

```yaml
description: Evaluate models on multiple datasets
environment:
  image: my-image:latest
target:
  service: sing
  name: barlow05
  workspace_name: gcraml-wus2
search:
  job_template:
    name: eval_{model}_{data}_{time}
    sku: G4-V100
    command:
      - "python evaluate.py --model {model} --data {data} --time {time}"
    submit_args:
      env:
        CUDA_LAUNCH_BLOCKING: 1
  sampling: grid
  max_trials: 100
  params:
    - name: model
      values: ["llama3_8b", "gpt35"]
    - name: data
      values: ["news", "paper"]
    - name: time
      values: [1, 2, 3]
```

**Single job** (`config/single.yaml`):

```yaml
description: Single evaluation job
environment:
  image: my-image:latest
target:
  service: sing
  name: barlow05
  workspace_name: gcraml-wus2
job:
  name: eval_llama3_news
  sku: G4-V100
  command:
    - "python evaluate.py --model llama3_8b --data news"
```

### 3. Submit Jobs

```bash
# Preview without submitting
jrun submit config/search.yaml --dry-run

# Submit for real
jrun submit config/search.yaml
```

### 4. Check Status

```bash
# All tracked jobs
jrun status

# Jobs under a specific search name
jrun status eval

# Single job by ID
jrun status <job-id>
```

### 5. Manage Jobs

```bash
# Stop a running job
jrun stop <job-id>

# Archive a finished job
jrun cancel <job-id>

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
| `{auto:5s}` | Deterministic 5-char hash based on parameters |

## Project Structure

```
your-project/
├── .jrun/
│   ├── settings.json    # Experiment name/ID
│   └── jobs.json        # Submitted job tracking
└── config/
    └── search.yaml      # Job config files
```
