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

## Platform API Documentation

`docs/platform-api.md` is the authoritative reference for every jiuding platform REST API that jrun depends on (auth, queue/select, experiment create/edit/delete, job/select, job-snapshots, etc.), including request/response shapes, projset scoping rules, and a jrun-module ↔ endpoint mapping table.

**Rule: Any change to jrun that touches platform behavior must first consult `docs/platform-api.md`.** When the platform API itself changes (new/changed fields, endpoints, auth), update BOTH `docs/platform-api.md` and the code (typically `src/jrun/api.py`) in the same change — never one without the other. When adding a new endpoint call to the code, add it to the doc (including the mapping table) as part of the same task.

Maintenance docs like this live in `docs/` — put future docs of the same kind there too.

## Docs & Skill Sync

**Rule: Any change to the CLI surface (commands, flags, output) or the config schema must, in the same change, update all of:**

- `skills/jrun-workflow/` (`SKILL.md` + `references/config-patterns.md`) — the workflow skill shipped with the repo
- `README.md` and `README_zh.md` — keep both languages in sync
- `src/jrun/template.py` and `config/template.yaml` — must stay content-identical (guarded by `test_template_file_in_repo_matches_builtin` in `test_cli_misc.py`)
- `examples/*.yaml` — when the schema change affects them
- The test-mapping table and module descriptions in this file

The current command layout: top-level `init`/`login`/`template`/`submit`/`queues`/`gpu-usage`, plus the `experiment` group (`create`/`list`/`jobs`/`delete`) and the `job` group (`status`/`stop`/`remove`). Config schema highlights: `experiment:` is identity-only (`name` required); `image`/`image_region` are job-level fields; the queue is `resource_config.queue_name`; the deprecated `experiment.image`/`image_region`/`queue_name` remain as fallbacks.

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
| `platform.py` | `test_platform.py` | PlatformClient: status parsing (JSON and text), subprocess command construction, find_experiment |
| `formatter.py` | `test_formatter.py` | StatusFormatter: colored output, hyperlinks, table printing; pct/format_ms/snapshot_sort_key/print_experiment_job_table helpers |
| `scheduler.py` | `test_scheduler.py` | Scheduler: PID file management, is_running/stop, path properties |
| `api.py` | `test_api.py` | PlatformAPI: AK/SK signature, token exchange, auth precedence, queue quotas, experiment creation/deletion, experiment jobs (job/select pagination), job snapshots (incl. job_snapshots_all pagination) |
| `submission.py` | `test_submission.py` | ensure_experiment, resolve_experiment (spec/legacy/no-experiment paths), classify_jobs, submit_single |
| `tracking.py` | `test_tracking.py` | find_tracked_job, stop_tracked_job, remove_one_job, refresh_active_statuses scoping |
| `commands/` | `test_cli_<area>.py` | Per-command CliRunner tests: `test_cli_misc.py` (template/init/login), `test_cli_submit.py`, `test_cli_status.py` (job status), `test_cli_jobs.py` (job stop/remove), `test_cli_experiment.py` (experiment create/list), `test_cli_inspect.py` (queues/gpu-usage). Shared fixtures (runner, project_dir, SAMPLE_QUEUE, SPEC_CONFIG) live in `tests/conftest.py` |

Dev dependency: `pytest>=7.0` (install with `uv sync`).

No linter or formatter configured. No CI pipeline exists.

## Key Commands

Conceptual model: a **project** is the current directory's `.jrun/` folder tracking all submissions made from it; an **experiment** is a jiuding platform experiment; a **job** is a single run under an experiment. The CLI mirrors this: `jrun submit` creates jobs, `jrun job ...` manages tracked jobs, `jrun experiment ...` manages platform experiments.

