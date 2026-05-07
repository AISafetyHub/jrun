import json
import re
import subprocess
import tempfile
from pathlib import Path

import click

from jrun.settings import find_jrun_dir, load_settings, save_settings, extract_platform_ids, build_job_url
from jrun.config import load_config, expand_grid, build_single_job, get_search_name, get_scheduler_params
from jrun.tracker import (
    record_search, record_single_job, get_jobs_by_search,
    get_all_jobs, update_job_status, load_tracker,
    find_job_by_name, remove_job, remove_search,
    record_pending_jobs, replace_pending_with_real,
)
from jrun.submit import submit_one_job
from jrun import airsctl
from jrun.template import TEMPLATE_CONTENT
from jrun.scheduler import start_scheduler, stop_scheduler, is_scheduler_running


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
    if not experiment_id:
        click.echo(f"Looking up experiment '{experiment_name}'...")
        output = airsctl.experiment_list(exp_name=experiment_name)
        if output:
            try:
                info = json.loads(output)
                experiment_id = info.get("experiment_id")
            except json.JSONDecodeError:
                pass
        if experiment_id:
            click.echo(f"Found experiment ID: {experiment_id}")
        else:
            click.echo("Warning: could not find experiment ID. You can set it later with jrun init -e <id>.", err=True)

    data = {"experiment_name": experiment_name}
    if experiment_id:
        data["experiment_id"] = experiment_id
        # Auto-extract platform IDs for status links
        output = airsctl.experiment_list(exp_id=experiment_id)
        if output:
            try:
                exp_data = json.loads(output)
                platform = extract_platform_ids(exp_data)
                if platform:
                    data["platform"] = platform
            except json.JSONDecodeError:
                pass
    jrun_dir = save_settings(data)
    click.echo(f"Initialized jrun: {jrun_dir}")


def _log_airsctl_error(result, action: str):
    if result.returncode != 0:
        click.echo(f"  Warning: {action} failed (rc={result.returncode})", err=True)
        if result.stdout:
            click.echo(result.stdout.strip(), err=True)
        if result.stderr:
            click.echo(result.stderr.strip(), err=True)


def _is_local_job(job: dict) -> bool:
    return job.get("resource_config", {}).get("queue_name") == "local"


def _run_local(job: dict):
    click.echo(f"[local] {job['name']}")
    click.echo(f"  $ {job['command']}")
    result = subprocess.run(job["command"], shell=True)
    if result.returncode != 0:
        click.echo(f"  Exit code: {result.returncode}", err=True)


