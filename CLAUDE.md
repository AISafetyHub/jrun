# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What is jrun

A Python CLI tool that wraps `airsctl` to provide grid-search parameter expansion, multi-node job support, local debugging, and job tracking for the airsctl platform. It lets users define jobs in YAML configs (single, grid search, multi-node, or local), submits them via `airsctl` subprocess calls, and tracks their state in a local `.jrun/jobs.json` file.

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
jrun init -N <experiment-name>                  # Initialize .jrun/ in current directory
jrun init -N <name> -e <id>                     # Initialize with experiment ID
jrun template                                    # Print config template to stdout
jrun template -f config/my.yaml                  # Write config template to file
jrun submit config/search.yaml                   # Submit jobs from config
jrun submit config/search.yaml --dry-run         # Preview without submitting
jrun submit config/search.yaml -o                # Auto-overwrite existing jobs
jrun submit config/search.yaml -o -s Failed      # Only overwrite jobs with specific status
jrun status                                      # Show all tracked jobs (colored, with platform links)
jrun status <search-name-or-job-name-or-id>      # Filter by search, job name, or job ID
jrun status -s Running                           # Filter by status
jrun stop <job-name-or-id>                       # Stop a running job
jrun remove <job-name-or-id>                     # Remove job: stop + delete config + remove from tracker
jrun remove <search-name>                        # Remove entire search and all its jobs
jrun list                                        # List all experiments
```

## Architecture

All source lives in `src/jrun/`. The package has six modules:

- **cli.py** — Click command group (`cli`). Entry point registered as `jrun = "jrun.cli:cli"` in pyproject.toml. Defines subcommands: `init`, `template`, `submit`, `status`, `stop`, `remove`, `list`. The `submit` command orchestrates: load config → expand grid → handle overwrite/skip logic → for each job: modify experiment via temp JSON → run job → record in tracker. Supports local execution when `queue_name` is `local`.

- **config.py** — YAML config loading and grid expansion. `load_config()` reads YAML with `$CONFIG_DIR`/`$HOME`/`$$VAR` variable substitution. `expand_grid()` does cartesian product over search params to produce job dicts (with `envs` support). `build_single_job()` handles the non-search case. `{auto:Ns}` in job names produces a deterministic MD5-based hash. Both `command` (string) and `commands` (list, joined with `&&`) are supported.

- **airsctl.py** — Thin subprocess wrapper around the `airsctl` CLI binary. Functions: `experiment_list`, `experiment_modify`, `job_list`, `job_run`, `job_stop`, `job_cancel`.

- **settings.py** — Manages `.jrun/settings.json` (experiment name/ID/platform IDs). `find_jrun_dir()` walks up the directory tree. `extract_platform_ids()` parses platform IDs from experiment data for building job URLs. `build_job_url()` constructs clickable platform links.

- **tracker.py** — Manages `.jrun/jobs.json` for local job tracking. Records searches (with constituent job IDs) and individual jobs. Supports: `record_search`, `record_single_job`, `find_job_by_name`, `remove_job`, `remove_search`, `update_job_status`.

- **template.py** — Contains `TEMPLATE_CONTENT`, the annotated YAML config template string used by the `jrun template` command.

## Config Format

The code reads two top-level keys from YAML configs:

- **Grid search** — `search:` section with `job_template` (name, commands, envs, resource_config), `params` (list of name/values), `sampling: grid`, `max_trials`, `parallel_trials`.
- **Single job** — `job:` section with `name`, `command`/`commands`, `envs`, `resource_config`.
- **Multi-node job** — Single job with `resource_config.worker` containing `replicas` and optional resource overrides.
- **Local job** — Single job with `resource_config.queue_name: local`, runs directly on the local machine.

Resource config fields: `queue_name`, `priority`, `accelerator_model`, `accelerator_count`, `cpu_cores`, `mem_gib`, `shared_mem_gib`, `worker` (replicas + resource overrides).

## Job Submission Flow

1. Load YAML config and expand grid parameters
2. Check for existing jobs with same name — prompt or auto-overwrite based on flags
3. Teardown old jobs if overwriting (stop + remove config + remove from tracker)
4. Fetch current experiment config from airsctl (`experiment list -e <id>`)
5. Clone the first existing `advance_config_infos` entry as a template
6. Apply resource_config overrides, worker config, and environment variables
7. Write modified experiment config to a temp JSON file
8. Call `airsctl experiment modify -f <tmp.json>` to register the new config
9. Call `airsctl job run` with experiment + config name to start the job
10. Parse job UUID from stdout and record in `.jrun/jobs.json`
