import json
import subprocess
import tempfile
from collections import Counter
from pathlib import Path

import click

from jrun.config import ConfigLoader
from jrun.errors import PlatformError
from jrun.formatter import StatusFormatter
from jrun.models import Job
from jrun.platform import PlatformClient
from jrun.project import Project, extract_platform_ids
from jrun.scheduler import Scheduler, TERMINAL_STATUSES
from jrun.template import TEMPLATE_CONTENT


@click.group()
def cli():
    """jrun - Job submission and management CLI for airsctl."""
    pass


@cli.command()
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


@cli.command()
@click.option("--experiment-name", "-N", prompt="Experiment name", help="Name of the experiment")
@click.option("--experiment-id", "-e", default=None, help="Experiment ID (optional)")
def init(experiment_name, experiment_id):
    """Initialize jrun in the current directory."""
    platform_client = PlatformClient()

    if not experiment_id:
        click.echo(f"Looking up experiment '{experiment_name}'...")
        try:
            result = platform_client.experiment_list(exp_name=experiment_name)
            if result.stdout:
                try:
                    info = json.loads(result.stdout)
                    experiment_id = info.get("experiment_id")
                except json.JSONDecodeError:
                    pass
        except PlatformError:
            pass
        if experiment_id:
            click.echo(f"Found experiment ID: {experiment_id}")
        else:
            click.echo("Warning: could not find experiment ID. You can set it later with jrun init -e <id>.", err=True)

    platform = None
    if experiment_id:
        try:
            result = platform_client.experiment_list(exp_id=experiment_id)
            if result.stdout:
                try:
                    exp_data = json.loads(result.stdout)
                    platform = extract_platform_ids(exp_data)
                except json.JSONDecodeError:
                    pass
        except PlatformError:
            pass

    project = Project.init(experiment_name, experiment_id, platform)
    click.echo(f"Initialized jrun: {project.jrun_dir}")


def _run_local(job: Job):
    click.echo(f"[local] {job.name}")
    click.echo(f"  $ {job.command}")
    result = subprocess.run(job.command, shell=True)
    if result.returncode != 0:
        click.echo(f"  Exit code: {result.returncode}", err=True)


def _refresh_active_statuses(project: Project, platform: PlatformClient, tracker: dict,
                              job_ids: list[str] | None = None):
    """Refresh status from platform for non-terminal, non-queued jobs.

    If job_ids is given, only refresh those jobs. Otherwise refresh all.
    """
    jobs = tracker.get("jobs", {})
    candidates = job_ids if job_ids is not None else list(jobs.keys())
    active = [(jid, jobs[jid]) for jid in candidates
              if jid in jobs and not jid.startswith("queued-")
              and jobs[jid].get("status") not in TERMINAL_STATUSES]
    if not active:
        return
    refreshed = 0
    with click.progressbar(active, label="Refreshing statuses", show_pos=True) as bar:
        for jid, info in bar:
            new_status = platform.get_job_status(jid)
            if new_status and new_status != info.get("status"):
                info["status"] = new_status
                project.update_job_status(jid, new_status)
                refreshed += 1
    if refreshed:
        click.echo(f"Refreshed {refreshed} job statuses from platform.")


