TEMPLATE_CONTENT = """\
# ==========================================================================
# jrun Config Template
# ==========================================================================
# Usage:
#   jrun submit config/my_job.yaml            # submit jobs
#   jrun submit config/my_job.yaml --dry-run  # preview without submitting
#
# This file shows both workload types: "search" (grid search) and "job"
# (single job). Use one or the other in your actual config; when both are
# present, only "search" is used.
#
# Unknown keys in a job (job: or search.job_template:) are rejected as typos.
# Put raw platform fields that jrun does not model under platform_overrides:.
# ==========================================================================

# (optional) Human-readable description, not used by jrun itself.
description: My experiment description

# ==========================================================================
# Experiment (required for a real submit; --dry-run only warns if missing)
# On submit, jrun looks the experiment up by exact name and creates it via
# the platform API if missing (using the first job's image/queue for the
# placeholder config). Requires credentials (`jrun login`).
# Besides its name, the block holds defaults for jobs (image, image_region,
# queue and location names) and experiment-level settings (description,
# train_frame, restart_policy, mounts, ...). A job-level value always
# wins over the experiment default.
# On each submit, jrun replaces this experiment's config list with the selected
# current jobs; the local .jrun/jobs.json file retains job history and IDs.
# Do not share an experiment unless replacing its configs is intended.
# ==========================================================================
experiment:
  # (required) Experiment name.
  name: my_experiment

  # (optional) Defaults for jobs that do not set their own.
  # image: harbor.platform.baai-inner.ac.cn/library/pytorch-sshd:23.08
  # image_region: PUBLIC          # PUBLIC (default) or PRIVATE, case-insensitive
  # Queue names are resolved in this order:
  # project_set_name -> project_name -> region_name -> zone_name -> queue_name.
  # queue_category: dedicated     # dedicated (default), public, or idle
  # project_set_name: baai-safety
  # project_name: baai-safety_research
  # region_name: 北京-大兴
  # zone_name: 可用区A
  # queue_name: my-queue-name

  # (optional) Experiment-level settings.
  # description: Created by jrun
  # train_frame: PyTorch             # PyTorch/TensorFlow/Paddle/MPI/FlagScale
  # restart_policy: Never
  # Storage mounted into every job (default: all three, read-write).
  # Each value is rw, ro, or false (not mounted).
  # mounts:
  #   home: rw        # /home/<user>
  #   project: rw     # /share/project
  #   projset: rw     # /share/projset
  # native_cluster: default-cluster
  # profiler_info: {enabled: false, level: typical}
  # model_storage_info: {}
  # platform_overrides: {}            # raw experiment fields for new platform versions

# ==========================================================================
# Remote submission (optional)
# Jobs whose queue category (after auto placement) is listed here are sent
# to a jrun remote-submission service, which submits them with its own
# account (e.g. a key allowed to use the public pool); all other jobs are
# submitted locally as usual. The service checks your identity with your own
# credentials and that you can see the target project set and queue. Jobs
# sent there need project_set_name; their experiment is named
# "<your alias>-<experiment name>" on the service and mounts.home is off.
# `jrun job status/stop/remove` reach those jobs through the service.
# See docs/remote-service.md.
# ==========================================================================
# remote:
#   url: http://jrun-svc.example.com:8080   # or $JRUN_REMOTE_URL
#   categories: [public]                     # default: public only

# ==========================================================================
# Grid Search
# Expands all parameter combinations (cartesian product) and submits each
# as a separate job.
# ==========================================================================
search:
  # (optional) Search name for tracking via `jrun job status <name>`.
  # Auto-derived from job_template.name if omitted.
  name: my_search

  job_template:
    # (required) Job name template.
    # Use {param} to reference parameter values.
    # Use {auto:Ns} for a deterministic N-char MD5 hash based on params.
    # e.g.: eval_{model}_{data}_{auto:4s}
    name: job_{param1}_{param2}_{auto:4s}

    # (optional) Docker image jobs run with; overrides experiment.image and
    # supports {param} substitution.
    image: harbor.platform.baai-inner.ac.cn/library/pytorch-sshd:23.08

    # (optional) Image repo type: PUBLIC (default) or PRIVATE.
    # image_region: PRIVATE

    # (required, use "commands" or "command")
    # A list of commands joined with newlines (one shell line each), or a
    # single command string. Lines are not fail-fast: add "set -e" first or
    # chain with "&&" in one line when later lines must not run after a failure.
    # Supports {param} substitution and $$ escaping ($$VAR -> $VAR at runtime).
    commands:
      - set -e
      - cd /workspace
      - python train.py --param1 {param1} --param2 {param2}

    # (optional) Environment variables.
    # Values support {param} substitution and $$ escaping.
    envs:
      HOME: /home/user
      CUDA_VISIBLE_DEVICES: "0,1"
      MODEL_NAME: "{param1}"
      PATH: "$$PATH:/custom/bin"

    # (optional) airsctl job-run options. Public queues require duration_hours,
    # queue_priority_mode, and usage_desc. Unknown keys are rejected.
    # run_options:
    #   duration_hours: 12
    #   enable_checkpoint: false
    #   queue_priority_mode: normal    # normal or high
    #   usage_desc: benchmark

    # (optional) Further Jiuding config fields and raw passthrough.
    # train_frame: PyTorch
    # restart_policy: Never
    # code: ""
    # code_path: ""
    # git_prefix: ""
    # git_branch: ""
    # git_commit: ""
    # gitee: ""
    # dataset: {items: []}
    # model_storage_info: {}
    # model_storage_info_items: []
    # selected_nodes: []
    # platform_overrides: {}

    # (optional) Resource configuration; overrides experiment defaults.
    resource_config:
      # Set to "local" to run on local machine instead of submitting to platform,
      # or "auto" to let jrun pick the queue (see below).
      # See `jrun queues` for available queue names.
      queue_name: my-queue-name
      # dedicated (default), public, or idle. Public/idle use shared queues.
      # Public only accepts priority high and idle only low: omit priority for
      # shared queues and jrun sets the required value.
      queue_category: dedicated
      # project_set_name: baai-safety
      # project_name: baai-safety_research
      # region_name: 北京-大兴
      # zone_name: 可用区A

      # e.g.: low, medium, high
      priority: high

      # Automatic placement: set queue_name: auto and turn accelerator_model /
      # accelerator_count below into lists of acceptable pairs, matched by
      # position and in preference order (a scalar count applies to every model).
      # queue_category may then be a preference-ordered list. On submit jrun
      # reads free cards and public/idle submit slots and picks a queue per
      # job; `--dry-run` previews the placement. A FIFO queue that already has
      # jobs waiting is not used to "start now" even if it shows free cards
      # (see the WAITING column of `jrun queues`). Placed jobs get
      # JRUN_QUEUE_NAME / JRUN_ACCELERATOR_MODEL / JRUN_ACCELERATOR_COUNT envs
      # (use $$JRUN_ACCELERATOR_COUNT in commands). Location names above filter
      # the candidate queues; roles must not override the accelerator fields.
      # With public/idle in the list, remove `priority` (it is set per category).
      # queue_name: auto
      # queue_category: [public, idle]
      # accelerator_model: [NVIDIA_A100-SXM4-40GB, NVIDIA_V100-PCIE-32GB]
      # accelerator_count: [4, 8]

      # Per-node resources (inherited by every role below).
      # e.g.: J1_ADV_H1-SXM4-80GB, NVIDIA_A100-SXM4-40GB
      accelerator_model: J1_ADV_H1-SXM4-80GB
      accelerator_count: 1
      cpu_cores: 8
      mem_gib: 128
      shared_mem_gib: 64
      # rdma_shared_count: 0
      # accelerator_resource_name: nvidia.com/gpu
      # fb_mem_gib: 80
      # accelerator_mem_gib: 80
      # cpu_model: ""
      # resource_region: ACCELERATOR_SUITE   # applies to the master role
      # pod_restart_policy: Never
      # restart_scope: FailedInstanceOnly
      # selected_nodes: []
      # platform_overrides: {}

      # (optional) Multi-node: list the node roles. Each role inherits the
      # resource fields above (and resource_region); its own values override.
      # Without roles the job is single-node (one Master).
      # roles:
      #   - name: Master
      #     replicas: 1
      #   - name: Worker
      #     replicas: 3
      #     # accelerator_count: 2
      #     # resource_region: ACCELERATOR_SUITE
      #     # extra_label_info: {}
      #     # platform_overrides: {}

  # (required) Sampling method. Currently only "grid" (cartesian product).
  sampling: grid

  # (optional, default 9999) Max parameter combinations.
  max_trials: 100

  # (optional) Max concurrent jobs on the platform. When set, submit runs the
  # first N jobs immediately and a background scheduler runs the rest as
  # slots open up.
  parallel_trials: 4

  # (optional, default 30) Polling interval in seconds for the scheduler daemon.
  poll_interval: 30

  # (required) Parameter list. All params are crossed (cartesian product).
  params:
    - name: param1
      values: ["value_a", "value_b"]
    - name: param2
      values: [1, 2, 3]

# ==========================================================================
# Single Job
# Submit one job directly.
# ==========================================================================
job:
  # (required) Job name.
  name: my_single_job

  # (optional) Docker image the job runs with; overrides experiment.image.
  image: harbor.platform.baai-inner.ac.cn/library/pytorch-sshd:23.08

  # (optional) Image repo type: PUBLIC (default) or PRIVATE.
  # image_region: PRIVATE

  # (required, use "commands" or "command")
  # A list of commands joined with newlines, or a single command string.
  commands:
    - cd /workspace
    - torchrun --nnodes=2 --nproc_per_node=1 train.py

  # (optional) Environment variables.
  envs:
    HOME: /home/user
    CUDA_VISIBLE_DEVICES: "0"

  # (optional) airsctl job-run options. Required fields for public queues are
  # duration_hours, queue_priority_mode, and usage_desc.
  # run_options:
  #   duration_hours: 12
  #   enable_checkpoint: false
  #   queue_priority_mode: normal
  #   usage_desc: benchmark

  # (optional) Further Jiuding config fields; same as search.job_template.
  # train_frame: PyTorch
  # restart_policy: Never
  # selected_nodes: []
  # platform_overrides: {}

  # (optional) Resource configuration. Same fields as search.job_template.resource_config.
  resource_config:
    queue_name: my-queue-name
    queue_category: dedicated  # public or idle for shared queues
    # project_set_name: baai-safety
    # project_name: baai-safety_research
    # region_name: 北京-大兴
    # zone_name: 可用区A
    priority: high
    accelerator_model: J1_ADV_H1-SXM4-80GB
    accelerator_count: 1
    cpu_cores: 8
    mem_gib: 128
    shared_mem_gib: 64
    # Two nodes: one Master plus one Worker, both with the resources above.
    roles:
      - name: Master
        replicas: 1
      - name: Worker
        replicas: 1

# ==========================================================================
# Variable Reference
# ==========================================================================
# $CONFIG_DIR  -> directory containing this config file (jrun built-in)
# $VAR         -> resolved at submit time from local environment variables.
#                 Errors if undefined. Comment-only lines are not expanded.
# $$VAR        -> becomes $VAR in the submitted command (remote env reference)
# {param}      -> parameter value from search grid
# {auto:Ns}    -> deterministic N-char MD5 hash based on parameters
#
# Remote job environment (reference as $$VAR):
#   platform: AIRS_WORKLOAD_TYPE=experiment, AIRS_EXPERIMENT_ID,
#             HOSTNAME=job-<job id>-master-0, MASTER_ADDR, MASTER_PORT, RANK,
#             WORLD_SIZE, PET_NNODES, PET_NODE_RANK, PET_WORLD_SIZE,
#             PET_MASTER_ADDR, PET_MASTER_PORT, PET_NPROC_PER_NODE=auto
#             (plain `torchrun` uses every visible GPU), NVIDIA_VISIBLE_DEVICES
#   jrun:     every key under envs:, plus JRUN_QUEUE_NAME,
#             JRUN_ACCELERATOR_MODEL, JRUN_ACCELERATOR_COUNT on auto jobs
"""
