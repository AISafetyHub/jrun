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

There is no test suite, linter, or formatter configured. No Makefile or CI pipeline exists.

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

All source lives in `src/jrun/`. Entry point: `jrun = "jrun.cli:cli"` in pyproject.toml. Dependencies are only `click>=8.0` and `pyyaml>=6.0`.

- **cli.py** — Click command group. Defines subcommands: `init`, `template`, `submit`, `status`, `stop`, `remove`, `list`. The `submit` command is the most complex — it orchestrates config loading, grid expansion, overwrite/skip logic, and per-job submission. Contains `_submit_one_job()` which handles the experiment-modify-then-job-run flow.

- **config.py** — YAML config loading and grid expansion. `load_config()` does `$CONFIG_DIR`/`$HOME` substitution at the raw text level before YAML parsing. `expand_grid()` does cartesian product over search params. `_resolve_command()` handles both `command` (string) and `commands` (list, joined with `&&`), plus `$$` → `$` escaping and `{param}` substitution. `{auto:Ns}` in job names produces a deterministic MD5-based hash. Note: `build_single_job()` does NOT apply `$$` escaping — only `expand_grid` path does.

- **airsctl.py** — Thin subprocess wrapper. All calls go through `_run()` which prepends `airsctl` to args. Functions: `experiment_list`, `experiment_modify`, `job_list`, `job_run`, `job_stop`, `job_cancel`. No error handling on subprocess failures — callers check stdout.

- **settings.py** — Manages `.jrun/settings.json`. `find_jrun_dir()` walks up the directory tree from cwd. `extract_platform_ids()` parses `projsetId`/`projId`/`userId` from experiment storage_info for building platform URLs. The platform base URL is hardcoded.

- **tracker.py** — Manages `.jrun/jobs.json`. Two-level structure: `searches` (name → config_file, submitted_at, job_ids) and `jobs` (id → name, search_name, params, status, submitted_at). `record_search()` merges new jobs with existing ones, keeping skipped jobs from prior runs.

- **template.py** — Contains `TEMPLATE_CONTENT`, the annotated YAML config template string.

## Job Submission Flow (cli.py `_submit_one_job`)

1. Fetch current experiment config via `airsctl experiment list -e <id>`
2. Clone the first `advance_config_infos` entry as a template
3. Set `config_name` to the job name, apply resource_config overrides
4. Build worker config if `resource_config.worker` is present (multi-node)
5. Merge environment variables into `envs` list (key-value dicts)
6. Write modified experiment JSON to a temp file
7. Call `airsctl experiment modify -f <tmp.json>` to register the config
8. Call `airsctl job run -e <id> -n <config_name>` to start the job
9. Parse job UUID from stdout regex (`[Jj]ob.*?([0-9a-f-]{36})`)
10. Clean up temp file

## Config Format

Two mutually exclusive top-level keys:

- **`search:`** — Grid search with `job_template` (name, commands, envs, resource_config), `params` (list of name/values), `sampling: grid`, `max_trials`, `parallel_trials` (not yet implemented).
- **`job:`** — Single job with `name`, `command`/`commands`, `envs`, `resource_config`.

Special cases:
- `resource_config.queue_name: local` → runs command locally via `subprocess.run(shell=True)` instead of submitting
- `resource_config.worker.replicas` → multi-node job with master + worker pods

Variable substitution (all resolved in `load_config()` at the raw text level before YAML parsing): `$CONFIG_DIR` (jrun built-in), `$VAR` (local env, errors if undefined), `$$VAR` → `$VAR` (remote env passthrough), `{param}` (grid params), `{auto:Ns}` (MD5 hash).
