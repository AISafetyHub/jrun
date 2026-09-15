"""Inspection commands: queues / gpu-usage."""

import click

from jrun.api import PlatformAPI, queue_location_info
from jrun.errors import ApiError
from jrun.formatter import format_ms, pad, pct, snapshot_sort_key
from jrun.commands import default_proj_ids


def register(cli):
    cli.add_command(queues)
    cli.add_command(gpu_usage)


@click.command()
@click.option("--proj-id", default=None, help="Project ID (default: from .jrun/settings.json)")
@click.option("--projset-id", default=None, help="Project set ID (default: from .jrun/settings.json)")
@click.option("--ids", is_flag=True, help="Also show queue/cluster/zone IDs")
def queues(proj_id, projset_id, ids):
    """List queues with free GPU counts, grouped by cluster."""
    default_proj, default_projset = default_proj_ids()
    proj_id = proj_id or default_proj
    projset_id = projset_id or default_projset

    api = PlatformAPI()
    try:
        queue_list = api.list_queues(proj_id=proj_id, projset_id=projset_id)
    except ApiError as e:
        click.echo(f"Error: {e}", err=True)
        raise SystemExit(1)
    if not queue_list:
        click.echo("No queues found.")
        return

    groups: dict[str, list] = {}
    for queue in queue_list:
        location = queue_location_info(queue)
        cluster = location["cluster_display_name"] or location["cluster_id"] or "unknown"
        groups.setdefault(cluster, []).append((queue, location))

    for cluster, rows in groups.items():
        click.echo(f"\nCluster: {cluster}")
        header = f"  {'QUEUE':<40} {'MODEL':<24} {'TOTAL':>6} {'USED':>6} {'FREE':>6}"
        if ids:
            header += f"  {'QUEUE_ID':<38} {'CLUSTER_ID':<38} ZONE_ID"
        click.echo(header)
        click.echo("  " + "-" * (len(header) - 2))
        for queue, location in rows:
            resources = PlatformAPI.queue_free_resources(queue)
            if not resources:
                resources = [{"model": "-", "total": "-", "used": "-", "free": "-"}]
            for i, r in enumerate(resources):
                name = queue.get("name", "") if i == 0 else ""
                line = (f"  {name:<40} {r['model']:<24} "
                        f"{r['total']:>6} {r['used']:>6} {r['free']:>6}")
                if ids and i == 0:
                    line += (f"  {location['queue_id']:<38} "
                             f"{location['cluster_id']:<38} {location['zone_id']}")
                click.echo(line)


@click.command("gpu-usage")
@click.option("--proj-id", default=None, help="Project ID (default: from .jrun/settings.json)")
@click.option("--projset-id", default=None, help="Project set ID (default: from .jrun/settings.json)")
@click.option("--cluster-id", multiple=True, help="Filter by cluster ID (repeatable)")
@click.option("--zone-id", multiple=True, help="Filter by zone ID (repeatable)")
@click.option("--owner", default=None, help="Filter by owner (job/workspace name)")
@click.option("-s", "--status", multiple=True,
              help="Filter by status (repeatable, default: Running)")
@click.option("--all", "show_all", is_flag=True, help="Include jobs of any status")
@click.option("--gpu-only/--include-cpu", default=True,
              help="Only show jobs occupying GPUs (default), or include CPU-only jobs")
@click.option("--sort", "sort_by",
              type=click.Choice(["time", "gpus", "gpu", "gmem", "cpu", "mem", "owner"]),
              default="time", help="Sort by: start time, GPU count, utilization, owner")
@click.option("--asc", is_flag=True, help="Sort ascending (default: descending)")
@click.option("-n", "max_rows", default=50, help="Max rows to show")
def gpu_usage(proj_id, projset_id, cluster_id, zone_id, owner, status, show_all,
              gpu_only, sort_by, asc, max_rows):
    """Show GPU utilization of jobs across the cluster (requires admin token)."""
    default_proj, default_projset = default_proj_ids()
    proj_id = proj_id or default_proj
    projset_id = projset_id or default_projset

    api = PlatformAPI()
    try:
        items = api.job_snapshots_all(
            proj_id=proj_id, projset_id=projset_id,
            cluster_ids=list(cluster_id), zone_ids=list(zone_id), owner=owner,
        )
    except ApiError as e:
        hint = " (this endpoint requires an admin token)" if e.status_code in (401, 403) else ""
        click.echo(f"Error: {e}{hint}", err=True)
        raise SystemExit(1)

    if not show_all:
        wanted = {s.lower() for s in status} if status else {"running"}
        items = [i for i in items if (i.get("stateName") or "").lower() in wanted]
    if gpu_only:
        items = [i for i in items if (i.get("acceleratorReq") or 0) > 0]
    if not items:
        click.echo("No matching jobs.")
        return

    items.sort(key=snapshot_sort_key(sort_by), reverse=not asc)
    items = items[:max_rows]

    click.echo(f"{'JOB':<10} {'CREATOR':<12} {'QUEUE':<34} {'CLUSTER':<12} "
               f"{'STATUS':<10} {'GPUS':>5} {'GPU%':>7} {'GMEM%':>7} {'CPU%':>7} {'MEM%':>7}  STARTED")
    click.echo("-" * 132)
    for item in items:
        creator = (item.get("creatorName") or item.get("ownerName") or "")[:12]
        cluster = item.get("clusterDisplayName") or item.get("clusterName", "")
        click.echo(
            f"{item.get('jobId', '')[:8]:<10} "
            f"{pad(creator, 12)} "
            f"{item.get('queueName', '')[:34]:<34} "
            f"{pad(cluster, 12)} "
            f"{item.get('stateName', ''):<10} "
            f"{item.get('acceleratorReq', 0):>5} "
            f"{pct(item.get('acceleratorUtil')):>7} "
            f"{pct(item.get('fbmemUtil')):>7} "
            f"{pct(item.get('cpuUtil')):>7} "
            f"{pct(item.get('memUtil')):>7}  "
            f"{format_ms(item.get('beginTime'))}"
        )
