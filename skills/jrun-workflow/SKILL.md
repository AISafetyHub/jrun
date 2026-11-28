---
name: jrun-workflow
description: Use this skill when a user asks to create, edit, explain, validate, preview, submit, monitor, stop, remove, or troubleshoot jrun jobs and YAML configurations for airsctl, including single jobs, grid searches, multi-node jobs, local runs, automatic queue placement by free cards (queue_name: auto), experiments, queues, and tracked job lifecycle operations. Also trigger for Chinese requests such as 创建、提交、查询、停止或删除 jrun 任务, 自动选队列/哪里有空卡.
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
  a multi-node job (`resource_config.roles`), or a local debug run.
- **Preview or validate**: find the config and use dry-run output. Dry-run
  never changes platform state; it makes no platform call unless the config
  uses `queue_name: auto`, in which case it reads queue capacity and submit
  slots (read-only) to preview the placement.
- **Submit or experiment management**: identify the experiment, image, queue,
  credentials, and intended number of jobs. When the user accepts several
  accelerator models or categories, or asks to use "whatever has free cards",
  use `queue_name: auto` (section 3) instead of picking one queue yourself.
- **Observe or inspect**: use tracked-job status, experiment listing, queue
  capacity, or GPU usage.
- **Public pool without public permission**: if the user's account cannot
  submit to the public pool but a jrun remote-submission service is
  available, add a top-level `remote:` block (see "Remote submission" below);
  do not ask for or use someone else's AK/SK.
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

`jrun init` creates `.jrun/` (its `settings.json` is only an empty marker);
it does not choose an experiment. Every remote submit needs a top-level
`experiment:` block with at least `name:` in the config. The legacy
`experiment_id`/`experiment_name` keys under `job:`/`search:` are removed and
rejected; there is no project-level default experiment.

List existing experiments or queues when needed:

```bash
jrun experiment list
jrun experiment jobs <experiment-name-or-id>   # platform-side jobs under one experiment
jrun queues
jrun queues --category public                  # dedicated/public/idle, repeatable
jrun queues --ids
```

`jrun queues` lists each shared pool under both `public` and `idle` (same pool,
different submission mode and RUN/LIMIT). With `queue_name: auto` there is no
need to choose a queue from this list first; use it to explain a placement
(compare the FREE, RUN/LIMIT, and WAITING columns). WAITING counts jobs
already queued ahead of a new one; a queue with free cards but WAITING > 0
will not start a new job right away (FIFO scheduling). `--proj-id`/`--projset-id` on
`queues`, `gpu-usage`, `experiment jobs`, and `experiment delete` default to no
project and an auto-detected joined project set.

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

A top-level `experiment:` block is required for a real submit (`--dry-run`
only warns when it is missing). jrun finds the experiment by exact name or
creates it. Only `name` is required. Optional job defaults are `image`,
`image_region`, `project_set_name`, `project_name`, `region_name`,
`zone_name`, `queue_name`, and `queue_category`; experiment-level settings
include `description`, `train_frame`, `restart_policy`, and `mounts`
(`{home, project, projset}`: `rw`/`ro`/`false`, default all `rw`; raw
`storage_info` is rejected).
Job-level values always override the experiment defaults. When the experiment
must be created, jrun builds the placeholder config from the first job's image
and resolved resource names.

Keep resource settings under `resource_config`; its `queue_name` is resolved to
project, region, zone, and queue IDs via the platform API (this needs credentials) and
overrides the experiment defaults. Set `queue_category: public` or `idle` for
shared queues (their `priority` is fixed: public only `high`, idle only
`low`; leave `priority` unset and jrun writes the required value), and use `run_options` for public queue duration, priority mode,
usage description, and checkpointing. `region_name` and `zone_name` can be set
per job. Set `queue_name: local` only for local debugging. A local grid run executes
only its first expanded job, so prefer a single `job:` when validating one
command locally. For multi-node jobs list the node roles in
`resource_config.roles` (e.g. `[{name: Master, replicas: 1}, {name: Worker,
replicas: 3}]`); each role inherits the `resource_config` resource fields and
may override them. `resource_config.worker` is removed and rejected.

Unknown keys in `job:` or `search.job_template:` are rejected as typos; put raw
platform fields under `platform_overrides:`. `run_options` accepts only
`duration_hours`, `queue_priority_mode` (`normal`/`high`), `usage_desc`, and
`enable_checkpoint` (the old `submission:` alias is rejected).

Use `commands` for a newline-separated list or `command` for one command.
Because newline-separated shell commands are not inherently fail-fast, use one
command with `&&` or begin with `set -e` when later commands must not run after
a failure.

For portable field rules and examples, read
[references/config-patterns.md](references/config-patterns.md). Replace every
placeholder image, queue, command, and resource value before real submission.

#### Automatic queue placement