@cli.command()
@click.argument("config_file")
@click.option("--dry-run", is_flag=True, help="Print jobs without submitting")
@click.option("--overwrite", "-o", is_flag=True, help="Auto-overwrite existing jobs matching --status")
@click.option("--status", "-s", multiple=True, help="Only overwrite jobs with this status (can be repeated)")
def submit(config_file, dry_run, overwrite, status):
    """Submit jobs from a config file."""
    settings, jrun_dir = load_settings()
    exp_name = settings.get("experiment_name")
    exp_id = settings.get("experiment_id")

    config = load_config(config_file)

    search = config.get("search")
    if search:
        jobs = expand_grid(config)
        search_name = get_search_name(config)
        click.echo(f"Search: {search_name} ({len(jobs)} jobs)")

        if dry_run:
            for i, j in enumerate(jobs, 1):
                click.echo(f"\n--- Job {i}: {j['name']} ---")
                click.echo(f"  Params: {j['params']}")
                click.echo(f"  $ {j['command']}")
                if j.get("envs"):
                    click.echo(f"  Envs: {j['envs']}")
                worker = j.get("resource_config", {}).get("worker")
                if worker:
                    click.echo(f"  Worker: {worker}")
            click.echo(f"\nTotal: {len(jobs)} jobs (dry run, nothing submitted)")
            return

        if _is_local_job(jobs[0]):
            click.echo(f"Local debug: running first job only")
            _run_local(jobs[0])
            return

        # Phase 1: scan and classify
        new_jobs = []
        overwrite_jobs = []  # (job, old_id, old_status)
        skipped_jobs = []
        for j in jobs:
            existing = find_job_by_name(jrun_dir, j["name"])
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
                click.echo(f"Job '{j['name']}' already exists (id={old_id[:8]}, status={old_status})")
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
                click.echo(f"    {j['name']} (id={old_id[:8]}, status={old_status})")
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
            click.echo(f"Cleaning up {len(overwrite_jobs)} old jobs...")
            for i, (j, old_id, old_status) in enumerate(overwrite_jobs, 1):
                click.echo(f"  [{i}/{len(overwrite_jobs)}] Cancelling {j['name']}...", nl=False)
                if old_status in ("Submitted", "Running", "Queued", "Starting", "Scheduling"):
                    _log_airsctl_error(airsctl.job_stop(old_id), "job stop")
                if not old_id.startswith("queued-"):
                    _log_airsctl_error(airsctl.job_cancel(old_id), "job cancel")
                click.echo(" done")

            # Batch remove configs from experiment in one modify call
            click.echo("  Removing configs from experiment...", nl=False)
            settings_tmp, _ = load_settings()
            exp_id_tmp = settings_tmp.get("experiment_id")
            if exp_id_tmp:
                names_to_remove = {j["name"] for j, _, _ in overwrite_jobs}
                _remove_configs_from_experiment(exp_id_tmp, names_to_remove)
            click.echo(" done")

            # Remove from tracker
            for j, old_id, old_status in overwrite_jobs:
                remove_job(jrun_dir, old_id)

        # Phase 4: submit
        submit_list = new_jobs + [j for j, _, _ in overwrite_jobs]
        sched_params = get_scheduler_params(config)
        parallel = sched_params["parallel_trials"]

        if parallel and parallel < len(submit_list):
            # Parallel mode: submit first batch, schedule the rest
            immediate = submit_list[:parallel]
            pending = submit_list[parallel:]

            job_entries = []
            click.echo(f"Submitting first {len(immediate)} jobs (parallel_trials={parallel})...")
            for j in immediate:
                job_id = _submit_one_job(j, exp_name, exp_id, quiet=True)
                if job_id:
                    job_entries.append({
                        "job_id": job_id,
                        "name": j["name"],
                        "params": j["params"],
                    })

            record_search(jrun_dir, search_name, config_file, job_entries)
            pending_ids = record_pending_jobs(jrun_dir, search_name, config_file, pending)

            pid = start_scheduler(
                jrun_dir, search_name, exp_name, exp_id,
                parallel, sched_params["poll_interval"], pending,
            )
            click.echo(f"Submitted {len(job_entries)} jobs, {len(pending)} queued")
            click.echo(f"Scheduler daemon started (pid={pid}, poll={sched_params['poll_interval']}s)")
            click.echo(f"  Log: {jrun_dir / 'scheduler' / f'{search_name}.log'}")
            _show_submitted_jobs(jrun_dir, [e["job_id"] for e in job_entries], settings)
        else:
            # Original behavior: submit all at once
            job_entries = []
            with click.progressbar(submit_list, label="Submitting", item_show_func=lambda j: j["name"] if j else "") as bar:
                for j in bar:
                    job_id = _submit_one_job(j, exp_name, exp_id, quiet=True)
                    if job_id:
                        job_entries.append({
                            "job_id": job_id,
                            "name": j["name"],
                            "params": j["params"],
                        })
            record_search(jrun_dir, search_name, config_file, job_entries)
            click.echo(f"\nSubmitted {len(job_entries)} jobs under search '{search_name}'")
            _show_submitted_jobs(jrun_dir, [e["job_id"] for e in job_entries], settings)

    else:
        job = build_single_job(config)
        if not job:
            click.echo("Error: config has neither 'search' nor 'job' section.", err=True)
            raise SystemExit(1)

        if dry_run:
            click.echo(f"Job: {job['name']}")
            click.echo(f"  $ {job['command']}")
            if job.get("envs"):
                click.echo(f"  Envs: {job['envs']}")
            worker = job.get("resource_config", {}).get("worker")
            if worker:
                click.echo(f"  Worker: {worker}")
            click.echo("\n(dry run, nothing submitted)")
            return

        if _is_local_job(job):
            _run_local(job)
            return

        existing = find_job_by_name(jrun_dir, job["name"])
        if existing:
            old_id, old_info = existing
            old_status = old_info.get("status", "?")
            if overwrite:
                if status and old_status not in status:
                    click.echo(f"Skipped {job['name']} (status={old_status} not in {list(status)}).")
                    return
                click.echo(f"Will overwrite: {job['name']} (id={old_id[:8]}, status={old_status})")
                if not click.confirm("Proceed?", default=True):
                    click.echo("Aborted.")
                    return
            else:
                click.echo(f"Job '{job['name']}' already exists (id={old_id[:8]}, status={old_status})")
                choice = click.prompt(
                    "[o]verwrite / [s]kip?",
                    type=click.Choice(["o", "s"], case_sensitive=False),
                    default="s",
                )
                if choice == "s":
                    click.echo("Skipped.")
                    return
            if old_status in ("Submitted", "Running", "Queued", "Starting", "Scheduling"):
                _log_airsctl_error(airsctl.job_stop(old_id), "job stop")
            _remove_one_job(old_id, job["name"], settings.get("experiment_id"))
            remove_job(jrun_dir, old_id)

        click.echo(f"Submitting {job['name']}...")
        job_id = _submit_one_job(job, exp_name, exp_id)
        if job_id:
            record_single_job(jrun_dir, job_id, job["name"])
            click.echo(f"Submitted → {job_id[:8]}")
            _show_submitted_jobs(jrun_dir, [job_id], settings)