@cli.command()
@click.argument("config_file")
@click.option("--dry-run", is_flag=True, help="Print jobs without submitting")
@click.option("--overwrite", "-o", is_flag=True, help="Auto-overwrite existing jobs matching --status")
@click.option("--status", "-s", multiple=True, help="Only overwrite jobs with this status (can be repeated)")
def submit(config_file, dry_run, overwrite, status):
    """Submit jobs from a config file."""
    project = Project()
    platform = PlatformClient()
    loader = ConfigLoader(config_file)
    formatter = StatusFormatter(project)

    exp_name = loader.get_experiment_name() or project.experiment_name
    exp_id = loader.get_experiment_id() or project.experiment_id

    if not dry_run and not exp_id:
        click.echo("Error: no experiment ID. Set one in the config or run 'jrun init -e <id>'.", err=True)
        raise SystemExit(1)

    if not dry_run:
        config_count = platform.count_experiment_configs(exp_id)
        if config_count is not None and config_count >= 100:
            click.echo(
                f"Warning: experiment '{exp_name or exp_id}' already has {config_count} configs. "
                f"Too many configs may cause platform performance issues.",
                err=True,
            )
            if not click.confirm("Continue?", default=False):
                click.echo("Aborted.")
                return

    search = loader.config.get("search")
    if search:
        jobs = loader.expand_grid()
        search_name = loader.get_search_name()
        click.echo(f"Search: {search_name} ({len(jobs)} jobs)")

        if dry_run:
            for i, j in enumerate(jobs, 1):
                click.echo(f"\n--- Job {i}: {j.name} ---")
                click.echo(f"  Params: {j.params}")
                click.echo(f"  $ {j.command}")
                if j.envs:
                    click.echo(f"  Envs: {j.envs}")
                if j.resource_config.worker:
                    click.echo(f"  Worker: {j.resource_config.worker.to_dict()}")
            click.echo(f"\nTotal: {len(jobs)} jobs (dry run, nothing submitted)")
            return

        if jobs[0].is_local:
            click.echo(f"Local debug: running first job only")
            _run_local(jobs[0])
            return

        # Phase 1: scan and classify
        tracker = project.load_tracker()
        search_job_ids = tracker.get("searches", {}).get(search_name, {}).get("job_ids", [])
        _refresh_active_statuses(project, platform, tracker, job_ids=search_job_ids)
        new_jobs = []
        overwrite_jobs = []
        skipped_jobs = []
        for j in jobs:
            existing = project.find_job_by_name(j.name)
            if not existing:
                new_jobs.append(j)
                continue
            old_id, old_info = existing
            old_status = old_info.get("status", "?")
            if overwrite:
                if status and old_status not in status:
                    skipped_jobs.append((j, old_id, old_status))
                else:
                    overwrite_jobs.append((j, old_id, old_status))
            else:
                click.echo(f"Job '{j.name}' already exists (id={old_id[:8]}, status={old_status})")
                choice = click.prompt(
                    "  [o]verwrite / [s]kip?",
                    type=click.Choice(["o", "s"], case_sensitive=False),
                    default="s",
                )
                if choice == "o":
                    overwrite_jobs.append((j, old_id, old_status))
                else:
                    skipped_jobs.append((j, old_id, old_status))

        # Phase 2: summary and confirm
        if new_jobs:
            click.echo(f"  New: {len(new_jobs)} jobs")
        if overwrite_jobs:
            click.echo(f"  Overwrite: {len(overwrite_jobs)} jobs")
            for j, old_id, old_status in overwrite_jobs:
                click.echo(f"    {j.name} (id={old_id[:8]}, status={old_status})")
        if skipped_jobs:
            click.echo(f"  Skip: {len(skipped_jobs)} jobs (status not in {list(status)})")

        total = len(new_jobs) + len(overwrite_jobs)
        if total == 0:
            click.echo("Nothing to submit.")
            return

        if overwrite and overwrite_jobs:
            if not click.confirm(f"Proceed with {total} jobs?", default=True):
                click.echo("Aborted.")
                return

        # Phase 3: teardown old jobs
        if overwrite_jobs:
            total = len(overwrite_jobs)
            with click.progressbar(overwrite_jobs, label="Cleaning up old jobs", show_pos=True) as bar:
                for j, old_id, old_status in bar:
                    if not old_id.startswith("queued-"):
                        try:
                            if old_status in ("Submitted", "Running", "Starting", "Scheduling"):
                                platform.job_stop(old_id)
                            platform.job_cancel(old_id)
                        except PlatformError:
                            pass

            click.echo("Removing configs from experiment...", nl=False)
            if exp_id:
                names_to_remove = {j.name for j, _, _ in overwrite_jobs}
                try:
                    platform.remove_configs(exp_id, names_to_remove)
                except PlatformError:
                    pass
            click.echo(" done")

            for j, old_id, old_status in overwrite_jobs:
                project.remove_job(old_id)

        # Phase 4: submit
        submit_list = new_jobs + [j for j, _, _ in overwrite_jobs]
        sched_params = loader.get_scheduler_params()
        parallel = sched_params["parallel_trials"]

        if parallel and parallel < len(submit_list):
            immediate = submit_list[:parallel]
            pending = submit_list[parallel:]

            job_entries = []
            click.echo(f"Submitting first {len(immediate)} jobs (parallel_trials={parallel})...")
            for j in immediate:
                try:
                    job_id = platform.submit_job(j, exp_name, exp_id)
                    job_entries.append({"job_id": job_id, "name": j.name, "params": j.params})
                except PlatformError as e:
                    click.echo(f"  {j.name}: Error: {e}", err=True)

            project.record_search(search_name, config_file, job_entries,
                                   experiment_id=exp_id, experiment_name=exp_name)
            pending_dicts = [j.to_dict() for j in pending]
            project.record_pending_jobs(search_name, config_file, pending_dicts,
                                        experiment_id=exp_id, experiment_name=exp_name)

            scheduler = Scheduler(
                project.jrun_dir, search_name, exp_name, exp_id,
                parallel, sched_params["poll_interval"],
            )
            pid = scheduler.start(pending_dicts)
            click.echo(f"Submitted {len(job_entries)} jobs, {len(pending)} queued")
            click.echo(f"Scheduler daemon started (pid={pid}, poll={sched_params['poll_interval']}s)")
            click.echo(f"  Log: {project.jrun_dir / 'scheduler' / f'{search_name}.log'}")
            _show_submitted_jobs(project, formatter, [e["job_id"] for e in job_entries])
        else:
            job_entries = []
            with click.progressbar(submit_list, label="Submitting", item_show_func=lambda j: j.name if j else "") as bar:
                for j in bar:
                    try:
                        job_id = platform.submit_job(j, exp_name, exp_id)
                        job_entries.append({"job_id": job_id, "name": j.name, "params": j.params})
                    except PlatformError as e:
                        click.echo(f"\n  {j.name}: Error: {e}", err=True)
            project.record_search(search_name, config_file, job_entries,
                                   experiment_id=exp_id, experiment_name=exp_name)
            click.echo(f"\nSubmitted {len(job_entries)} jobs under search '{search_name}'")
            _show_submitted_jobs(project, formatter, [e["job_id"] for e in job_entries])

    else:
        job = loader.build_single_job()
        if not job:
            click.echo("Error: config has neither 'search' nor 'job' section.", err=True)
            raise SystemExit(1)

        if dry_run:
            click.echo(f"Job: {job.name}")
            click.echo(f"  $ {job.command}")
            if job.envs:
                click.echo(f"  Envs: {job.envs}")
            if job.resource_config.worker:
                click.echo(f"  Worker: {job.resource_config.worker.to_dict()}")
            click.echo("\n(dry run, nothing submitted)")
            return

        if job.is_local:
            _run_local(job)
            return

        existing = project.find_job_by_name(job.name)
        if existing:
            old_id, old_info = existing
            if not old_id.startswith("queued-") and old_info.get("status") not in TERMINAL_STATUSES:
                new_status = platform.get_job_status(old_id)
                if new_status:
                    old_info["status"] = new_status
                    project.update_job_status(old_id, new_status)
            old_status = old_info.get("status", "?")
            if overwrite:
                if status and old_status not in status:
                    click.echo(f"Skipped {job.name} (status={old_status} not in {list(status)}).")
                    return
                click.echo(f"Will overwrite: {job.name} (id={old_id[:8]}, status={old_status})")
                if not click.confirm("Proceed?", default=True):
                    click.echo("Aborted.")
                    return
            else:
                click.echo(f"Job '{job.name}' already exists (id={old_id[:8]}, status={old_status})")
                choice = click.prompt(
                    "[o]verwrite / [s]kip?",
                    type=click.Choice(["o", "s"], case_sensitive=False),
                    default="s",
                )
                if choice == "s":
                    click.echo("Skipped.")
                    return
            if not old_id.startswith("queued-"):
                try:
                    if old_status in ("Submitted", "Running", "Starting", "Scheduling"):
                        platform.job_stop(old_id)
                except PlatformError:
                    pass
            _remove_one_job(platform, old_id, job.name, exp_id)
            project.remove_job(old_id)

        click.echo(f"Submitting {job.name}...")
        try:
            job_id = platform.submit_job(job, exp_name, exp_id)
            project.record_single_job(job_id, job.name,
                                      experiment_id=exp_id, experiment_name=exp_name)
            click.echo(f"Submitted → {job_id[:8]}")
            _show_submitted_jobs(project, formatter, [job_id])
        except PlatformError as e:
            click.echo(f"Error: {e}", err=True)
            raise SystemExit(1)


