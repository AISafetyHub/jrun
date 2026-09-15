"""Click command registration.

Each module defines thin command functions (argument parsing + service calls +
error formatting). This package is the only layer that prints errors.
"""

from jrun.project import Project


def default_proj_ids() -> tuple[str | None, str | None]:
    """Project IDs from .jrun/settings.json, if a project is initialized."""
    try:
        platform_ids = Project().platform_ids or {}
    except FileNotFoundError:
        return None, None
    return platform_ids.get("projId"), platform_ids.get("projsetId")


def register(cli):
    from jrun.commands import experiment, inspect, jobs, misc, submit

    for module in (misc, inspect, experiment, submit, jobs):
        module.register(cli)