def _show_submitted_jobs(jrun_dir, job_ids: list, settings: dict):
    """Show a status table for the just-submitted jobs."""
    tracker = load_tracker(jrun_dir)
    jobs = {}
    for jid in job_ids:
        if jid in tracker.get("jobs", {}):
            info = tracker["jobs"][jid]
            if info.get("status") != "Queued":
                result = airsctl.job_list(jid)
                if result.returncode == 0 and result.stdout:
                    s = _parse_job_status(result.stdout)
                    if s:
                        info["status"] = s
                        update_job_status(jrun_dir, jid, s)
            jobs[jid] = info
    if jobs:
        click.echo()
        _print_job_table(jobs, settings)


def _click_log_fn(msg, err=False):
    click.echo(msg, err=err)


def _submit_one_job(job: dict, exp_name: str | None, exp_id: str | None, quiet: bool = False) -> str | None:
    return submit_one_job(job, exp_name, exp_id, log_fn=_click_log_fn)


@cli.command()
@click.argument("name_or_id", required=False)
@click.option("--status", "-s", multiple=True, help="Only show jobs with this status (can be repeated)")
@click.option("--max", "-n", "max_jobs", default=10, show_default=True, help="Max number of jobs to display")
def status(name_or_id, status, max_jobs):
    """Show job status. Optionally filter by search name or job ID."""
    settings, jrun_dir = load_settings()
    tracker = load_tracker(jrun_dir)
    status_filter = set(status) if status else None

    if not name_or_id:
        _show_all_jobs(tracker, status_filter, max_jobs)
    elif name_or_id in tracker.get("searches", {}):
        _show_search_jobs(tracker, name_or_id, jrun_dir, status_filter, max_jobs)
    else:
        result = find_job_by_name(jrun_dir, name_or_id)
        if result:
            job_id, _ = result
            _show_single_job(job_id, tracker, jrun_dir)
        elif name_or_id in tracker.get("jobs", {}):
            _show_single_job(name_or_id, tracker, jrun_dir)
        else:
            click.echo(f"No job or search found matching '{name_or_id}'.")