def _show_submitted_jobs(project: Project, formatter: StatusFormatter, job_ids: list):
    platform = PlatformClient()
    tracker = project.load_tracker()
    jobs = {}
    for jid in job_ids:
        if jid in tracker.get("jobs", {}):
            info = tracker["jobs"][jid]
            if info.get("status") != "Queued":
                new_status = platform.get_job_status(jid)
                if new_status:
                    info["status"] = new_status
                    project.update_job_status(jid, new_status)
            jobs[jid] = info
    if jobs:
        click.echo()
        formatter.print_job_table(jobs)


def _remove_one_job(platform: PlatformClient, job_id: str, job_name: str, exp_id: str | None):
    if not job_id.startswith("queued-"):
        try:
            platform.job_cancel(job_id)
        except PlatformError:
            pass
    if exp_id:
        try:
            platform.remove_configs(exp_id, {job_name})
        except PlatformError:
            pass


@cli.command("status")
@click.argument("name_or_id", required=False)
@click.option("--status", "-s", multiple=True, help="Filter by status")
@click.option("-n", "max_jobs", default=10, help="Max jobs to show (0 for all)")
def status_cmd(name_or_id, status, max_jobs):
    """Show job status."""
    project = Project()
    platform = PlatformClient()
    formatter = StatusFormatter(project)
    tracker = project.load_tracker()
    status_filter = set(status) if status else None

    if name_or_id:
        if name_or_id in tracker.get("searches", {}):
            _show_search_jobs(project, platform, formatter, tracker, name_or_id, status_filter, max_jobs)
            return

        for jid, info in tracker.get("jobs", {}).items():
            if jid == name_or_id or info.get("name") == name_or_id:
                _show_single_job(project, platform, formatter, jid, tracker)
                return

        for sname, sinfo in tracker.get("searches", {}).items():
            if name_or_id.lower() in sname.lower():
                _show_search_jobs(project, platform, formatter, tracker, sname, status_filter, max_jobs)
                return

        click.echo(f"No job or search matching '{name_or_id}'.")
        return

    _show_all_jobs(project, platform, formatter, tracker, status_filter, max_jobs)


