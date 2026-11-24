"""Experiment commands: experiment create / list / jobs / delete."""

import click

from jrun.api import PlatformAPI
from jrun.config import ConfigLoader
from jrun.errors import ApiError, ConfigError, PlatformError
from jrun.formatter import print_experiment_job_table
from jrun.commands import fail
from jrun.models import job_target
from jrun.platform import PlatformClient
from jrun.project import Project, build_job_url
from jrun.submission import ensure_experiment


def register(cli):
    cli.add_command(experiment)


@click.group()
def experiment():
    """Manage experiments via the platform API."""
    pass


@experiment.command("create")
@click.argument("config_file")
@click.option("--yes", "-y", is_flag=True, help="Create without confirmation")
def experiment_create(config_file, yes):
    """Create the experiment defined by the config's experiment: block.

    The creation placeholder config takes image and resource names from the
    first job, then applies experiment defaults.
    """
    try:
        loader = ConfigLoader(config_file)
        spec = loader.get_experiment_spec()
        if not spec:
            raise ConfigError("config has no 'experiment' section.")
        jobs = loader.jobs()
        exp_id = ensure_experiment(spec, PlatformClient(), PlatformAPI(),
                                   job_target(jobs[0], spec), assume_yes=yes,
                                   confirm=click.confirm, echo=click.echo)
    except (ApiError, ConfigError, PlatformError) as e:
        fail(e)
    click.echo(f"Experiment '{spec.name}' ready (id={exp_id})")


@experiment.command("list")
def experiment_list():
    """List all experiments on the platform."""
    platform = PlatformClient()
    try:
        result = platform.experiment_list()
        if result.returncode == 0 and result.stdout:
            click.echo(result.stdout)
        else:
            if result.stderr:
                click.echo(f"Error: {result.stderr.strip()}", err=True)
            else:
                click.echo("No experiments found.")
    except PlatformError as e:
        fail(e)


def _resolve_experiment_or_fail(platform: PlatformClient, name_or_id: str) -> dict:
    exp = platform.find_experiment(name_or_id)
    if not exp:
        fail(f"experiment '{name_or_id}' not found.")
    return exp


@experiment.command("jobs")
@click.argument("name_or_id")
@click.option("-s", "--status", multiple=True, help="Filter by status (repeatable)")
@click.option("--proj-id", default=None, help="Project ID header (default: none)")
@click.option("--projset-id", default=None,
              help="Project set ID (default: auto-detected from joined project sets)")
def experiment_jobs(name_or_id, status, proj_id, projset_id):
    """List jobs under an experiment on the platform (by name or ID).

    Unlike `jrun job status`, this queries the platform directly and also
    shows jobs not tracked by this project.
    """
    platform = PlatformClient()
    exp = _resolve_experiment_or_fail(platform, name_or_id)
    exp_id = exp["experiment_id"]
    exp_name = exp.get("experiment_name") or name_or_id

    api = PlatformAPI()
    try:
        jobs = api.experiment_jobs_all(exp_id, proj_id=proj_id, projset_id=projset_id)
    except ApiError as e:
        fail(e)

    if status:
        wanted = {s.lower() for s in status}
        jobs = [j for j in jobs if (j.get("status") or "").lower() in wanted]
    if not jobs:
        click.echo(f"No matching jobs in experiment '{exp_name}'.")
        return
    jobs.sort(key=lambda j: int(j.get("createdTime") or 0), reverse=True)
    click.echo(f"Experiment '{exp_name}' (id={exp_id}): {len(jobs)} job(s)")
    print_experiment_job_table(jobs, job_urls=_job_urls(jobs, exp, exp_name))


def _job_urls(jobs: list[dict], exp: dict, exp_name: str) -> dict:
    """Per-job platform URLs: experiment JSON < the job's own cluster/zone."""
    base_platform = Project.extract_platform_ids(exp) or {}
    urls = {}
    for info in jobs:
        per_job = dict(base_platform)
        for key in ("clusterName", "zoneName"):
            if info.get(key):
                per_job[key] = info[key]
        urls[info.get("id")] = build_job_url(info.get("id"), per_job, exp_name)
    return urls


@experiment.command("delete")
@click.argument("name_or_id")
@click.option("--yes", "-y", is_flag=True, help="Delete without confirmation")
@click.option("--proj-id", default=None, help="Project ID header (default: none)")
@click.option("--projset-id", default=None,
              help="Project set ID (default: auto-detected from joined project sets)")
def experiment_delete(name_or_id, yes, proj_id, projset_id):
    """Delete an experiment on the platform (by name or ID).

    Note: the platform's delete is archive semantics (the experiment
    disappears from the active experiment lists).
    """
    platform = PlatformClient()
    exp = _resolve_experiment_or_fail(platform, name_or_id)
    exp_id = exp["experiment_id"]
    exp_name = exp.get("experiment_name") or name_or_id

    if not yes:
        click.confirm(f"Delete experiment '{exp_name}' (id={exp_id})?", abort=True)

    api = PlatformAPI()
    try:
        api.delete_experiment(exp_id, exp_name, proj_id=proj_id, projset_id=projset_id)
    except ApiError as e:
        fail(e)
    click.echo(f"Experiment '{exp_name}' deleted (id={exp_id})")
