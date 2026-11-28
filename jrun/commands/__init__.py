"""Click command registration and shared command-layer helpers.

Each module defines thin command functions (argument parsing + service calls +
error formatting). This package is the only layer that prints errors.
"""

import json

import click

from jrun.project import Project
from jrun.tracking import plain_progress, refresh_active_statuses, sync_remote_jobs


def fail(message):
    """Print ``Error: <message>`` to stderr and exit 1."""
    click.echo(f"Error: {message}", err=True)
    raise SystemExit(1)


def open_project() -> Project:
    """The enclosing jrun project, or a clean error if there is none."""
    try:
        return Project()
    except FileNotFoundError as e:
        fail(e)


def progressbar(items, label="", **kwargs):
    return click.progressbar(items, label=label, show_pos=True, **kwargs)


def refresh_statuses(project, platform, tracker, job_ids=None, *, remotes=None,
                     as_json=False):
    """Refresh active job statuses (local platform + remote services).

    Shows a progress bar and a summary line; with ``as_json`` both are
    suppressed (stdout is reserved for the JSON result) and warnings go to
    stderr.
    """
    count = refresh_active_statuses(project, platform, tracker, job_ids=job_ids,
                                    progress=plain_progress if as_json else progressbar)
    count += sync_remote_jobs(
        project, tracker, remotes, job_ids,
        on_error=lambda url, e: click.echo(f"Warning: remote service {url}: {e}", err=True))
    if count and not as_json:
        click.echo(f"Refreshed {count} job statuses from platform.")
    return count


def echo_json(data):
    """Print a command's JSON result (the only stdout output in --json mode)."""
    click.echo(json.dumps(data, ensure_ascii=False, default=str))


def register(cli):
    from jrun.commands import experiment, inspect, jobs, misc, submit

    for module in (misc, inspect, experiment, submit, jobs):
        module.register(cli)
