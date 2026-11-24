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

`docs/airsctl.md` documents the `airsctl` subcommands and flags jrun shells out to (experiment list/modify, job list/run/stop/cancel, public-queue `job run` flags) and how jrun uses them. Consult it before changing `src/jrun/platform.py`.

Maintenance docs like these live in `docs/` — put future docs of the same kind there too.

## Docs & Skill Sync

**Rule: Any change to the CLI surface (commands, flags, output) or the config schema must, in the same change, update all of:**

- `skills/jrun-workflow/` (`SKILL.md` + `references/config-patterns.md`) — the workflow skill shipped with the repo
- `README.md` and `README_zh.md` — keep both languages in sync
- `src/jrun/template.py` and `config/template.yaml` — must stay content-identical (guarded by `test_template_file_in_repo_matches_builtin` in `test_cli_misc.py`)
- `examples/*.yaml` — when the schema change affects them
- The test-mapping table and module descriptions in this file

The current command layout: top-level `init`/`login`/`template`/`submit`/`queues`/`gpu-usage`, plus the `experiment` group (`create`/`list`/`jobs`/`delete`) and the `job` group (`status`/`stop`/`remove`). Config schema highlights: a top-level `experiment:` block (at least `name`) is required for a real submit and supplies defaults; `image`/`image_region` and resource names are job-level fields that win over those defaults; queue placement uses `project_set_name`/`project_name`/`region_name`/`zone_name`/`queue_name`; multi-node uses `resource_config.roles`; `resource_config.queue_name: auto` places jobs by free cards with paired `accelerator_model`/`accelerator_count` lists and a preference-ordered `queue_category` list; unknown job keys are rejected (raw fields go under `platform_overrides:`).

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
| `models.py` | `test_models.py` | Status constants (Approving/Pending active and stoppable), per-category priority rule, queue-category (incl. lists) and image-region helpers, RunOptions (validation, unknown keys), ResourceConfig/RoleConfig (roles, removed `worker`/ID fields, `accelerator_choices` pairing), Job, ExperimentSpec, JobTarget/`job_target` precedence, `validate_job` (auto rules), `total_replicas` |
| `config.py` | `test_config.py` | ConfigLoader: variable resolution, $$ escaping, grid expansion, max_trials, search name, scheduler params, removed/invalid/unknown keys, `jobs()` and `commands` newline joining, experiment spec |
| `project.py` | `test_project.py` | Project: init (empty settings), tracker CRUD (record/update/remove jobs and searches, per-job experiment_id), `build_job_url`, `extract_platform_ids` |
| `platform.py` | `test_platform.py` | PlatformClient: status parsing, command construction, Unauthenticated retry, `get_experiment` cache, `_build_job_config` (roles inheritance, overrides, public/idle get their required priority, dedicated keeps the template's), `modify_configs`, `run_config`/`_parse_job_id` (missing ID is an error), `remove_configs`, `find_experiment` (exact name) |
| `formatter.py` | `test_formatter.py` | StatusFormatter: colored output (every known status has a color), hyperlinks, table printing; pct/format_ms/snapshot_sort_key/print_experiment_job_table helpers |
| `scheduler.py` | `test_scheduler.py` | Scheduler: PID file management, is_running/stop, path properties, tracker sync, run-only loop, run_options/queue-category forwarding, unlimited `parallel_trials=0`, public/idle submit-slot gating and its read-failure fallback |
| `api.py` | `test_api.py` | PlatformAPI: AK/SK signature, token exchange, auth precedence, dedicated and shared queues (list/job-count/config), `list_category_queues`, `shared_queue_budgets`, `filter_queues`/`with_project_context`, queue location info, experiment creation/deletion, experiment jobs (job/select pagination + projset fallback), job snapshots, `resolve_project_context` |
| `placement.py` | `test_placement.py` | Auto placement: pair/category/free-card preference, card consumption, public/idle shared pool, per-mode submit slots, role replicas, overflow to dedicated, wait/deferred, location filters, no-candidate error, one query per context/category, `apply_placement` (concrete job + `JRUN_*` envs) |
| `submission.py` | `test_submission.py` | find_queue, resolve_queue_locations (incl. known queues), ensure_experiment, validate_jobs, resolve_experiment (auto placement before experiment creation; dry-run placement and API-failure warning), run_local (exit code), classify_jobs, submit_single (incl. refused deferred job), submit_jobs (incl. stop-on-overwrite, parallel_trials, deferred jobs to the scheduler) |
| `tracking.py` | `test_tracking.py` | find_tracked_job, stop_tracked_job and release_job (strict; platform Pending/Approving jobs are really stopped), remove_tracked_jobs (stops and waits for active jobs before cancel, keeps still-active jobs tracked, per-experiment config removal), refresh_active_statuses |
| `commands/` | `test_cli_<area>.py` | Per-command CliRunner tests: `test_cli_misc.py` (template/init/login), `test_cli_submit.py` (dry-run, clean config errors, local run exit code, overwrite stop, batch flow, auto placement table/skip-before-place/no-slot error), `test_cli_status.py` (job status), `test_cli_jobs.py` (job stop/remove incl. search and status filters, remove stops running jobs first and keeps still-active ones), `test_cli_experiment.py` (experiment create/list/jobs/delete), `test_cli_inspect.py` (queues all/public/idle, gpu-usage). Shared fixtures (runner, project_dir, SAMPLE_QUEUE, SPEC_CONFIG) live in `tests/conftest.py`, plus an autouse fixture that makes any real HTTP request fail fast (mock `PlatformAPI` or inject a fake opener) |

Dev dependency: `pytest>=7.0` (install with `uv sync`).

No linter or formatter configured. No CI pipeline exists.

## Key Commands

Conceptual model: a **project** is the current directory's `.jrun/` folder tracking all submissions made from it; an **experiment** is a jiuding platform experiment; a **job** is a single run under an experiment. The CLI mirrors this: `jrun submit` creates jobs, `jrun job ...` manages tracked jobs, `jrun experiment ...` manages platform experiments.

```bash
jrun init                                        # Initialize .jrun/ in current directory (settings.json = {} marker, no experiment binding)
jrun login                                       # Store platform AK/SK in ~/.jrun/auth.json (0600); --ak/--sk skip the prompts
jrun template                                    # Print config template to stdout
jrun template -f config/my.yaml                  # Write config template to file
jrun experiment create config/my.yaml            # Create experiment from the config's experiment: block
jrun experiment list                             # List all experiments on the platform
jrun experiment jobs <name-or-id>                # List platform-side jobs under an experiment (-s status filter; --proj-id/--projset-id)
jrun experiment delete <name-or-id>              # Delete (archive) an experiment; -y skips confirmation
jrun submit config/search.yaml                   # Submit jobs (auto-creates the experiment if missing)
jrun submit config/search.yaml -y                # Skip the experiment-creation confirmation
jrun queues                                      # Free GPUs per queue, grouped by cluster (shared pools listed under both public and idle)
jrun queues --category public --ids              # Category filter (dedicated/public/idle, repeatable) + show IDs
jrun gpu-usage                                   # Per-job GPU utilization (admin token; -s status filter, --gpu-only default, --sort time/gpus/gpu/gmem/cpu/mem/owner, --asc, --cluster-id/--zone-id, -n 50)
jrun submit config/search.yaml --dry-run         # Preview without submitting (warns if no experiment: block; queue_name: auto prints the placement via read-only API queries)
jrun submit config/search.yaml -o                # Auto-overwrite existing jobs (stops the old runs first)
jrun submit config/search.yaml -o -s Failed      # Only overwrite jobs with specific status
jrun job status                                  # Show tracked jobs (colored, with platform links); 10 entries by default
jrun job status -n 0                             # Show all entries
jrun job status <search-name-or-job-name-or-id>  # Filter by search, job name, or job ID
jrun job status -s Running                       # Filter by status
jrun job stop <job-name-or-id>                   # Stop a running job
jrun job stop <search-name>                      # Stop a search's stoppable jobs and its scheduler
jrun job stop -s Running                         # Stop all tracked jobs in a stoppable status
jrun job remove <job-name-or-id>                 # Remove job: stop if active (wait to end) + cancel/archive + delete config (job's own experiment) + untrack
jrun job remove <search-name> [-s Failed]        # Remove entire search (or only jobs with the status)
```

`--proj-id`/`--projset-id` on `queues`, `gpu-usage`, `experiment jobs`, and `experiment delete` default to none / an auto-detected joined projset; nothing is read from `.jrun/settings.json`. Experiment lookup by name requires an exact match.

## Architecture

All source lives in `src/jrun/`. Entry point: `jrun = "jrun.cli:cli"` in pyproject.toml. Dependencies are only `click>=8.0` and `pyyaml>=6.0`. Layering: `commands/` (click 命令层) → `submission.py`/`placement.py`/`tracking.py` (服务层) → `platform.py`/`api.py`/`project.py` (平台与状态) → `models.py`/`config.py` (数据与配置).

- **cli.py** — Entry point only: defines the click group and calls `jrun.commands.register(cli)`. No command logic here.

- **commands/** — Thin Click command layer, one module per command area: `misc.py` (template/init/login), `inspect.py` (queues/gpu-usage), `experiment.py` (`experiment` group: create/list/jobs/delete), `submit.py`, `jobs.py` (`job` group: stop/remove, plus `status` mounted from `status.py`). Each module exposes `register(cli)`. This is the only layer that formats errors for the user. Shared helpers in `commands/__init__.py`: `fail(message)` (prints `Error: ...` to stderr, exits 1), `open_project()` (clean error when no `.jrun/`), `progressbar`, `refresh_statuses` (refresh + summary line), `register`.

- **placement.py** — Auto queue placement for `queue_name: auto`, no click. `collect_candidates(api, targets)` (one `list_category_queues` per project context × category, one `shared_queue_budgets` per context; free cards per pool `(queue_id, model)` so public/idle count once), pure `plan_placement(requests, free, budgets)` (start → queue → wait, see Config Format), `apply_placement` (rewrites the job into a concrete job + `JRUN_QUEUE_NAME`/`JRUN_ACCELERATOR_MODEL`/`JRUN_ACCELERATOR_COUNT` envs), `place_auto_jobs(api, jobs, spec) -> (Placement, queues by location_key)`. `Placement.deferred` names jobs waiting for a public/idle slot. Downstream code only sees concrete jobs.

- **submission.py** — Submission orchestration service, no click dependency (interactive decisions and messages injected as `confirm`/`choose`/`echo`/`err_echo`/`progress` callbacks). `ResolvedExperiment(spec, id, queue_locations, experiment_data)` with `.name`/`.config_count`; `find_queue` (ConfigError listing available queues), `resolve_queue_locations` (one lookup per distinct `JobTarget.location_key`), `ensure_experiment(spec, platform, api, target, ...)` (find by exact name or create via API from the target's image/queue), `validate_jobs` (`models.validate_job` per job), `has_auto_jobs`, `resolve_experiment(platform, api, spec, jobs, dry_run=..., ...)` (jobs = the ones being submitted; places auto jobs first, then requires the `experiment:` block unless dry-run, reads the experiment detail once; dry-run places only when given an API and warns on ApiError), `run_local` (returns the exit code), `classify_jobs` (new/overwrite/skipped), `submit_jobs(project, platform, resolved, search_name, config_file, jobs, sched_params, replacements=...)` (release old jobs, one modify, run each, track, scheduler for `parallel_trials` pending plus deferred auto jobs), `submit_single(project, platform, resolved, job, ...)` (refuses a deferred auto job with ConfigError). Result dataclasses: `ResolvedExperiment` (with `placement`/`deferred`), `JobClassification`, `SubmitResult`. The `submit` command classifies grid jobs before `resolve_experiment`, so skipped jobs consume no placement capacity.

- **tracking.py** — Tracker/lifecycle service, no click: `plain_progress`, `is_queued_id`, `refresh_job_for_display`, `refresh_active_statuses` (returns changed count; progress injected), `find_tracked_job` (by ID or name), `stop_tracked_job` (best-effort; local `queued-*` entries are only marked Stopped, platform `Pending`/`Approving` jobs are really stopped), `release_job` (strict stop used before an overwrite; raises PlatformError if a still-active job cannot be stopped), `remove_tracked_jobs` (stops non-terminal jobs and polls up to `wait_seconds` until they end, since `job cancel` only archives ended jobs; then best-effort cancel + config removal grouped per job's own experiment + untrack; returns `RemoveResult(removed, still_active)` and jobs still active stay tracked).

- **models.py** — Core data model. Dataclasses `RunOptions` (rejects unknown keys; validates public-queue fields), `RoleConfig`, `ResourceConfig` (`roles`, no `worker`; rejects removed ID fields), `ExperimentSpec` (identity, job defaults, and `DOCUMENT_FIELDS` written to the experiment document on modify), `Job`. `JobTarget` + `job_target(job, spec)` is the single resolver of the effective image/image_region/queue/category/location (job wins, spec fallback), with `.is_auto`, `.categories` (preference tuple), `.category` (first) and `.location_key`. `validate_job(job, spec)` enforces the auto rules (lists only with `queue_name: auto`, model+count required, roles must not override accelerators, public in the list requires run_options); `ResourceConfig.accelerator_choices()` pairs the model/count lists (scalar broadcast); `total_replicas`; constants `AUTO_QUEUE`/`LOCAL_QUEUE`; `normalize_queue_categories`. Status constants `TERMINAL_STATUSES`/`ACTIVE_STATUSES`/`STOPPABLE_STATUSES` (platform `Approving` and `Pending` are active and stoppable; `STOPPABLE_STATUSES == ACTIVE_STATUSES`); queue-category helpers `normalize_queue_category`/`queue_category_value`/`queue_category_alias`; image-region helpers (`normalize_image_region`, `IMAGE_REGION_VALUES`); `DETAIL_FIELDS` (per-node resource fields shared by resource_config and roles) and `JOB_CONFIG_FIELDS` (job fields copied into the advance config). Generic `_from_dict`/`_to_dict` collect unknown keys into `platform_overrides`. `Job.is_local` checks `queue_name == "local"`.

- **config.py** — `ConfigLoader` raises `ConfigError` for every problem. Loads YAML with `$CONFIG_DIR`/`$VAR` substitution and `$$` → `$` escaping at the raw text level before parsing (comment-only lines skipped). `jobs()` (expanded grid or the single job), `is_search`, `expand_grid()` and `build_single_job()` (both via `Job.from_dict`), rejecting removed keys (`experiment_id`, `experiment_name`, `submission`) and unknown job keys; `commands` lists are joined with newlines. Also `get_experiment_spec()`, `get_search_name()`, `get_scheduler_params()`. `{auto:Ns}` in job names produces a deterministic MD5-based hash.

- **platform.py** — `PlatformClient` wraps `airsctl` subprocess calls and handles snake_case documents only. `_run` (one retry on `Unauthenticated` after 35s); raw commands `experiment_list`, `experiment_modify`, `job_list`, `job_run(exp_id, config_name, run_options)`, `job_stop`, `job_cancel`; `get_job_info`/`get_job_status`; `run_config` (validates run_options for the category, raises if the output has no job ID); `get_experiment` (cached detail), `find_experiment` (UUID or exact name; caches detail), `experiment_configs`, `modify_configs` (full `advance_config_infos` replacement in one modify, cloned from the first existing config), `remove_configs` (best-effort); `_build_job_config` + `_apply_resource_config` (image, resolved location IDs, roles with inherited resource fields, overrides).

- **api.py** — `PlatformAPI` class. Direct REST calls to `https://platform-multi.baai.ac.cn` for what airsctl cannot do: token exchange (AK/SK + HMAC-SHA256), dedicated `queue/select`, shared `shared-queue/list`/`job-count`/`config`, experiment creation, `experiment/delete` (archive semantics), `job/select` filtered by experiment (`jrun experiment jobs`), `job-snapshots/select` (utilization), `projsets/select-joined` (the account's projsets), `resolve_project_context` (project_set_name/project_name → IDs), `find_queue`. Auth resolves in order: `AIRS_AK`/`AIRS_SK` env → `~/.jrun/auth.json` (`jrun login`) → `/etc/accesskey/{user-ak,user-sk}` (base64-encoded, k8s secret mount on jiuding pods) → `/tmp/.airs/.token` (airsctl's cache). Uses stdlib urllib with env proxies bypassed (`ProxyHandler({})`). Queue and shared queue endpoints are projset-scoped where required; `jrun queues` combines dedicated/public/idle card counts. `list_category_queues(category)` dispatches dedicated/shared listing; `shared_queue_budgets` returns remaining public/idle submit slots per mode (limit − running − queueing; None = unlimited). Module helpers: `compute_signature`, `queue_location_info`, `filter_queues` (named-location filter shared by `find_queue` and placement), `with_project_context`, `save_auth`.

- **project.py** — `Project` class. Manages `.jrun/` (`settings.json` is an empty `{}` marker) and the job tracker (`jobs.json`). `Project.init(directory=None)` takes no experiment/platform arguments; `Project` has no experiment or platform-ID attributes (each tracked job/search stores its own `experiment_id`/`experiment_name`). `_find_jrun_dir()` walks up the directory tree. Tracker operations: `record_search`, `record_single_job`, `find_job_by_name`, `remove_job`, `remove_search`, `update_job_status`, `sync_job_info`, `record_pending_jobs`. `extract_platform_ids` is a staticmethod; module function `build_job_url(job_id, platform, experiment_name)`. Uses `_LockedTracker` context manager with `fcntl` file locking.

- **formatter.py** — `StatusFormatter()` (no project argument; colored output, ANSI hyperlinks, job tables with links built per job via `project.build_job_url`) plus pure display helpers: `pct`, `format_ms`, `format_ms_long`, `snapshot_sort_key`, `colored_status`, `hyperlink`, `print_experiment_job_table` (the `experiment jobs` panel; mirrors the `job status` columns, LINK when job URLs can be built, else raw IDs).

- **scheduler.py** — `Scheduler` class. Run-only background daemon for `parallel_trials`: manages PID files, polls job statuses, and runs already-installed pending configs up to the parallel limit (dropping a replaced job's old mapping once its replacement starts). It never modifies the experiment. `parallel_trials <= 0` means no concurrency limit (used for deferred auto jobs). Before running a public/idle job it reads the remaining submit slots once per poll via `PlatformAPI.shared_queue_budgets` and skips the job while none is left (on read failure it just tries the run). `Scheduler(jrun_dir, search_name)` is enough for PID management (`is_running`/`stop`); status constants come from `models`.

- **errors.py** — Exception hierarchy: `JrunError` (base), `PlatformError` (airsctl failures), `ConfigError` (config loading/validation), `ApiError` (REST API failures).

- **template.py** — Contains `TEMPLATE_CONTENT`, the annotated YAML config template string.

## Error Handling Convention

All error handling follows a layered exception pattern:

1. **`errors.py` defines the exception hierarchy:**
   - `JrunError` — base exception for all jrun errors
   - `PlatformError(command, returncode, stderr, stdout)` — raised when `airsctl` subprocess calls fail
   - `ConfigError` — raised for config loading/validation errors (config.py converts YAML, variable, and model `ValueError`s into it)
   - `ApiError(url, status_code, message)` — raised when platform REST API calls fail

2. **`PlatformClient` (platform.py) raises exceptions, never logs:**
   - `_run(args, check=True)` raises `PlatformError` when `returncode != 0` and `check=True`
   - `_run` auto-retries once on `Unauthenticated` errors (35s delay) before raising
   - `modify_configs()` and `run_config()` raise `PlatformError` on platform failures; `run_config()` also raises when the `job run` output contains no job ID
   - Query methods like `get_job_status()` / `get_job_info()` use `check=False` and return `None` on failure
   - `remove_configs` uses `check=False` (best-effort cleanup)

3. **Service layer (`tracking.py`, `submission.py`):**
   - `release_job` is strict: it raises `PlatformError` when a still-active job cannot be stopped (an overwrite must not start a duplicate run). `submit_jobs` reports that per job via `err_echo` and skips it; `submit_single` lets it propagate (the submit aborts).
   - `stop_tracked_job` and `remove_tracked_jobs` are best-effort: explicit `except PlatformError: pass`.
   - Per-job run errors in batch submit are caught in `submission.submit_jobs`, printed via injected `err_echo`, and the next job continues.

4. **Command layer (commands/) catches and formats:**
   - Critical operations: `except (ConfigError, PlatformError, ApiError) as e: fail(e)` → `Error: <message>` on stderr + exit 1, never a traceback
   - Local jobs (`queue_name: local`): `jrun submit` exits with the command's exit code

5. **Scheduler (scheduler.py) catches and logs:**
   - `try/except PlatformError as e` → `self._log(f"Submit error: {e}")` + retry logic

**Rules for new code:**
- Never use `log_fn` callbacks for error reporting
- Never silently swallow errors with bare `return None` — either raise or explicitly `except PlatformError: pass` with clear intent
- `PlatformError.__str__()` produces a human-readable message like: `airsctl experiment list failed (rc=255): login expired`
- The command layer (`commands/`) is the only place that formats errors for the user (use `jrun.commands.fail`); service modules raise or take injected `echo`/`confirm` callbacks

## Job Submission Flow

0. Classify grid jobs (new/overwrite/skip) so only the jobs being submitted
   go on. Place `queue_name: auto` jobs (`placement.place_auto_jobs`): read
   free cards and public/idle submit slots, rewrite each into a concrete job.
   Jobs with no public/idle slot are "deferred" (grid: scheduler; single job:
   error).
1. Resolve the experiment named by the config's `experiment:` block (exact
   name; created via the REST API if missing) and read its detail once. The
   first existing `advance_config_infos` entry is retained as a structural
   template.
2. Expand the YAML and classify names as new, explicitly overwritten, or
   skipped. Only the first two categories form the current submission set.
3. For each overwritten name, `tracking.release_job` stops the old job if it is
   still active on the platform (local `queued-*` entries are only marked
   `Stopped`; platform `Pending`/`Approving` jobs are stopped like running
   ones; terminal jobs are untouched). Grid search: a job whose
   old run cannot be stopped is skipped with an error, the rest proceed.
   Single job: the stop failure aborts the submit.
4. Clone the template once per selected job and replace the entire
   `advance_config_infos` list with that set in one
   `airsctl experiment modify -f <tmp.json>` call.
5. Call `airsctl job run -e <id> -n <config_name>` once for each selected job.
   A run failure (including output without a job ID) is reported and does not
   prevent other selected jobs from being attempted.
6. Write successful IDs to `.jrun/jobs.json`. For an overwrite, remove the old
   local mapping only after its replacement starts; modify/run failure leaves
   the old mapping intact.
7. With `parallel_trials` or deferred auto jobs, all selected configs are
   installed in step 4; the scheduler later performs only step 5 for pending
   configs (gated by `parallel_trials` and public/idle submit slots) and drops
   a replaced job's old mapping once its replacement starts.

The experiment config is therefore the current YAML working set, not a job
history. Configs dropped from the list do not stop jobs that already started;
only overwritten names stop their previous run. `.jrun/jobs.json` is the
durable local source of job IDs and status. Do not share an experiment between
independent submitters unless full-list replacement is intended.

**Important:** `airsctl experiment modify` parses the JSON file with plain `encoding/json` (NOT protojson), so enum fields like `image_region` must be **integer** values (`PUBLIC=1`, `PRIVATE=2` — see `models.IMAGE_REGION_VALUES`) and `queue_category` too (`dedicated=0`, `public=1`, `idle=2`); string names are rejected. The REST API (`api.py`) is the opposite: it accepts enum names. airsctl `experiment list -e/-N` returns a bare snake_case document, so platform.py handles snake_case only.

## Config Format

Two mutually exclusive top-level workload keys (if both are present, `search:` wins):

- **`search:`** — Grid search with `job_template` (name, command/commands, envs, image, resource_config, run_options, ...), `params` (list of name/values), `sampling: grid`, `max_trials`, optional `parallel_trials` (initial batch plus run-only scheduler) and `poll_interval` (default 30s).
- **`job:`** — Single job with `name`, `command`/`commands`, `envs`, `image`, `resource_config`, ...

A top-level **`experiment:`** block with at least `name` is required for a real submit (`--dry-run` only warns; local jobs never reach it). On submit, jrun looks the experiment up by exact name via airsctl and creates it via the REST API (`PlatformAPI.create_experiment`) if missing, using the first job's image and resolved resource path for the placeholder config. The block also accepts job defaults (`image`, `image_region`, `queue_name`, `queue_category`, `project_set_name`, `project_name`, `region_name`, `zone_name`) and experiment-level settings (`description`, `train_frame`, `restart_policy`, `storage_info`, `native_cluster`, `profiler_info`, `model_storage_info`, `platform_overrides`). Job-level values win over experiment defaults (`models.job_target`). Resource IDs are resolved per distinct effective named path via `queue/select` or the shared queue API (`submission.resolve_queue_locations`), so different jobs in one config may use different queues.

Removed and rejected with a `ConfigError`: `search.experiment_id`/`experiment_name`, `job.experiment_id`/`experiment_name` (and the same in `job_template`), `submission:` (use `run_options:`), `resource_config.worker` (use `roles`), and the old resource ID/display-name fields. There is no project-level experiment binding; `.jrun/settings.json` is only a marker.

Unknown keys in `job:` / `search.job_template:` are rejected as typos; raw platform fields go under `platform_overrides:`. Unknown keys inside `resource_config`, roles, and `experiment` still pass through as platform overrides.

When submitting, if the target experiment already has ≥100 configs, jrun will warn and ask for confirmation before proceeding. `resource_config.queue_category` selects dedicated (default), public, or idle shared queues. `run_options` (`duration_hours`, `usage_desc`, `queue_priority_mode` normal|high, `enable_checkpoint`; unknown keys rejected) is required for public queues and forwarded to `airsctl job run` whenever set.

Auto placement (`resource_config.queue_name: auto`, also allowed as `experiment.queue_name`): `accelerator_model`/`accelerator_count` may be lists paired by position (a scalar repeats), `queue_category` may be a preference-ordered list; lists without `auto` are rejected. Location names filter candidates. Each job needs `count × total role replicas` cards. Order: (1) **start** on the first candidate (category pref, pair pref, most free cards) with enough free cards and, for public/idle, a remaining per-projset submit slot; (2) **queue** on dedicated if listed, else on a public/idle queue with a slot left; (3) **wait** (deferred) for a slot via the scheduler. Queues whose total is below the job's cards are never used. Model match is exact, case-insensitive. Placed jobs get `JRUN_QUEUE_NAME`/`JRUN_ACCELERATOR_MODEL`/`JRUN_ACCELERATOR_COUNT` envs. A deferred job's queue is fixed at submit time.

Shared queues accept exactly one priority (`models.CATEGORY_PRIORITY`: public `high`, idle `low`; platform `QueueCategoryPriorityMismatch` otherwise; dedicated is free). `_apply_resource_config` writes the required priority for public/idle jobs without one (the cloned template's priority would otherwise leak in), and `validate_job` rejects a conflicting explicit priority for any of the job's categories.

Remote job environment (verified 2026-11-24, see `docs/platform-api.md` §11): `envs:` are delivered as environment variables via `hyper_parameter`; the platform sets `AIRS_WORKLOAD_TYPE=experiment`, `AIRS_EXPERIMENT_ID`, `HOSTNAME=job-<job id>-master-0`, `MASTER_ADDR`/`MASTER_PORT`/`RANK`/`WORLD_SIZE`, the torchrun `PET_*` set (`PET_NPROC_PER_NODE=auto`), and `NVIDIA_VISIBLE_DEVICES`. There is no platform job-ID/queue/accelerator variable. Keep README, skill, and template in sync when this list changes.

Special cases:
- `resource_config.queue_name: local` → runs command locally via `subprocess.run(shell=True)` instead of submitting; `jrun submit` exits with its exit code
- `resource_config.roles` → node roles (`[{name: Master, replicas: 1}, {name: Worker, replicas: 3}]`); each role inherits the resource_config resource fields (`DETAIL_FIELDS`) and `resource_region`, its own values override, and it accepts `extra_label_info`/`platform_overrides`. Without roles the job is single-node (one Master); `resource_config.resource_region` applies to the master role.

Variable substitution (all resolved in `ConfigLoader` at the raw text level before YAML parsing, skipping comment-only lines): `$CONFIG_DIR` (jrun built-in), `$VAR` (local env, errors if undefined), `$$VAR` → `$VAR` (remote env passthrough), `{param}` (grid params), `{auto:Ns}` (MD5 hash). `commands:` list items are joined with newlines.