def _show_all_jobs(project: Project, platform: PlatformClient, formatter: StatusFormatter,
                   tracker: dict, status_filter: set | None, max_jobs: int):
    searches = tracker.get("searches", {})
    standalone_jobs = {
        jid: info for jid, info in tracker.get("jobs", {}).items()
        if not info.get("search_name")
    }

    if not searches and not standalone_jobs:
        click.echo("No jobs tracked. Submit jobs with 'jrun submit <config>'.")
        return

    rows = []
    for sname, sinfo in searches.items():
        job_ids = sinfo.get("job_ids", [])
        statuses = []
        for jid in job_ids:
            job_info = tracker["jobs"].get(jid)
            if job_info:
                if jid.startswith("queued-"):
                    if job_info.get("status") != "Queued":
                        job_info["status"] = "Queued"
                        project.update_job_status(jid, "Queued")
                elif job_info.get("status") not in TERMINAL_STATUSES:
                    new_status = platform.get_job_status(jid)
                    if new_status:
                        job_info["status"] = new_status
                        project.update_job_status(jid, new_status)
                statuses.append(job_info.get("status", "?"))
        if status_filter and not any(s in status_filter for s in statuses):
            continue
        counts = Counter(statuses)
        summary = ", ".join(f"{v} {k}" for k, v in counts.items())
        n_jobs = len(job_ids)
        rows.append((f"{sname} ({n_jobs} jobs)", summary, sinfo.get("submitted_at", ""), None, "search"))

    for jid, info in standalone_jobs.items():
        if status_filter and info.get("status") not in status_filter:
            continue
        rows.append((info["name"], info.get("status", "?"), info.get("submitted_at", ""), jid, "job"))

    if not rows:
        click.echo("No jobs match the filter.")
        return

    total = len(rows)
    if max_jobs > 0 and total > max_jobs:
        rows = rows[:max_jobs]

    has_links = project.platform_ids is not None
    max_name_len = max(len(r[0]) for r in rows)
    max_status_len = max(len(r[1]) for r in rows)
    name_width = max(max_name_len, 4) + 2
    status_width = max(max_status_len, 6) + 2

    header = f"{'NAME':<{name_width}} {'STATUS':<{status_width}} {'SUBMITTED':<20}"
    if has_links:
        header += " LINK"
    click.echo(header)
    click.echo("-" * len(header))

    for name, status_str, submitted_at, job_id, row_type in rows:
        if row_type == "search":
            line = f"{name:<{name_width}} {status_str:<{status_width}} {submitted_at:<20}"
        else:
            line = f"{name:<{name_width}} {formatter.colored_status(status_str, status_width)} {submitted_at:<20}"
            if has_links and job_id:
                url = project.build_job_url(job_id)
                if url:
                    line += f" {formatter.hyperlink(url, 'url')}"
        click.echo(line)

    if max_jobs > 0 and total > max_jobs:
        click.echo(f"\n... showing {max_jobs}/{total} entries (use -n to show more)")

    for sname in searches:
        scheduler = Scheduler(project.jrun_dir, sname, None, "", 0, 0)
        pid = scheduler.is_running()
        if pid:
            click.echo(f"\nScheduler active for '{sname}' (pid={pid})")