def _show_all_jobs(tracker: dict, status_filter: set | None = None, max_jobs: int = 10):
    from collections import Counter

    settings, jrun_dir = load_settings()
    all_jobs = tracker.get("jobs", {})
    searches = tracker.get("searches", {})

    if not all_jobs and not searches:
        click.echo("No jobs tracked. Submit some jobs first.")
        return

    # Standalone jobs: not belonging to any search
    standalone_jobs = {
        jid: info for jid, info in all_jobs.items()
        if info.get("search_name") is None
    }

    # Refresh status for standalone jobs only
    for jid, info in standalone_jobs.items():
        if jid.startswith("queued-"):
            continue
        result = airsctl.job_list(jid)
        if result.returncode == 0 and result.stdout:
            s = _parse_job_status(result.stdout)
            if s:
                info["status"] = s
                update_job_status(jrun_dir, jid, s)

    # Refresh status for search jobs
    for sname, sinfo in searches.items():
        for jid in sinfo.get("job_ids", []):
            if jid not in all_jobs or jid.startswith("queued-"):
                continue
            if all_jobs[jid].get("status") in {"Completed", "Succeed", "Failed", "Stopped", "Cancelled", "Canceled"}:
                continue
            result = airsctl.job_list(jid)
            if result.returncode == 0 and result.stdout:
                s = _parse_job_status(result.stdout)
                if s:
                    all_jobs[jid]["status"] = s
                    update_job_status(jrun_dir, jid, s)

    # Build rows: (name, status_display, submitted_at, job_id_or_none, row_type)
    rows = []

    # Search summary rows
    for sname, sinfo in searches.items():
        job_ids = sinfo.get("job_ids", [])
        statuses = [all_jobs[jid].get("status", "?") for jid in job_ids if jid in all_jobs]
        if status_filter and not any(s in status_filter for s in statuses):
            continue
        counts = Counter(statuses)
        summary = ", ".join(f"{v} {k}" for k, v in counts.items())
        n_jobs = len(job_ids)
        rows.append((f"{sname} ({n_jobs} jobs)", summary, sinfo.get("submitted_at", ""), None, "search"))

    # Standalone job rows
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

    # Print table
    has_links = settings and settings.get("platform")
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
            line = f"{name:<{name_width}} {_colored_status(status_str, status_width)} {submitted_at:<20}"
            if has_links and job_id:
                url = build_job_url(settings, job_id)
                if url:
                    line += f" {_hyperlink(url, 'url')}"
        click.echo(line)

    if max_jobs > 0 and total > max_jobs:
        click.echo(f"\n... showing {max_jobs}/{total} entries (use -n to show more)")

    # Show scheduler info for active searches
    for sname in searches:
        pid = is_scheduler_running(jrun_dir, sname)
        if pid:
            click.echo(f"\nScheduler active for '{sname}' (pid={pid})")


def _show_search_jobs(tracker: dict, search_name: str, jrun_dir: Path, status_filter: set | None = None, max_jobs: int = 10):
    settings, _ = load_settings()
    search = tracker["searches"][search_name]
    click.echo(f"Search: {search_name}")
    click.echo(f"Config: {search['config_file']}")
    click.echo(f"Submitted: {search['submitted_at']}")

    pid = is_scheduler_running(jrun_dir, search_name)
    if pid:
        click.echo(f"Scheduler: active (pid={pid})")
    click.echo()

    jobs = {}
    for jid in search["job_ids"]:
        if jid in tracker["jobs"]:
            if not jid.startswith("queued-"):
                result = airsctl.job_list(jid)
                if result.returncode == 0 and result.stdout:
                    s = _parse_job_status(result.stdout)
                    if s:
                        tracker["jobs"][jid]["status"] = s
                        update_job_status(jrun_dir, jid, s)
            jobs[jid] = tracker["jobs"][jid]

    if status_filter:
        jobs = {jid: info for jid, info in jobs.items() if info.get("status") in status_filter}

    total = len(jobs)
    if max_jobs > 0 and total > max_jobs:
        jobs = dict(list(jobs.items())[:max_jobs])

    _print_job_table(jobs, settings)

    if max_jobs > 0 and total > max_jobs:
        click.echo(f"\n... showing {max_jobs}/{total} jobs (use -n to show more)")