Set `resource_config.queue_name: auto` to let jrun pick a queue per job from
current capacity. `accelerator_model` and `accelerator_count` become lists
paired by position (a scalar on either side applies to every entry), and
`queue_category` may be a list; list order is preference order. Location
names (`project_set_name`, `project_name`, `region_name`, `zone_name`) filter
the candidate queues. Lists require `queue_name: auto`; roles of an auto job
must not override the accelerator fields; a job's cards are
`accelerator_count` x total role replicas. If `public` is listed,
`run_options` must satisfy public-queue requirements. Queues with jobs
already waiting ahead (FIFO) are not used to start a job immediately, even
when they show free cards. Details, placement
rules, and an example are in
[references/config-patterns.md](references/config-patterns.md).

When writing an auto config:

- Do not add `idle` to the category list unless the user accepts that idle jobs
  can be preempted.
- Different models have different memory and card counts. Tell the user the
  command must adapt instead of hard-coding the card count: a plain `torchrun`
  (no `--nproc_per_node`) already uses every visible GPU because the platform
  sets `PET_NPROC_PER_NODE=auto`; other programs can read the jrun-injected
  `$$JRUN_ACCELERATOR_COUNT`, `$$JRUN_ACCELERATOR_MODEL`, `$$JRUN_QUEUE_NAME`.
- `cpu_cores`/`mem_gib`/`shared_mem_gib` are shared by every pair; size them for
  the largest pair and say so.

#### Remote submission

A top-level `remote:` block sends some jobs to a jrun remote-submission
service, which submits them with its own account (typically one allowed to use
the public pool):

```yaml
remote:
  url: https://jrun-svc.example.com   # ask the user; never guess the URL
  categories: [public]                # default: only public jobs go remote
```

- Routing happens after auto placement: a job whose final category is listed
  goes remote, every other job is submitted locally. Dry-run shows a `Route:`
  line per job; show it to the user.
- Remote jobs need `project_set_name` (job or experiment level). The service
  checks the user's own token: the account must be active, must have joined
  that project set, and must see the target pool as idle.
- The service only accepts a restricted config. It rejects
  `platform_overrides`, `code`/`git_*`, `dataset`, `model_storage_*`, and
  `image_region: PRIVATE`. It names the experiment `<alias>-<name>` and turns
  `mounts.home` off. Expect remote jobs not to see the user's home directory;
  keep code and data under project or projset storage.
- Remote jobs are tracked with the service URL. `jrun job status/stop/remove`
  work on them through the service; the user's own key cannot stop them
  directly on the platform.

### 4. Preview and validate

For every new or changed config, run:

```bash
jrun submit path/to/config.yaml --dry-run
```

Check the rendered job names, parameter combinations, commands, environment
keys, resources, roles and replicas, experiment, image, queue, and total job
count. Config errors print one `Error: ...` line and exit 1.
Dry-run must not create an experiment, submit a job, modify a platform
experiment, or change `.jrun/jobs.json`.

For `queue_name: auto`, dry-run prints an "Auto placement" table (job →
category, queue, model x count, and whether it starts now, is queued on the
platform, or waits for a public/idle slot). Show this table to the user and
explain that it is a snapshot: the real submit places again with the capacity
at that moment, so the result can differ. Jobs that wait are started later by
the background scheduler; point out its log path after submit. If dry-run
warns that queue capacity cannot be read, fix credentials before submitting.

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

Overwriting stops the old run: jrun stops each replaced job that is still
active on the platform before installing the new configs (scheduler-held
`queued-*` entries are only marked `Stopped` locally; platform jobs in
`Pending` or `Approving` are stopped like running ones). Tell the user which
running jobs will be stopped before confirming an overwrite. In a grid, a job
whose old run cannot be stopped is skipped with an error while the rest
proceed; for a single job a stop failure aborts the submit.

Use `--status` only when the user specifies which existing statuses may be
replaced. Do not add `-y` automatically; it suppresses jrun's experiment
creation prompt. For a local config, confirm the command and working directory,
then run the same submit command without silently changing it.

For `queue_name: auto`, report the placement jrun printed during the real
submit (queue and model x count per job), not the dry-run snapshot. If a single
`job:` is refused because no public/idle submit slot is left, offer the user
two options: add `dedicated` to `queue_category`, or resubmit after a running
job finishes. Do not change the categories on your own.

After execution, report submitted, skipped, and failed counts; job IDs or
names; experiment identity; scheduler PID/log path if any; and the next
read-only status command. Preserve successful batch results when individual
jobs fail.

