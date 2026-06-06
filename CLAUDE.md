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

# Install as editable tool (already done, no need to reinstall after code changes)
uv tool install -e .
```

**Important:** jrun is installed with `uv tool install -e .` (editable mode). Code changes take effect immediately — do NOT reinstall after modifying source files.

**Important:** Unset `http_proxy` and `https_proxy` environment variables before running jrun, or airsctl subprocess calls will fail.

## Testing

**Rule: Every feature or bug fix MUST include corresponding unit tests before it is considered complete.**

**Rule: When fixing a bug, also analyze why existing tests failed to catch it. Add or strengthen tests to cover the gap — the fix is not complete until the test suite would catch a regression.**

```bash
# Run the full test suite
uv run pytest

# Run with verbose output
uv run pytest -v

# Run a specific test file
uv run pytest tests/test_models.py

# Run a specific test class or method
uv run pytest tests/test_config.py::TestGridExpansion::test_multi_param_cartesian
```

Test files map 1:1 to source modules:

| Source | Test | Coverage |
|--------|------|----------|
| `models.py` | `test_models.py` | WorkerConfig, ResourceConfig, Job dataclasses, from_dict/to_dict roundtrips |
| `config.py` | `test_config.py` | ConfigLoader: variable resolution, $$ escaping, grid expansion, max_trials, search name, scheduler params |
| `project.py` | `test_project.py` | Project: init, tracker CRUD (record/update/remove jobs and searches), URL building, platform ID extraction |
| `platform.py` | `test_platform.py` | PlatformClient: status parsing (JSON and text), subprocess command construction |
| `formatter.py` | `test_formatter.py` | StatusFormatter: colored output, hyperlinks, table printing |
| `scheduler.py` | `test_scheduler.py` | Scheduler: PID file management, is_running/stop, path properties |
| `cli.py` | `test_cli.py` | Click commands: template, init, submit --dry-run, status |

Dev dependency: `pytest>=7.0` (install with `uv sync`).

No linter or formatter configured. No CI pipeline exists.

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

All source lives in `src/jrun/`. Entry point: `jrun = "jrun.cli:cli"` in pyproject.toml. Dependencies are only `click>=8.0` and `pyyaml>=6.0`. The codebase uses an OOP architecture with clear separation of concerns.

- **models.py** — Core dataclasses: `Job`, `ResourceConfig`, `WorkerConfig`. All have `from_dict()`/`to_dict()` for JSON serialization. `Job.is_local` checks if `queue_name == "local"`.

- **config.py** — `ConfigLoader` class. Loads YAML configs with `$CONFIG_DIR`/`$VAR` substitution and `$$` → `$` escaping at the raw text level before parsing. `expand_grid()` does cartesian product over search params. `_resolve_command()` handles both `command` (string) and `commands` (list). `{auto:Ns}` in job names produces a deterministic MD5-based hash.

- **platform.py** — `PlatformClient` class. Wraps `airsctl` subprocess calls: `experiment_list`, `experiment_modify`, `job_run`, `job_stop`, `job_cancel`, `job_list`. `submit_job()` orchestrates the experiment-modify-then-job-run flow. `remove_configs()` removes named configs from an experiment.

- **project.py** — `Project` class. Manages `.jrun/` directory, settings (`settings.json`), and job tracker (`jobs.json`). `find_jrun_dir()` walks up the directory tree. Tracker operations: `record_search`, `record_single_job`, `find_job_by_name`, `remove_job`, `remove_search`, `update_job_status`, `record_pending_jobs`. Uses `_LockedTracker` context manager with `fcntl` file locking.

- **formatter.py** — `StatusFormatter` class. Handles colored terminal output, ANSI hyperlinks, and tabular job status display.

- **scheduler.py** — `Scheduler` class. Background daemon for `parallel_trials` job management. Manages PID files, polls job statuses, and submits queued jobs up to the parallel limit.

- **errors.py** — Exception hierarchy: `JrunError` (base), `PlatformError` (airsctl failures), `ConfigError` (config loading/validation).

- **cli.py** — Thin Click command layer. Defines subcommands: `init`, `template`, `submit`, `status`, `stop`, `remove`, `list`. Delegates all business logic to the classes above.

- **template.py** — Contains `TEMPLATE_CONTENT`, the annotated YAML config template string.

## Error Handling Convention

All error handling follows a layered exception pattern:

1. **`errors.py` defines the exception hierarchy:**
   - `JrunError` — base exception for all jrun errors
   - `PlatformError(command, returncode, stderr, stdout)` — raised when `airsctl` subprocess calls fail
   - `ConfigError` — raised for config loading/validation errors

2. **`PlatformClient` (platform.py) raises exceptions, never logs:**
   - `_run(args, check=True)` raises `PlatformError` when `returncode != 0` and `check=True`
   - `_run` auto-retries once on `Unauthenticated` errors (35s delay) before raising
   - `submit_job()` raises `PlatformError` on any failure (no `log_fn` parameter)
   - Query methods like `get_job_status()` use `check=False` and return `None` on failure
   - `remove_config_from_experiment` / `remove_configs_from_experiment` use `check=False` (best-effort cleanup)

3. **CLI layer (cli.py) catches and formats:**
   - Critical operations (submit, list): `try/except PlatformError as e` → `click.echo(f"Error: {e}", err=True)` + `raise SystemExit(1)`
   - Best-effort operations (stop/cancel during cleanup, init lookup): `try/except PlatformError: pass`
   - Per-job errors in batch submit: catch per-job, print error, continue to next job

4. **Scheduler (scheduler.py) catches and logs:**
   - `try/except PlatformError as e` → `self._log(f"Submit error: {e}")` + retry logic

**Rules for new code:**
- Never use `log_fn` callbacks for error reporting
- Never silently swallow errors with bare `return None` — either raise or explicitly `except PlatformError: pass` with clear intent
- `PlatformError.__str__()` produces a human-readable message like: `airsctl experiment list failed (rc=255): login expired`
- The CLI layer is the only place that formats errors for the user

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

Both `search:` and `job:` support optional `experiment_id` and `experiment_name` fields that override the defaults from `jrun init`. If omitted, the defaults from `.jrun/settings.json` are used.

When submitting, if the target experiment already has ≥100 configs, jrun will warn and ask for confirmation before proceeding.

Special cases:
- `resource_config.queue_name: local` → runs command locally via `subprocess.run(shell=True)` instead of submitting
- `resource_config.worker.replicas` → multi-node job with master + worker pods

Variable substitution (all resolved in `load_config()` at the raw text level before YAML parsing): `$CONFIG_DIR` (jrun built-in), `$VAR` (local env, errors if undefined), `$$VAR` → `$VAR` (remote env passthrough), `{param}` (grid params), `{auto:Ns}` (MD5 hash).