def _show_search_jobs(project: Project, platform: PlatformClient, formatter: StatusFormatter,
                      tracker: dict, search_name: str, status_filter: set | None, max_jobs: int):
    search = tracker["searches"][search_name]
    click.echo(f"Search: {search_name}")
    click.echo(f"Config: {search['config_file']}")
    click.echo(f"Submitted: {search['submitted_at']}")

    scheduler = Scheduler(project.jrun_dir, search_name, None, "", 0, 0)
    pid = scheduler.is_running()
    if pid:
        click.echo(f"Scheduler: active (pid={pid})")
    click.echo()

    jobs = {}
    for jid in search["job_ids"]:
        if jid in tracker["jobs"]:
            if jid.startswith("queued-"):
                if tracker["jobs"][jid].get("status") != "Queued":
                    tracker["jobs"][jid]["status"] = "Queued"
                    project.update_job_status(jid, "Queued")
            elif tracker["jobs"][jid].get("status") not in TERMINAL_STATUSES:
                new_status = platform.get_job_status(jid)
                if new_status:
                    tracker["jobs"][jid]["status"] = new_status
                    project.update_job_status(jid, new_status)
            jobs[jid] = tracker["jobs"][jid]

    if status_filter:
        jobs = {jid: info for jid, info in jobs.items() if info.get("status") in status_filter}

    total = len(jobs)
    if max_jobs > 0 and total > max_jobs:
        jobs = dict(list(jobs.items())[:max_jobs])

    formatter.print_job_table(jobs)

    if max_jobs > 0 and total > max_jobs:
        click.echo(f"\n... showing {max_jobs}/{total} jobs (use -n to show more)")


def _show_single_job(project: Project, platform: PlatformClient, formatter: StatusFormatter,
                     job_id: str, tracker: dict):
    new_status = platform.get_job_status(job_id)
    if new_status and job_id in tracker.get("jobs", {}):
        tracker["jobs"][job_id]["status"] = new_status
        project.update_job_status(job_id, new_status)

    if job_id in tracker.get("jobs", {}):
        formatter.print_job_table({job_id: tracker["jobs"][job_id]})
    else:
        click.echo(f"Job:    {job_id}")
        click.echo(f"Status: {new_status or '?'}")


@cli.command()
@click.argument("name_or_id", required=False)
@click.option("--status", "-s", multiple=True, help="Only stop jobs with this status (can be repeated)")
def stop(name_or_id, status):
    """Stop a running job or search scheduler by name or ID, or stop jobs by status."""
    project = Project()
    platform = PlatformClient()
    tracker = project.load_tracker()
    status_filter = set(status) if status else None

    if not name_or_id:
        if not status_filter:
            click.echo("Error: must provide either a job/search name or --status filter.", err=True)
            raise SystemExit(1)

        _refresh_active_statuses(project, platform, tracker)
        all_jobs = tracker.get("jobs", {})
        stopped_count = 0
        for jid, job_info in all_jobs.items():
            if job_info.get("status") in status_filter:
                if jid.startswith("queued-") or job_info.get("status") == "Pending":
                    project.update_job_status(jid, "Stopped")
                else:
                    try:
                        platform.job_stop(jid)
                    except PlatformError:
                        pass
                stopped_count += 1
                click.echo(f"Stopped {job_info['name']} (status={job_info.get('status')})")

        click.echo(f"Stopped {stopped_count} jobs matching status {list(status_filter)}.")
        return

    if name_or_id in tracker.get("searches", {}):
        search = tracker["searches"][name_or_id]
        search_job_ids = search.get("job_ids", [])
        _refresh_active_statuses(project, platform, tracker, job_ids=search_job_ids)

        scheduler = Scheduler(project.jrun_dir, name_or_id, None, "", 0, 0)
        stopped_sched = scheduler.stop()
        if stopped_sched:
            click.echo(f"Stopped scheduler for '{name_or_id}'")

        stopped_count = 0
        for jid in search_job_ids:
            job_info = tracker["jobs"].get(jid, {})
            job_status = job_info.get("status", "")
            if status_filter and job_status not in status_filter:
                continue
            if job_status in ("Submitted", "Running", "Queued", "Starting", "Scheduling"):
                if jid.startswith("queued-") or job_status == "Pending":
                    project.update_job_status(jid, "Stopped")
                else:
                    try:
                        platform.job_stop(jid)
                    except PlatformError:
                        pass
                stopped_count += 1
                click.echo(f"Stopped {job_info.get('name', jid)} (status={job_status})")

        click.echo(f"Stopped {stopped_count} jobs in search '{name_or_id}'.")
        return

    job_id, job_info = None, None
    for jid, info in tracker.get("jobs", {}).items():
        if jid == name_or_id or info.get("name") == name_or_id:
            job_id, job_info = jid, info
            break

    if not job_id:
        click.echo(f"No job or search matching '{name_or_id}'.", err=True)
        raise SystemExit(1)

    _refresh_active_statuses(project, platform, tracker, job_ids=[job_id])

    if job_id.startswith("queued-") or job_info.get("status") == "Pending":
        project.update_job_status(job_id, "Stopped")
    else:
        try:
            platform.job_stop(job_id)
        except PlatformError:
            pass
    click.echo(f"Stopped {job_info.get('name', job_id)}")