`jrun submit` treats the target experiment's config list as a current working
set. One successful submit replaces the whole `advance_config_infos` list
with the selected jobs (new jobs plus explicitly confirmed/`--overwrite`
jobs); skipped jobs and configs from earlier submits are omitted. Before an
overwrite, jrun stops the old non-terminal platform job. The local
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
jrun job status -n 0          # all entries (default shows 10)
```

Use `jrun gpu-usage` only when cluster utilization is requested and the user
has the required administrator access. Apply cluster, zone, owner, status,
`--all`, `--include-cpu`, sort, and row-limit options only as needed.

Before stopping, refresh status, show the exact target, and ask for
confirmation:

```bash
jrun job stop <job-name-or-id>
jrun job stop <search-name>            # stoppable jobs in the search + its scheduler
jrun job stop -s Running               # all tracked jobs in a stoppable status
```

Before removal, warn that `jrun job remove` stops still-active remote jobs
(waiting up to 60s for them to end), archives them, removes their configs from
each job's own experiment, and deletes their local tracking entries. A job that
is still active after the wait is reported and stays tracked; rerun the remove
once it has stopped.
Confirm whether the target is one job, a whole search, or a status-filtered
subset:

```bash
jrun job remove <job-name-or-id>
jrun job remove <search-name>
jrun job remove <search-name> -s Failed
```

`jrun experiment jobs <name-or-id>` lists the platform-side jobs under one
experiment, including jobs not tracked by this project; use it when the user
asks about an experiment's full job list rather than tracked submissions.
`jrun experiment delete <name-or-id>` deletes (archives) a whole experiment
on the platform — always confirm with the user first, warn that this is
platform-side archive semantics, and never add `-y` automatically.

Jobs waiting for a public/idle submit slot appear as `Queued` in
`jrun job status`; the scheduler starts them when a slot frees up. Their
queue was fixed at submit time and is not re-placed later.

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

Remote jobs see these variables (verified on a real single-node job; write
them as `$$VAR` in the YAML so they expand remotely):

| Variable | Source | Meaning |
|---|---|---|
| `AIRS_WORKLOAD_TYPE` | platform | `experiment` in jobs, `workspace` in dev workspaces |
| `AIRS_EXPERIMENT_ID` | platform | Experiment UUID |
| `HOSTNAME` | platform | `job-<job UUID>-master-0`; the only source of the job ID |
| `MASTER_ADDR` / `MASTER_PORT` / `RANK` / `WORLD_SIZE` | platform | Distributed rendezvous (single node: rank `0`, world size `1`) |
| `PET_NNODES` / `PET_NODE_RANK` / `PET_NPROC_PER_NODE` / `PET_WORLD_SIZE` / `PET_MASTER_ADDR` / `PET_MASTER_PORT` | platform | `torchrun` defaults; `PET_NPROC_PER_NODE=auto` = one process per visible GPU |
| `NVIDIA_VISIBLE_DEVICES` | platform | Visible GPUs |
| keys under `envs:` | jrun | Set as environment variables |
| `JRUN_QUEUE_NAME` / `JRUN_ACCELERATOR_MODEL` / `JRUN_ACCELERATOR_COUNT` | jrun | Only on `queue_name: auto` jobs |

Do not invent other platform variables: there is no job-ID, queue, or
accelerator variable from the platform, and image variables (`CUDA_VERSION`,
`HOME`, ...) change with the image. Multi-node `RANK`/`PET_*` values are not
verified yet; say so when a command depends on them.

Do not interpolate secrets into job names or print resolved secret values.
Quote YAML strings containing colons, hashes, dollar signs, braces, or shell
operators. Prefer environment entries over credentials embedded in commands.

## Troubleshooting

- **`jrun` not found**: use the project's `uv run jrun` environment or explain
  that jrun must be installed; do not silently install it.
- **No `.jrun/` directory**: run `jrun init` in the intended project root.
- **No experiment**: add a top-level `experiment:` block with `name:`; do not
  guess the name. Experiment names are matched exactly.
- **Removed or unknown field** (`worker`, `submission`, `experiment_id`,
  `experiment_name`, a misspelled job key): migrate to `roles`, `run_options`,
  the `experiment:` block, or `platform_overrides:` as the error says.
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
  retry only after addressing its cause. A `job run` whose output contains no
  job ID is reported as an error for that job.
- **`no <category> queue offers <model> xN`** (auto placement): model names
  must match exactly (case-insensitive); compare with the MODEL column of
  `jrun queues`, check the count against the queue size, or relax the location
  filters.
- **Accelerator lists differ in length / lists require `queue_name: auto`**:
  fix the config as the error says.
- **No public/idle submit slot left** (single auto job): see RUN/LIMIT in
  `jrun queues`; offer adding `dedicated` or waiting, as in section 5.
- **Old job could not be stopped on overwrite**: check it with
  `jrun job status <name>`, stop it explicitly, then resubmit.
- **Remote service errors**: HTTP 401 means the user's own credentials are
  invalid or expired. HTTP 403 explains which check failed: project set not
  served or not joined, pool not visible, or a field that is not allowed.
  Fix the config or ask the service operator; do not work around it. If the
  service is unreachable, status shows a warning and remote jobs keep their
  last known status.
- **Running jrun outside a jiuding pod**: set
  `JRUN_AIRSCTL_GATEWAY=http://platform-multi.baai.ac.cn` so airsctl goes
  through the public gateway with the job's project context.

## Completion checklist

Verify that:

1. the config is in the requested path and contains no credentials;
2. dry-run output matches the requested job count and resources (and, for
   `queue_name: auto`, the placement table was shown to the user);
3. every side-effecting command was explicitly confirmed;
4. submitted jobs/searches and their IDs or names are reported;
5. the user has a precise status, stop, or cleanup command for the next step.
