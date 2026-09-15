---
name: jrun-workflow
description: Use this skill when a user asks to create, edit, explain, validate, preview, submit, monitor, stop, remove, or troubleshoot jrun jobs and YAML configurations for airsctl, including single jobs, grid searches, multi-node jobs, local runs, experiments, queues, and tracked job lifecycle operations. Also trigger for Chinese requests such as 创建、提交、查询、停止或删除 jrun 任务.
---

# jrun Workflow

Turn a workload request into a safe, reproducible jrun configuration and manage
its lifecycle. Keep the workflow self-contained: use the bundled reference for
configuration details and the installed `jrun` executable for live help and
previews. Do not require the repository that originally contained this skill.

## Operating contract

- Treat the user's explicit instructions as higher priority than this skill.
- Inspect before editing or running anything. Preserve existing configs,
  `.jrun/` tracking data, and unrelated user changes.
- Start with read-only checks and `jrun submit <config> --dry-run` whenever a
  configuration can be previewed.
- Before remote experiment creation, job submission, overwrite, stop, removal,
  or local command execution, show the exact command, target, job count, queue,
  image, and likely side effects, then ask for confirmation.
- Do not hide confirmation behind `--yes` or `--overwrite`.
- Never print, copy, or commit AK/SK values, access tokens, or secret
  environment-variable values. Redact secrets in summaries and logs.
- Do not invent queue names, experiment IDs, image names, resource limits, or
  platform URLs. Ask for missing values or inspect available queues.

## Workflow

### 1. Classify the request

Identify the requested operation and its scope:

- **Create or edit**: determine whether the user needs one `job`, a `search`,
  a multi-node worker, or a local debug run.
- **Preview or validate**: find the config and use dry-run output; do not make
  a platform call.
- **Submit or experiment management**: identify the experiment, image, queue,
  credentials, and intended number of jobs.
- **Observe or inspect**: use tracked-job status, experiment listing, queue
  capacity, or GPU usage.
- **Stop or remove**: resolve the exact job/search and explain the cleanup
  scope before asking for confirmation.

If the request is ambiguous, ask for the smallest missing decision, such as
queue, image, experiment, or whether a grid is intended.

### 2. Run preflight checks

Run these checks in the intended project directory:

```bash
command -v jrun
jrun --help
```

If `jrun` is not on `PATH`, look for a project-managed invocation such as
`uv run jrun`. Do not install packages without telling the user first.

For a new jrun project, initialize tracking once:

```bash
jrun init
```

`jrun init` creates `.jrun/`; it does not choose an experiment. For remote
operations, resolve the experiment from one of these sources:

1. a top-level `experiment:` block in the config;
2. `experiment_id` or `experiment_name` in the `job` or `search` section;
3. an experiment already recorded in `.jrun/settings.json`.

List existing experiments or queues when needed:

```bash
jrun experiment list
jrun experiment jobs <experiment-name-or-id>   # platform-side jobs under one experiment
jrun queues
jrun queues --ids
```

Use `jrun login` only when credentials are needed and unavailable. Credentials
may also come from `AIRS_AK`/`AIRS_SK`, `~/.jrun/auth.json`, a platform-pod
access-key mount, or the airsctl token cache. Never inspect or echo the secret
values themselves.

Run jrun/airsctl subprocesses with `http_proxy` and `https_proxy` unset in
the documented jiuding environment; scope the change to the command or current
shell and do not alter the user's shell profile.

### 3. Build or update the configuration

Use `jrun template` as the live schema source when available:

```bash
jrun template
jrun template -f config/my_job.yaml
```

Use exactly one top-level workload section:

- `job:` for one run.
- `search:` with `job_template`, `params`, and `sampling: grid` for a
  Cartesian parameter search. Each combination becomes one tracked job.

Use a top-level `experiment:` block when the config should find or create an
experiment by name. Only `name` is required — on the platform an experiment is
just a container for job configs. Optional `description`, project/project-set
IDs, and cluster/zone IDs make creation and resolution explicit. The Docker
image and queue are job-level fields: set `image:` (and optional
`image_region: PRIVATE`) on `job:` or `search.job_template:`, and the queue via
`resource_config.queue_name`. When the experiment must be created, jrun builds
the placeholder config from the first job's image/queue. The deprecated
`experiment.image`/`image_region`/`queue_name` fields still work as fallbacks
for jobs that don't set them, but jrun prints a deprecation warning.

Keep resource settings under `resource_config`; its `queue_name` is resolved to
queue/cluster/zone IDs via the platform API (this needs credentials) and
overrides the experiment template. Set `queue_name: local` only for local
debugging. A local grid run executes only its first expanded job, so prefer a
single `job:` when validating one command locally. Add `worker.replicas` and
optional worker resource overrides for multi-node jobs.

Use `commands` for a newline-separated list or `command` for one command.
Because newline-separated shell commands are not inherently fail-fast, use one
command with `&&` or begin with `set -e` when later commands must not run after
a failure.

For portable field rules and examples, read
[references/config-patterns.md](references/config-patterns.md). Replace every
placeholder image, queue, command, and resource value before real submission.

### 4. Preview and validate

For every new or changed config, run:

```bash
jrun submit path/to/config.yaml --dry-run
```

Check the rendered job names, parameter combinations, commands, environment
keys, resources, worker count, experiment, image, queue, and total job count.
Dry-run must not create an experiment, submit a job, modify a platform
experiment, or change `.jrun/jobs.json`.

For a grid search, verify the Cartesian count and `max_trials`. If
`parallel_trials` is set, explain that jrun submits the initial batch and a
background scheduler handles the remaining jobs; retain the scheduler log path
shown by jrun.