@cli.command()
@click.argument("name_or_id")
@click.option("--status", "-s", multiple=True, help="Only remove jobs with this status (can be repeated)")
def remove(name_or_id, status):
    """Remove a job or search: stop + delete config + remove from tracker."""
    project = Project()
    platform = PlatformClient()
    tracker = project.load_tracker()
    status_filter = set(status) if status else None

    if name_or_id in tracker.get("searches", {}):
        search = tracker["searches"][name_or_id]
        search_job_ids = search.get("job_ids", [])
        _refresh_active_statuses(project, platform, tracker, job_ids=search_job_ids)
        scheduler = Scheduler(project.jrun_dir, name_or_id, None, "", 0, 0)
        scheduler.stop()

        search = tracker["searches"][name_or_id]
        job_ids = search.get("job_ids", [])
        exp_id = search.get("experiment_id") or project.experiment_id

        if status_filter:
            config_names = set()
            removed_count = 0
            for jid in job_ids:
                job_info = tracker["jobs"].get(jid, {})
                if job_info.get("status") not in status_filter:
                    continue
                job_name = job_info.get("name", jid)
                if jid.startswith("queued-"):
                    project.remove_job(jid)
                else:
                    try:
                        platform.job_cancel(jid)
                    except PlatformError:
                        pass
                    if job_name:
                        config_names.add(job_name)
                    project.remove_job(jid)
                removed_count += 1

            if exp_id and config_names:
                try:
                    platform.remove_configs(exp_id, config_names)
                except PlatformError:
                    pass

            click.echo(f"Removed {removed_count} jobs in search '{name_or_id}' matching status {list(status_filter)}.")
        else:
            click.echo(f"Removing search '{name_or_id}' ({len(job_ids)} jobs)")

            config_names = set()
            for jid in job_ids:
                job_info = tracker["jobs"].get(jid, {})
                if jid.startswith("queued-"):
                    continue
                try:
                    platform.job_cancel(jid)
                except PlatformError:
                    pass
                name = job_info.get("name")
                if name:
                    config_names.add(name)

            if exp_id and config_names:
                try:
                    platform.remove_configs(exp_id, config_names)
                except PlatformError:
                    pass

            project.remove_search(name_or_id)
            click.echo(f"Search '{name_or_id}' removed.")
    else:
        job_id, job_info = None, None
        for jid, info in tracker.get("jobs", {}).items():
            if jid == name_or_id or info.get("name") == name_or_id:
                job_id, job_info = jid, info
                break

        if not job_id:
            click.echo(f"No job or search matching '{name_or_id}'.", err=True)
            raise SystemExit(1)

        exp_id = job_info.get("experiment_id") or project.experiment_id
        _refresh_active_statuses(project, platform, tracker, job_ids=[job_id])
        job_name = job_info.get("name", job_id)
        click.echo(f"Removing {job_name}...")
        if not job_id.startswith("queued-"):
            try:
                platform.job_cancel(job_id)
            except PlatformError:
                pass
            if exp_id:
                try:
                    platform.remove_configs(exp_id, {job_name})
                except PlatformError:
                    pass
        project.remove_job(job_id)
        click.echo(f"Removed {job_name}")


@cli.command("list")
def list_experiments():
    """List all experiments."""
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

