"""Click command registration and shared command-layer helpers.

Each module defines thin command functions (argument parsing + service calls +
error formatting). This package is the only layer that prints errors.
"""

import click

from jrun.project import Project
from jrun.tracking import refresh_active_statuses


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


def refresh_statuses(project, platform, tracker, job_ids=None):
    """Refresh active job statuses with a progress bar and a summary line."""
    count = refresh_active_statuses(project, platform, tracker, job_ids=job_ids,
                                    progress=progressbar)
    if count:
        click.echo(f"Refreshed {count} job statuses from platform.")
    return count


def register(cli):
    from jrun.commands import experiment, inspect, jobs, misc, submit

    for module in (misc, inspect, experiment, submit, jobs):
        module.register(cli)