Successful YAML parsing is not proof that a remote job is safe. Check command
paths, environment expansion, resource feasibility, and queue intent.

### 5. Execute only after confirmation

After presenting the preview, ask for confirmation immediately before the
side-effecting command. Use the narrowest applicable command:

```bash
# Submit a previously previewed config.
jrun submit path/to/config.yaml

# Create only the experiment described by the config.
jrun experiment create path/to/config.yaml

# Overwrite jobs only when replacement was explicitly requested.
jrun submit path/to/config.yaml --overwrite
```

Use `--status` only when the user specifies which existing statuses may be
replaced. Do not add `-y` automatically; it suppresses jrun's experiment
creation prompt. For a local config, confirm the command and working directory,
then run the same submit command without silently changing it.

After execution, report submitted, skipped, and failed counts; job IDs or
names; experiment identity; scheduler PID/log path if any; and the next
read-only status command. Preserve successful batch results when individual
jobs fail.

`jrun submit` treats the target experiment's config list as a current working
set. One successful submit replaces the whole `advance_config_infos` list
with the selected jobs (new jobs plus explicitly confirmed/`--overwrite`
jobs); skipped jobs and configs from earlier submits are omitted. This modify
does not stop or cancel already-started platform jobs. The local
`.jrun/jobs.json` tracker remains the source of historical IDs and statuses;
an overwrite changes that mapping only after its replacement has started.
When `parallel_trials` is enabled, the initial submit installs every selected
config and the scheduler later runs pending configs without another modify.
Warn the user before targeting an experiment shared with other submitters,
because its config list will be replaced.

### 6. Observe and manage lifecycle

Use tracked status first:

```bash
jrun job status
jrun job status <search-name-or-job-name-or-id>
jrun job status -s Running
jrun job status <search-name> -s Failed -s Stopped
```

Use `jrun gpu-usage` only when cluster utilization is requested and the user
has the required administrator access. Apply cluster, zone, owner, status,
`--all`, `--include-cpu`, sort, and row-limit options only as needed.

Before stopping, refresh status, show the exact target, and ask for
confirmation:

```bash
jrun job stop <job-name-or-id>
jrun job stop <search-name>
```

Before removal, warn that `jrun job remove` cancels applicable remote jobs,
removes their experiment configs, and deletes their local tracking entries.
Confirm whether the target is one job, a whole search, or a status-filtered
subset:

```bash
jrun job remove <job-name-or-id>
jrun job remove <search-name>
```

`jrun experiment jobs <name-or-id>` lists the platform-side jobs under one
experiment, including jobs not tracked by this project; use it when the user
asks about an experiment's full job list rather than tracked submissions.
`jrun experiment delete <name-or-id>` deletes (archives) a whole experiment
on the platform — always confirm with the user first, warn that this is
platform-side archive semantics, and never add `-y` automatically.

Do not manually edit `.jrun/jobs.json` for a normal lifecycle operation.

## Variable and configuration rules

Apply substitutions exactly as jrun does:

| Syntax | Meaning |
|---|---|
| `$CONFIG_DIR` | Absolute directory containing the config file. |
| `$VAR` | Resolve from the local environment; fail if undefined. |
| `$$VAR` | Escape one dollar sign so the submitted command sees `$VAR`. |
| `{param}` | Replace with the current grid-search parameter value. |
| `{auto:Ns}` | Deterministic MD5-derived suffix of `N` characters. |

Do not interpolate secrets into job names or print resolved secret values.
Quote YAML strings containing colons, hashes, dollar signs, braces, or shell
operators. Prefer environment entries over credentials embedded in commands.

## Troubleshooting

- **`jrun` not found**: use the project's `uv run jrun` environment or explain
  that jrun must be installed; do not silently install it.
- **No `.jrun/` directory**: run `jrun init` in the intended project root.
- **No experiment**: add `experiment:` or an explicit
  `experiment_id`/`experiment_name`; do not guess.
- **Authentication failure**: check credential availability and expiry without
  printing values; use `jrun login` if the user can provide AK/SK.
- **Queue unavailable**: run `jrun queues`, verify project/project-set scope,
  and ask the user to choose.
- **`queue/select` 403 permission denied**: the endpoint is projset-scoped.
  jrun auto-queries every projset the account has joined when no `projset_id`
  is set, so this usually means expired or missing credentials — re-run
  `jrun login`; on platform pods check the `/etc/accesskey` mount exists.
- **Experiment creation fails on `creatorId`**: the platform requires the
  numeric user ID in the request; jrun fills it from `userinfo` automatically,
  so this indicates an auth/account problem — re-login.
- **Proxy/airsctl failure**: retry with `http_proxy` and `https_proxy` unset
  for that command.
- **Undefined variable or YAML error**: identify the exact field, fix it, and
  rerun dry-run.
- **Duplicate job name**: show the existing status and offer skip or explicit
  overwrite; never overwrite implicitly.
- **Experiment config-count warning**: surface it and ask whether to continue;
  the next submit replaces the list with the current selected working set.
- **Pending grid jobs**: inspect `jrun job status <search>` and the scheduler
  log before changing or removing the search.
- **Platform command failure**: preserve the error text, redact secrets, and
  retry only after addressing its cause.

## Completion checklist

Verify that:

1. the config is in the requested path and contains no credentials;
2. dry-run output matches the requested job count and resources;
3. every side-effecting command was explicitly confirmed;
4. submitted jobs/searches and their IDs or names are reported;
5. the user has a precise status, stop, or cleanup command for the next step.