def _show_single_job(job_id: str, tracker: dict, jrun_dir):
    result = airsctl.job_list(job_id)
    if result.returncode != 0 or not result.stdout:
        if job_id in tracker.get("jobs", {}):
            settings, _ = load_settings()
            _print_job_table({job_id: tracker["jobs"][job_id]}, settings)
        else:
            click.echo(f"No info found for job {job_id}")
        return

    status = _parse_job_status(result.stdout)
    if status and job_id in tracker.get("jobs", {}):
        tracker["jobs"][job_id]["status"] = status
        update_job_status(jrun_dir, job_id, status)

    if job_id in tracker.get("jobs", {}):
        settings, _ = load_settings()
        _print_job_table({job_id: tracker["jobs"][job_id]}, settings)
    else:
        click.echo(f"Job:    {job_id}")
        click.echo(f"Status: {status or '?'}")


def _hyperlink(url: str, text: str) -> str:
    """Format text as a clickable terminal hyperlink using OSC 8."""
    return f"\033]8;;{url}\033\\{text}\033]8;;\033\\"


STATUS_COLORS = {
    "succeed": "green",
    "completed": "green",
    "running": "blue",
    "starting": "cyan",
    "scheduling": "cyan",
    "queued": "yellow",
    "submitted": "yellow",
    "failed": "red",
    "stopped": "magenta",
    "cancelled": "magenta",
    "pending": "bright_black",
    "queued": "bright_black",
}


def _colored_status(status: str, width: int) -> str:
    color = STATUS_COLORS.get(status.lower())
    padded = f"{status:<{width}}"
    if color:
        return click.style(padded, fg=color)
    return padded


def _print_job_table(jobs: dict, settings: dict | None = None):
    if not jobs:
        return

    has_links = settings and settings.get("platform")

    # Calculate column widths dynamically
    max_name_len = max(len(info['name']) for info in jobs.values())
    max_status_len = max(len(info.get('status', '?')) for info in jobs.values())
    name_width = max(max_name_len, 4) + 2  # at least "NAME" + padding
    status_width = max(max_status_len, 6) + 2  # at least "STATUS" + padding

    header = f"{'NAME':<{name_width}} {'STATUS':<{status_width}} {'SUBMITTED':<20}"
    if has_links:
        header += " LINK"
    click.echo(header)
    click.echo("-" * len(header))
    for jid, info in jobs.items():
        status = info.get('status', '?')
        line = f"{info['name']:<{name_width}} {_colored_status(status, status_width)} {info.get('submitted_at', ''):<20}"
        if has_links:
            url = build_job_url(settings, jid)
            if url:
                line += f" {_hyperlink(url, 'url')}"
        click.echo(line)


def _parse_job_status(output: str) -> str | None:
    """Extract status from airsctl job list output."""
    try:
        data = json.loads(output)
        if isinstance(data, dict) and "status" in data:
            return data["status"].capitalize()
    except (json.JSONDecodeError, ValueError, AttributeError):
        pass
    # Fallback: line-based matching for non-JSON output
    for line in output.splitlines():
        lower = line.lower().strip()
        for s in ("cancelled", "failed", "stopped", "succeed", "completed",
                  "running", "starting", "scheduling", "queued", "submitted"):
            if s in lower:
                return s.capitalize()
    return None


