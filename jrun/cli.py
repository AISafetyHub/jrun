"""jrun CLI entry point. Command implementations live in jrun.commands."""

import click

from jrun.commands import register


@click.group()
def cli():
    """jrun - Job submission and management CLI for airsctl."""
    pass


register(cli)