```bash
jrun init                                        # Initialize .jrun/ in current directory (no experiment binding)
jrun login                                       # Store platform AK/SK in ~/.jrun/auth.json (0600)
jrun template                                    # Print config template to stdout
jrun template -f config/my.yaml                  # Write config template to file
jrun experiment create config/my.yaml            # Create experiment from the config's experiment: block
jrun experiment list                             # List all experiments on the platform
jrun experiment jobs <name-or-id>                # List platform-side jobs under an experiment (-s status filter)
jrun experiment delete <name-or-id>              # Delete (archive) an experiment; -y skips confirmation
jrun submit config/search.yaml                   # Submit jobs (auto-creates the experiment if missing)
jrun submit config/search.yaml -y                # Skip the experiment-creation confirmation
jrun queues                                      # Free GPUs per queue, grouped by cluster
jrun gpu-usage                                   # Per-job GPU utilization (admin token; -s status filter, --gpu-only default, --sort time/gpus/gpu/gmem/cpu/mem/owner)
jrun submit config/search.yaml --dry-run         # Preview without submitting
jrun submit config/search.yaml -o                # Auto-overwrite existing jobs
jrun submit config/search.yaml -o -s Failed      # Only overwrite jobs with specific status
jrun job status                                  # Show all tracked jobs (colored, with platform links)
jrun job status <search-name-or-job-name-or-id>  # Filter by search, job name, or job ID
jrun job status -s Running                       # Filter by status
jrun job stop <job-name-or-id>                   # Stop a running job
jrun job remove <job-name-or-id>                 # Remove job: stop + delete config + remove from tracker
jrun job remove <search-name>                    # Remove entire search and all its jobs
```

## Architecture

All source lives in `src/jrun/`. Entry point: `jrun = "jrun.cli:cli"` in pyproject.toml. Dependencies are only `click>=8.0` and `pyyaml>=6.0`. Layering: `commands/` (click 命令层) → `submission.py`/`tracking.py` (服务层) → `platform.py`/`api.py`/`project.py` (平台与状态) → `models.py`/`config.py` (数据与配置).

- **cli.py** — Entry point only: defines the click group and calls `jrun.commands.register(cli)`. No command logic here.