@cli.command()
@click.argument("name_or_id", required=False)
@click.option("--status", "-s", multiple=True, help="Only stop jobs with this status (can be repeated)")
def stop(name_or_id, status):
    """Stop a running job or search scheduler by name or ID, or stop jobs by status."""
    settings, jrun_dir = load_settings()
    tracker = load_tracker(jrun_dir)
    status_filter = set(status) if status else None

    if not name_or_id:
        if not status_filter:
            click.echo("Error: must provide either a job/search name or --status filter.", err=True)
            raise SystemExit(1)

        all_jobs = tracker.get("jobs", {})
        stopped_count = 0
        for jid, job_info in all_jobs.items():
            if job_info.get("status") in status_filter:
                if jid.startswith("queued-") or job_info.get("status") == "Pending":
                    update_job_status(jrun_dir, jid, "Stopped")
                else:
                    _log_airsctl_error(airsctl.job_stop(jid), "job stop")
                stopped_count += 1
                click.echo(f"Stopped {job_info['name']} (status={job_info.get('status')})")

        click.echo(f"Stopped {stopped_count} jobs matching status {list(status_filter)}.")
        return

    # Check if it's a search name — stop scheduler + all active jobs
    if name_or_id in tracker.get("searches", {}):
        stopped_sched = stop_scheduler(jrun_dir, name_or_id)
        if stopped_sched:
            click.echo(f"Stopped scheduler for '{name_or_id}'")

        search = tracker["searches"][name_or_id]
        stopped_count = 0
        for jid in search.get("job_ids", []):
            job_info = tracker["jobs"].get(jid, {})
            if status_filter and job_info.get("status") not in status_filter:
                continue
            if jid.startswith("queued-") or job_info.get("status") == "Pending":
                update_job_status(jrun_dir, jid, "Stopped")
            else:
                _log_airsctl_error(airsctl.job_stop(jid), "job stop")
            stopped_count += 1

        if status_filter:
            click.echo(f"Stopped {stopped_count} jobs in search '{name_or_id}' matching status {list(status_filter)}.")
        else:
            click.echo(f"Stopped all jobs in search '{name_or_id}'")
        return

    # Single job
    result = find_job_by_name(jrun_dir, name_or_id)
    if result:
        job_id, job_info = result
        if status_filter and job_info.get("status") not in status_filter:
            click.echo(f"Skipped '{name_or_id}' (status={job_info.get('status')} not in {list(status_filter)})")
            return
        if job_id.startswith("queued-"):
            update_job_status(jrun_dir, job_id, "Stopped")
            click.echo(f"Cancelled pending job '{name_or_id}'")
            return
        name_or_id = job_id

    _log_airsctl_error(airsctl.job_stop(name_or_id), "job stop")


@cli.command()
@click.argument("name_or_id", required=False)
@click.option("--status", "-s", multiple=True, help="Only remove jobs with this status (can be repeated)")
def remove(name_or_id, status):
    """Remove a job or search: delete config from experiment, cancel on platform, and remove from tracker."""
    settings, jrun_dir = load_settings()
    exp_id = settings.get("experiment_id")
    tracker = load_tracker(jrun_dir)
    status_filter = set(status) if status else None

    if not name_or_id:
        if not status_filter:
            click.echo("Error: must provide either a job/search name or --status filter.", err=True)
            raise SystemExit(1)

        all_jobs = dict(tracker.get("jobs", {}))
        config_names = set()
        removed_count = 0
        for jid, job_info in all_jobs.items():
            if job_info.get("status") in status_filter:
                job_name = job_info.get("name", jid)
                if jid.startswith("queued-"):
                    remove_job(jrun_dir, jid)
                else:
                    _log_airsctl_error(airsctl.job_cancel(jid), "job cancel")
                    if job_name:
                        config_names.add(job_name)
                    remove_job(jrun_dir, jid)
                removed_count += 1
                click.echo(f"Removed {job_name} (status={job_info.get('status')})")

        if exp_id and config_names:
            _remove_configs_from_experiment(exp_id, config_names)

        click.echo(f"Removed {removed_count} jobs matching status {list(status_filter)}.")
        return

    if name_or_id in tracker.get("searches", {}):
        # Stop scheduler daemon first
        stop_scheduler(jrun_dir, name_or_id)

        search = tracker["searches"][name_or_id]
        job_ids = search.get("job_ids", [])

        if status_filter:
            # Only remove jobs matching status filter
            config_names = set()
            removed_count = 0
            for jid in job_ids:
                job_info = tracker["jobs"].get(jid, {})
                if job_info.get("status") not in status_filter:
                    continue
                job_name = job_info.get("name", jid)
                if jid.startswith("queued-"):
                    remove_job(jrun_dir, jid)
                else:
                    _log_airsctl_error(airsctl.job_cancel(jid), "job cancel")
                    if job_name:
                        config_names.add(job_name)
                    remove_job(jrun_dir, jid)
                removed_count += 1

            if exp_id and config_names:
                _remove_configs_from_experiment(exp_id, config_names)

            click.echo(f"Removed {removed_count} jobs in search '{name_or_id}' matching status {list(status_filter)}.")
        else:
            click.echo(f"Removing search '{name_or_id}' ({len(job_ids)} jobs)")

            config_names = set()
            for jid in job_ids:
                job_info = tracker["jobs"].get(jid, {})
                if jid.startswith("queued-"):
                    continue
                _log_airsctl_error(airsctl.job_cancel(jid), "job cancel")
                name = job_info.get("name")
                if name:
                    config_names.add(name)

            if exp_id and config_names:
                _remove_configs_from_experiment(exp_id, config_names)

            remove_search(jrun_dir, name_or_id)
            click.echo(f"Search '{name_or_id}' removed.")
    else:
        result = find_job_by_name(jrun_dir, name_or_id)
        if result:
            job_id, job_info = result
        else:
            job_id = name_or_id
            job_info = tracker.get("jobs", {}).get(job_id, {})

        if status_filter and job_info.get("status") not in status_filter:
            click.echo(f"Skipped '{name_or_id}' (status={job_info.get('status')} not in {list(status_filter)})")
            return

        job_name = job_info.get("name", name_or_id)
        if job_id.startswith("queued-"):
            remove_job(jrun_dir, job_id)
            click.echo(f"Queued job '{job_name}' removed.")
        else:
            _remove_one_job(job_id, job_name, exp_id)
            remove_job(jrun_dir, job_id)
            click.echo(f"Job '{job_name}' removed.")


