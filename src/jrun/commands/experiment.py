"""Experiment commands: experiment create / list / jobs / delete."""

import click

from jrun.api import PlatformAPI
from jrun.config import ConfigLoader
from jrun.errors import ApiError, ConfigError, PlatformError
from jrun.formatter import print_experiment_job_table
from jrun.models import job_runtime
from jrun.platform import PlatformClient
from jrun.project import Project, build_job_url
from jrun.submission import ensure_experiment
from jrun.commands import default_proj_ids


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

    The creation placeholder config takes image/queue from the first job
    (falling back to the deprecated experiment-block fields).
    """
    loader = ConfigLoader(config_file)
    try:
        spec = loader.get_experiment_spec()
    except ConfigError as e:
        click.echo(f"Error: {e}", err=True)
        raise SystemExit(1)
    if not spec:
        click.echo("Error: config has no 'experiment' section.", err=True)
        raise SystemExit(1)

    jobs = loader.expand_grid()
    if not jobs:
        single = loader.build_single_job()
        jobs = [single] if single else []
    image, image_region, queue_name = (
        job_runtime(jobs[0], spec) if jobs else (None, None, None)
    )

    platform = PlatformClient()
    api = PlatformAPI()
    try:
        exp_id = ensure_experiment(spec, platform, api, assume_yes=yes,
                                   confirm=click.confirm, echo=click.echo,
                                   image=image, image_region=image_region,
                                   queue_name=queue_name)
    except (ApiError, ConfigError, PlatformError) as e:
        click.echo(f"Error: {e}", err=True)
        raise SystemExit(1)
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
        click.echo(f"Error: {e}", err=True)
        raise SystemExit(1)


def _resolve_experiment_or_fail(platform: PlatformClient, name_or_id: str) -> dict:
    exp = platform.find_experiment(name_or_id)
    if not exp or not exp.get("experiment_id"):
        click.echo(f"Error: experiment '{name_or_id}' not found.", err=True)
        raise SystemExit(1)
    return exp


@experiment.command("jobs")
@click.argument("name_or_id")
@click.option("-s", "--status", multiple=True, help="Filter by status (repeatable)")
@click.option("--proj-id", default=None, help="Project ID (default: from .jrun/settings.json)")
@click.option("--projset-id", default=None, help="Project set ID (default: from .jrun/settings.json)")
def experiment_jobs(name_or_id, status, proj_id, projset_id):
    """List jobs under an experiment on the platform (by name or ID).

    Unlike `jrun job status`, this queries the platform directly and also
    shows jobs not tracked by this project.
    """
    platform = PlatformClient()
    exp = _resolve_experiment_or_fail(platform, name_or_id)
    exp_id = exp["experiment_id"]
    exp_name = exp.get("experiment_name") or name_or_id

    default_proj, default_projset = default_proj_ids()
    proj_id = proj_id or default_proj
    projset_id = projset_id or default_projset

    api = PlatformAPI()
    try:
        jobs = api.experiment_jobs_all(exp_id, proj_id=proj_id, projset_id=projset_id)
    except ApiError as e:
        click.echo(f"Error: {e}", err=True)
        raise SystemExit(1)

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
    """Per-job platform URLs, same merge order as `jrun job status`:
    project settings < experiment JSON < the job's own cluster/zone."""
    try:
        base_platform = Project().platform_ids or {}
    except FileNotFoundError:
        base_platform = {}
    base_platform = {**base_platform, **(Project.extract_platform_ids(exp) or {})}
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
@click.option("--proj-id", default=None, help="Project ID (default: from .jrun/settings.json)")
@click.option("--projset-id", default=None, help="Project set ID (default: from .jrun/settings.json)")
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

    default_proj, default_projset = default_proj_ids()
    proj_id = proj_id or default_proj
    projset_id = projset_id or default_projset

    api = PlatformAPI()
    try:
        api.delete_experiment(exp_id, exp_name, proj_id=proj_id, projset_id=projset_id)
    except ApiError as e:
        click.echo(f"Error: {e}", err=True)
        raise SystemExit(1)
    click.echo(f"Experiment '{exp_name}' deleted (id={exp_id})")
