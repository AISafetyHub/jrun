"""Misc commands: template / init / login."""

from pathlib import Path

import click

from jrun.api import PlatformAPI, save_auth
from jrun.errors import ApiError
from jrun.project import Project
from jrun.template import TEMPLATE_CONTENT


def register(cli):
    cli.add_command(template)
    cli.add_command(init)
    cli.add_command(login)


@click.command()
@click.option("-f", "--file", "file_path", default=None, help="Output file path (prints to stdout if omitted)")
def template(file_path):
    """Generate a config template with all available fields."""
    if not file_path:
        click.echo(TEMPLATE_CONTENT)
        return
    path = Path(file_path)
    if path.exists() and not click.confirm(f"'{file_path}' already exists. Overwrite?"):
        click.echo("Aborted.")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(TEMPLATE_CONTENT)
    click.echo(f"Template written to {file_path}")


@click.command()
def init():
    """Initialize jrun in the current directory (creates .jrun/)."""
    project = Project.init()
    click.echo(f"Initialized jrun: {project.jrun_dir}")


@click.command()
@click.option("--ak", default=None, help="Access Key (prompted if omitted)")
@click.option("--sk", default=None, help="Secret Key (prompted if omitted)")
def login(ak, sk):
    """Log in with platform AK/SK; credentials are stored in ~/.jrun/auth.json."""
    ak = ak or click.prompt("Access Key (AK)")
    sk = sk or click.prompt("Secret Key (SK)", hide_input=True)
    api = PlatformAPI(ak=ak, sk=sk)
    try:
        info = api.get_userinfo()
    except ApiError as e:
        click.echo(f"Login failed: {e}", err=True)
        raise SystemExit(1)
    data = info.get("data") if isinstance(info.get("data"), dict) else info
    alias = data.get("alias") or data.get("realName") or "unknown"
    path = save_auth(ak, sk)
    click.echo(f"Logged in as {alias}. Credentials saved to {path}")
