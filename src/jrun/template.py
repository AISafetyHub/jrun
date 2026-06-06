TEMPLATE_CONTENT = """\
# ==========================================================================
# jrun Config Template
# ==========================================================================
# Usage:
#   jrun submit config/my_job.yaml            # submit jobs
#   jrun submit config/my_job.yaml --dry-run  # preview without submitting
#
# This file shows both config types: "search" (grid search) and "job"
# (single job). Use one or the other in your actual config.
# ==========================================================================

# (optional) Human-readable description, not used by jrun itself.
description: My experiment description

# ==========================================================================
# Grid Search
# Expands all parameter combinations (cartesian product) and submits each
# as a separate job.
# ==========================================================================
search:
  # (optional) Search name for tracking via `jrun status <name>`.
  # Auto-derived from job_template.name if omitted.
  name: my_search

  # (optional) Override the experiment to submit to. If omitted, uses
  # the default from `jrun init`.
  # experiment_id: "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
  # experiment_name: my_other_experiment

  job_template:
    # (required) Job name template.
    # Use {param} to reference parameter values.
    # Use {auto:Ns} for a deterministic N-char MD5 hash based on params.
    # e.g.: eval_{model}_{data}_{auto:4s}
    name: job_{param1}_{param2}_{auto:4s}

    # (required, use "commands" or "command")
    # List of commands joined with "&&", or a single command string.
    # Supports {param} substitution and $$ escaping ($$VAR -> $VAR at runtime).
    commands:
      - cd /workspace
      - python train.py --param1 {param1} --param2 {param2}

    # (optional) Environment variables.
    # Values support {param} substitution and $$ escaping.
    envs:
      HOME: /home/user
      CUDA_VISIBLE_DEVICES: "0,1"
      MODEL_NAME: "{param1}"
      PATH: "$$PATH:/custom/bin"

    # (optional) Resource configuration, overrides experiment template defaults.
    resource_config:
      # Set to "local" to run on local machine instead of submitting to platform.
      queue_name: my-queue-name

      # e.g.: low, medium, high
      priority: high

      # e.g.: J1_ADV_H1-SXM4-80GB, A100-SXM4-40GB
      accelerator_model: J1_ADV_H1-SXM4-80GB
      accelerator_count: 1
      cpu_cores: 8
      mem_gib: 128
      shared_mem_gib: 64

      # (optional) Multi-node: add worker replicas. Omit for single-node.
      # Resource fields below are optional; they inherit from master if omitted.
      worker:
        replicas: 1
        # accelerator_model: J1_ADV_H1-SXM4-80GB
        # accelerator_count: 1
        # cpu_cores: 8
        # mem_gib: 32
        # shared_mem_gib: 16

  # (required) Sampling method. Currently only "grid" (cartesian product).
  sampling: grid

  # (optional, default 9999) Max parameter combinations.
  max_trials: 100

  # (optional) Max concurrent jobs on the platform. When set, submit sends the
  # first N jobs immediately and a background scheduler submits the rest as
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

  # (optional) Override the experiment to submit to. If omitted, uses
  # the default from `jrun init`.
  # experiment_id: "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
  # experiment_name: my_other_experiment

  # (required, use "commands" or "command")
  # List of commands joined with "&&", or a single command string.
  commands:
    - cd /workspace
    - python evaluate.py --model llama3_8b

  # (optional) Environment variables.
  envs:
    HOME: /home/user
    CUDA_VISIBLE_DEVICES: "0"

  # (optional) Resource configuration. Same fields as search.job_template.resource_config.
  resource_config:
    queue_name: my-queue-name
    priority: high
    accelerator_model: J1_ADV_H1-SXM4-80GB
    accelerator_count: 1
    cpu_cores: 8
    mem_gib: 128
    shared_mem_gib: 64
    worker:
      replicas: 1

# ==========================================================================
# Variable Reference
# ==========================================================================
# $CONFIG_DIR  -> directory containing this config file (jrun built-in)
# $VAR         -> resolved at submit time from local environment variables.
#                 Errors if undefined.
# $$VAR        -> becomes $VAR in the submitted command (remote env reference)
# {param}      -> parameter value from search grid
# {auto:Ns}    -> deterministic N-char MD5 hash based on parameters
"""