def _remove_one_job(job_id: str, config_name: str | None, exp_id: str | None):
    """Remove config from experiment and cancel job on platform."""
    if exp_id and config_name:
        _remove_config_from_experiment(exp_id, config_name)
    if not job_id.startswith("queued-"):
        _log_airsctl_error(airsctl.job_cancel(job_id), "job cancel")


def _remove_configs_from_experiment(exp_id: str, config_names: set[str]):
    """Batch remove multiple configs from experiment in one modify call."""
    output = airsctl.experiment_list(exp_id=exp_id)
    if not output:
        return
    try:
        config_data = json.loads(output)
    except json.JSONDecodeError:
        return

    advance_configs = config_data.get("advance_config_infos", [])
    filtered = [c for c in advance_configs if c.get("config_name") not in config_names]
    if len(filtered) == len(advance_configs):
        return

    config_data["advance_config_infos"] = filtered
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", prefix="jrun_", delete=False
    ) as f:
        json.dump(config_data, f, indent=2)
        tmp_path = f.name
    try:
        _log_airsctl_error(airsctl.experiment_modify(tmp_path), "experiment modify")
    finally:
        Path(tmp_path).unlink(missing_ok=True)


def _remove_config_from_experiment(exp_id: str, config_name: str):
    """Remove a config entry from experiment's advance_config_infos via modify."""
    output = airsctl.experiment_list(exp_id=exp_id)
    if not output:
        return
    try:
        config_data = json.loads(output)
    except json.JSONDecodeError:
        return

    advance_configs = config_data.get("advance_config_infos", [])
    filtered = [c for c in advance_configs if c.get("config_name") != config_name]
    if len(filtered) == len(advance_configs):
        return

    config_data["advance_config_infos"] = filtered
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", prefix="jrun_", delete=False
    ) as f:
        json.dump(config_data, f, indent=2)
        tmp_path = f.name
    try:
        _log_airsctl_error(airsctl.experiment_modify(tmp_path), "experiment modify")
    finally:
        Path(tmp_path).unlink(missing_ok=True)


@cli.command("list")
def list_experiments():
    """List all experiments."""
    output = airsctl.experiment_list()
    if output:
        click.echo(output)
    else:
        click.echo("No experiments found.")
