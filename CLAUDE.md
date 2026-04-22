# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What is jrun

A Python CLI tool that wraps `airsctl` to provide grid-search parameter expansion and job tracking for the airsctl platform. It lets users define jobs in YAML configs (single or grid search), submits them via `airsctl` subprocess calls, and tracks their state in a local `.jrun/jobs.json` file.

## Build & Run

```bash
# Install dependencies (uses uv + hatchling)
uv sync

# Run in development mode
uv run jrun --help

# Install as a tool
uv tool install .
```

**Important:** Unset `http_proxy` and `https_proxy` environment variables before running jrun, or airsctl subprocess calls will fail.

## Key Commands

```bash
jrun init -N <experiment-name>          # Initialize .jrun/ in current directory
jrun submit config/search.yaml          # Submit jobs from config
jrun submit config/search.yaml --dry-run # Preview without submitting
jrun status                              # Show all tracked jobs
jrun status <search-name-or-job-id>      # Filter by search or job
jrun stop <job-id>                       # Stop a running job
jrun cancel <job-id>                     # Archive a finished job
jrun list                                # List all experiments
```

## Architecture

All source lives in `src/jrun/`. The package has five modules with clear responsibilities:

- **cli.py** — Click command group (`cli`). Entry point registered as `jrun = "jrun.cli:cli"` in pyproject.toml. Defines all subcommands (`init`, `submit`, `status`, `stop`, `cancel`, `list`). The `submit` command orchestrates the full flow: load config → expand grid → for each job: modify experiment via temp JSON file → run job → record in tracker.

- **config.py** — YAML config loading and grid expansion. `load_config()` reads YAML with `$CONFIG_DIR`/`$HOME`/`$$VAR` variable substitution. `expand_grid()` does cartesian product over search params to produce job dicts. `build_single_job()` handles the non-search case. `{auto:Ns}` in job names produces a deterministic MD5-based hash.

- **airsctl.py** — Thin subprocess wrapper around the `airsctl` CLI binary. Every function shells out to `airsctl` with the appropriate subcommand and flags. Returns `CompletedProcess` or stdout strings.

- **settings.py** — Manages `.jrun/settings.json` (experiment name/ID). `find_jrun_dir()` walks up the directory tree to locate the nearest `.jrun/` directory.

- **tracker.py** — Manages `.jrun/jobs.json` for local job tracking. Records searches (with their constituent job IDs) and individual jobs. Provides lookup by search name, job name, or job ID.

## Config Format

Two YAML config types (see `test/` for examples):

- **Grid search** — has a `search:` section with `job_template`, `params` (list of name/values), `sampling: grid`, `max_trials`, `parallel_trials`.
- **Single job** — has a `job:` section with `name`, `command`, `resource_config`.

Both support `resource_config` with fields: `queue_name`, `priority`, `accelerator_model`, `accelerator_count`, `cpu_cores`, `mem_gib`, `shared_mem_gib`.

## Job Submission Flow

1. Load YAML config and expand grid parameters
2. Fetch current experiment config from airsctl (`experiment list -e <id>`)
3. Clone the first existing `advance_config_infos` entry as a template
4. Write modified experiment config to a temp JSON file
5. Call `airsctl experiment modify -f <tmp.json>` to register the new config
6. Call `airsctl job run` with experiment + config name to start the job
7. Parse job UUID from stdout and record in `.jrun/jobs.json`