- **commands/** — Thin Click command layer, one module per command area: `misc.py` (template/init/login), `inspect.py` (queues/gpu-usage), `experiment.py` (`experiment` group: create/list/jobs/delete), `submit.py`, `jobs.py` (`job` group: stop/remove, plus `status` mounted from `status.py`). Each module exposes `register(cli)`. This is the only layer that formats errors for the user. Shared helper: `default_proj_ids()` in `commands/__init__.py`.

- **submission.py** — Submission orchestration service, no click dependency (interactive decisions and messages injected as `confirm`/`choose`/`echo`/`progress` callbacks). `ensure_experiment()` (find-or-create via API; creation placeholder takes the first job's image/queue, falling back to the deprecated spec fields), `resolve_experiment()` (spec vs legacy experiment_id path, deprecation warning, one experiment snapshot for the config-count warning and modify), `resolve_queue_locations()` (per distinct effective queue, cached), `classify_jobs()` (new/overwrite/skipped), `submit_jobs()` and `submit_single()` (one full config replacement, run-only calls, local tracker replacement, and scheduler daemon), `run_local()`. `teardown_overwrites()` remains only as a deprecated no-op compatibility hook. Per-job overrides are computed by `models.job_runtime`/`models.effective_spec` (job-level image/queue win over spec fallbacks). Result dataclasses: `ResolvedExperiment` (with `queue_locations` and `experiment_data`), `JobClassification`, `SubmitResult`.

- **tracking.py** — Tracker/lifecycle service: `refresh_job_for_display`, `refresh_active_statuses` (with progress bar), `find_tracked_job` (by ID or name), `stop_tracked_job` (queued/pending short-circuit), `remove_one_job` (best-effort cancel + config removal).

- **models.py** — Core dataclasses: `Job` (incl. job-level `image`/`image_region`), `ResourceConfig`, `WorkerConfig`, `ExperimentSpec` (experiment identity + deprecated image/queue fallbacks; `IMAGE_REGION_VALUES` maps `PUBLIC`/`PRIVATE` to the int enum airsctl needs). All have `from_dict()`/`to_dict()` for JSON serialization. `Job.is_local` checks if `queue_name == "local"`. Module helpers `job_runtime()`/`effective_spec()` resolve the effective per-job image/queue (job-level wins, spec fields are the fallback).

- **config.py** — `ConfigLoader` class. Loads YAML configs with `$CONFIG_DIR`/`$VAR` substitution and `$$` → `$` escaping at the raw text level before parsing. `expand_grid()` does cartesian product over search params. `_resolve_command()` handles both `command` (string) and `commands` (list). `{auto:Ns}` in job names produces a deterministic MD5-based hash.

- **platform.py** — `PlatformClient` class. Wraps `airsctl` subprocess calls: `experiment_list`, `experiment_modify`, `job_run`, `job_stop`, `job_cancel`, `job_list`. `get_experiment()` caches one detail snapshot, `modify_configs()` replaces the complete `advance_config_infos` list from cloned templates, and `run_config()` starts an already-installed config without modifying the experiment. `submit_job()` remains a compatibility wrapper around one modify plus one run. `find_experiment()` resolves an experiment by name or UUID via `experiment list`; `remove_configs()` remains for explicit lifecycle commands.

- **api.py** — `PlatformAPI` class. Direct REST calls to `https://platform-multi.baai.ac.cn` for what airsctl cannot do: token exchange (AK/SK + HMAC-SHA256), `queue/select` (queue quotas), `experiment` creation, `experiment/delete` (archive semantics), `job/select` filtered by experiment (`jrun experiment jobs`), `job-snapshots/select` (utilization), `projsets/select-joined` (the account's projsets). Auth resolves in order: `AIRS_AK`/`AIRS_SK` env → `~/.jrun/auth.json` (`jrun login`) → `/etc/accesskey/{user-ak,user-sk}` (base64-encoded, k8s secret mount on jiuding pods) → `/tmp/.airs/.token` (airsctl's cache). Uses stdlib urllib with env proxies bypassed (`ProxyHandler({})`). `queue/select` and `job-snapshots/select` are projset-scoped (403 without one): when no `projset_id` is passed, `list_queues` queries every joined projset and merges, and `job_snapshots` falls back to the first joined projset. `job/select` (filtered by experiment) is projset-scoped too: `experiment_jobs_all` probes the joined projsets with a minimal page and paginates with the first that doesn't 403. Module helpers: `compute_signature`, `queue_location_info`, `save_auth`.

- **project.py** — `Project` class. Manages `.jrun/` directory, settings (`settings.json`), and job tracker (`jobs.json`). `find_jrun_dir()` walks up the directory tree. Tracker operations: `record_search`, `record_single_job`, `find_job_by_name`, `remove_job`, `remove_search`, `update_job_status`, `record_pending_jobs`. Uses `_LockedTracker` context manager with `fcntl` file locking.

- **formatter.py** — `StatusFormatter` class (colored output, ANSI hyperlinks, job tables) plus pure display helpers: `pct`, `format_ms`, `format_ms_long`, `snapshot_sort_key`, `colored_status`, `hyperlink`, `print_experiment_job_table` (the `experiment jobs` panel; mirrors the `job status` columns, LINK when job URLs can be built, else raw IDs).

- **scheduler.py** — `Scheduler` class. Background daemon for `parallel_trials` job management. Manages PID files, polls job statuses, and runs already-installed pending configs up to the parallel limit. It never modifies the experiment or stops/cancels an old overwrite job.

- **errors.py** — Exception hierarchy: `JrunError` (base), `PlatformError` (airsctl failures), `ConfigError` (config loading/validation), `ApiError` (REST API failures).

- **template.py** — Contains `TEMPLATE_CONTENT`, the annotated YAML config template string.

## Error Handling Convention

All error handling follows a layered exception pattern:

1. **`errors.py` defines the exception hierarchy:**
   - `JrunError` — base exception for all jrun errors
   - `PlatformError(command, returncode, stderr, stdout)` — raised when `airsctl` subprocess calls fail
   - `ConfigError` — raised for config loading/validation errors
   - `ApiError(url, status_code, message)` — raised when platform REST API calls fail

2. **`PlatformClient` (platform.py) raises exceptions, never logs:**
   - `_run(args, check=True)` raises `PlatformError` when `returncode != 0` and `check=True`
   - `_run` auto-retries once on `Unauthenticated` errors (35s delay) before raising
   - `modify_configs()` and `run_config()` raise `PlatformError` on platform failures; `submit_job()` is a compatibility wrapper around both (no `log_fn` parameter)
   - Query methods like `get_job_status()` use `check=False` and return `None` on failure
   - `remove_config_from_experiment` / `remove_configs_from_experiment` use `check=False` (best-effort cleanup)

3. **Command layer (commands/) catches and formats:**
   - Critical operations (submit, list): `try/except PlatformError as e` → `click.echo(f"Error: {e}", err=True)` + `raise SystemExit(1)`
   - Best-effort operations (stop/cancel during cleanup): `try/except PlatformError: pass` inside `tracking.py` helpers
   - Per-job errors in batch submit: caught per-job in `submission.submit_jobs`, printed via injected `err_echo`, continue to next job

4. **Scheduler (scheduler.py) catches and logs:**
   - `try/except PlatformError as e` → `self._log(f"Submit error: {e}")` + retry logic

**Rules for new code:**
- Never use `log_fn` callbacks for error reporting
- Never silently swallow errors with bare `return None` — either raise or explicitly `except PlatformError: pass` with clear intent
- `PlatformError.__str__()` produces a human-readable message like: `airsctl experiment list failed (rc=255): login expired`
- The command layer (`commands/`) is the only place that formats errors for the user; service modules raise or take injected `echo`/`confirm` callbacks

## Job Submission Flow

1. Resolve the target experiment and read its detail once. The first existing
   `advance_config_infos` entry is retained as a structural template.
2. Expand the YAML and classify names as new, explicitly overwritten, or
   skipped. Only the first two categories form the current submission set.
3. Clone the template once per selected job and replace the entire
   `advance_config_infos` list with that set in one
   `airsctl experiment modify -f <tmp.json>` call.
4. Call `airsctl job run -e <id> -n <config_name>` once for each selected job.
   A run failure is reported and does not prevent other selected jobs from
   being attempted.
5. Write successful IDs to `.jrun/jobs.json`. For an overwrite, remove the old
   local mapping only after its replacement starts; modify/run failure leaves
   the old mapping intact. No submit-time stop, cancel, or per-config removal
   is performed, and already-started platform jobs continue running.
6. With `parallel_trials`, all selected configs are installed in step 3; the
   scheduler later performs only step 4 for pending configs.

The experiment config is therefore the current YAML working set, not a job
history. `.jrun/jobs.json` is the durable local source of job IDs and status.
Do not share an experiment between independent submitters unless full-list
replacement is intended.

**Important:** `airsctl experiment modify` parses the JSON file with plain `encoding/json` (NOT protojson), so enum fields like `image_region` must be **integer** values (`PUBLIC=1`, `PRIVATE=2` — see `ExperimentSpec.image_region_value`); string names are rejected. The REST API (`api.py`) is the opposite: it accepts enum names.

## Config Format

Two mutually exclusive top-level keys:

- **`search:`** — Grid search with `job_template` (name, commands, envs, resource_config), `params` (list of name/values), `sampling: grid`, `max_trials`, and optional `parallel_trials` (initial batch plus run-only scheduler).
- **`job:`** — Single job with `name`, `command`/`commands`, `envs`, `resource_config`.

An optional third top-level key binds the config to an experiment:

- **`experiment:`** — `name` (required) + optional `description`/`proj_id`/`projset_id` (creation metadata) and `cluster_id`/`zone_id` (explicit queue-resolution overrides). On the platform an experiment is just a container for configs, so image/queue do NOT live here: they are job-level fields (`image:`/`image_region:` on `job:`/`search.job_template:`, queue via `resource_config.queue_name:`). The spec's `image`/`image_region`/`queue_name` remain as deprecated fallbacks for jobs that don't set them (a warning is printed). On submit, jrun looks the experiment up by name via airsctl and creates it via the REST API (`PlatformAPI.create_experiment`) if missing, using the first job's image/queue for the placeholder config. Queue IDs are resolved per distinct effective `queue_name` via `queue/select` (`submission.resolve_queue_locations`), so different jobs in one config may use different queues.

Both `search:` and `job:` support optional `experiment_id` and `experiment_name` fields that override the defaults from `.jrun/settings.json` (legacy flow without an `experiment:` block). If omitted, the defaults from `.jrun/settings.json` are used.

When submitting, if the target experiment already has ≥100 configs, jrun will warn and ask for confirmation before proceeding.

Special cases:
- `resource_config.queue_name: local` → runs command locally via `subprocess.run(shell=True)` instead of submitting
- `resource_config.worker.replicas` → multi-node job with master + worker pods

Variable substitution (all resolved in `load_config()` at the raw text level before YAML parsing): `$CONFIG_DIR` (jrun built-in), `$VAR` (local env, errors if undefined), `$$VAR` → `$VAR` (remote env passthrough), `{param}` (grid params), `{auto:Ns}` (MD5 hash).
