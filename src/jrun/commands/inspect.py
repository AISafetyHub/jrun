"""Inspection commands: queues / gpu-usage."""

import click

from jrun.api import PlatformAPI, queue_location_info
from jrun.errors import ApiError
from jrun.formatter import (display_width, format_ms, pad, pct, rpad,
                            snapshot_sort_key, truncate)
from jrun.commands import fail


def register(cli):
    cli.add_command(queues)
    cli.add_command(gpu_usage)


@click.command()
@click.option("--proj-id", default=None, help="Project ID header (default: none)")
@click.option("--projset-id", default=None,
              help="Project set ID (default: all / first joined project set)")
@click.option("--ids", is_flag=True, help="Also show queue/cluster/zone IDs")
@click.option("--category", type=click.Choice(["dedicated", "public", "idle"]),
              multiple=True, help="Queue category filter (default: all categories)")
def queues(proj_id, projset_id, ids, category):
    """List dedicated, public, and idle queues with free card counts."""
    api = PlatformAPI()
    try:
        wanted = set(category) or {"dedicated", "public", "idle"}
        queue_list = []
        if "dedicated" in wanted:
            queue_list.extend(api.list_queues(proj_id=proj_id, projset_id=projset_id))
        for shared_category in ("public", "idle"):
            if shared_category not in wanted:
                continue
            queue_list.extend(api.list_shared_queues(
                proj_id=proj_id, projset_id=projset_id,
                queue_category=shared_category,
            ))
        shared_counts = {}
        shared_limits = {}
        for shared_category in ("public", "idle"):
            if shared_category not in wanted:
                continue
            try:
                count_rows = api.list_shared_queue_job_counts(
                    proj_id=proj_id, projset_id=projset_id,
                    queue_category=shared_category,
                )
                for row in count_rows:
                    queue = row.get("queue") or {}
                    queue_id = queue.get("queueId") or queue.get("id")
                    if queue_id:
                        shared_counts[(shared_category, queue_id)] = row
                shared_limits[shared_category] = api.shared_queue_limits(
                    proj_id=proj_id, projset_id=projset_id,
                )
            except ApiError:
                # Counts/limits are decorative; the queue list still prints.
                pass
    except ApiError as e:
        fail(e)
    if not queue_list:
        click.echo("No queues found.")
        return

    groups: dict[str, list] = {}
    for queue in queue_list:
        location = queue_location_info(queue)
        cluster = location["region_name"] or "unknown"
        groups.setdefault(cluster, []).append((queue, location))

    for cluster, rows in groups.items():
        click.echo(f"\nCluster: {cluster}")
        header = ("  " + " ".join((
            pad("CATEGORY", 12), pad("QUEUE", 40), pad("CLUSTER", 16),
            pad("ZONE", 12), pad("MODEL", 24), rpad("TOTAL", 6),
            rpad("USED", 6), rpad("FREE", 6), rpad("RUN/LIMIT", 10),
        )))
        if ids:
            header += f"  {pad('QUEUE_ID', 38)} {pad('CLUSTER_ID', 38)} ZONE_ID"
        click.echo(header)
        click.echo("  " + "-" * (display_width(header) - 2))
        for queue, location in rows:
            resources = PlatformAPI.queue_free_resources(queue)
            if not resources:
                resources = [{"model": "-", "total": "-", "used": "-", "free": "-"}]
            for i, r in enumerate(resources):
                name = queue.get("name", "") if i == 0 else ""
                category_name = (location.get("queue_category") or queue.get("queueCategory", "")
                                 or "dedicated").replace("QUEUE_CATEGORY_", "").lower()
                count_row = shared_counts.get((category_name, location.get("queue_id")), {})
                limits = shared_limits.get(category_name, {})
                if count_row:
                    running = count_row.get("runningCount", 0)
                    limit_key = ("max_idle_jobs_per_projset" if category_name == "idle"
                                 else "max_jobs_per_projset")
                    run_limit = f"{running}/{limits.get(limit_key, '-')}"
                else:
                    run_limit = "-"
                line = ("  " + " ".join((
                    pad(category_name, 12), pad(name, 40),
                    pad(location['region_name'], 16), pad(location['zone_name'], 12),
                    pad(r['model'], 24), rpad(r['total'], 6),
                    rpad(r['used'], 6), rpad(r['free'], 6), rpad(run_limit, 10),
                )))
                if ids and i == 0:
                    line += (f"  {pad(location['queue_id'], 38)} "
                             f"{pad(location['cluster_id'], 38)} {location['zone_id']}")
                click.echo(line)


@click.command("gpu-usage")
@click.option("--proj-id", default=None, help="Project ID header (default: none)")
@click.option("--projset-id", default=None,
              help="Project set ID (default: all / first joined project set)")
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

    header = " ".join((
        pad("JOB", 10), pad("CREATOR", 12), pad("QUEUE", 34), pad("CLUSTER", 12),
        pad("STATUS", 10), rpad("GPUS", 5), rpad("GPU%", 7),
        rpad("GMEM%", 7), rpad("CPU%", 7), rpad("MEM%", 7), "STARTED",
    ))
    click.echo(header)
    click.echo("-" * display_width(header))
    for item in items:
        creator = (item.get("creatorName") or item.get("ownerName") or "")[:12]
        cluster = item.get("clusterDisplayName") or item.get("clusterName", "")
        click.echo(" ".join((
            pad(item.get('jobId', '')[:8], 10), pad(creator, 12),
            pad(truncate(item.get('queueName', ''), 34), 34), pad(cluster, 12),
            pad(item.get('stateName', ''), 10), rpad(item.get('acceleratorReq', 0), 5),
            rpad(pct(item.get('acceleratorUtil')), 7),
            rpad(pct(item.get('fbmemUtil')), 7), rpad(pct(item.get('cpuUtil')), 7),
            rpad(pct(item.get('memUtil')), 7), format_ms(item.get('beginTime')),
        )))
