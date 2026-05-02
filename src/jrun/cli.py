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
            click.echo("Cleaning up old jobs...")
            for j, old_id, old_status in overwrite_jobs:
                if old_status in ("Submitted", "Running", "Queued", "Starting", "Scheduling"):
                    _log_airsctl_error(airsctl.job_stop(old_id), "job stop")
                settings_tmp, _ = load_settings()
                _remove_one_job(old_id, j["name"], settings_tmp.get("experiment_id"))
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
            click.echo(f"Submitted {len(job_entries)} jobs, {len(pending)} pending")
            click.echo(f"Scheduler daemon started (pid={pid}, poll={sched_params['poll_interval']}s)")
            click.echo(f"  Log: {jrun_dir / 'scheduler' / f'{search_name}.log'}")
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



def _click_log_fn(msg, err=False):
    click.echo(msg, err=err)


def _submit_one_job(job: dict, exp_name: str | None, exp_id: str | None, quiet: bool = False) -> str | None:
    return submit_one_job(job, exp_name, exp_id, log_fn=_click_log_fn)


@cli.command()
@click.argument("name_or_id", required=False)
@click.option("--status", "-s", multiple=True, help="Only show jobs with this status (can be repeated)")
def status(name_or_id, status):
    """Show job status. Optionally filter by search name or job ID."""
    settings, jrun_dir = load_settings()
    tracker = load_tracker(jrun_dir)
    status_filter = set(status) if status else None

    if not name_or_id:
        _show_all_jobs(tracker, status_filter)
    elif name_or_id in tracker.get("searches", {}):
        _show_search_jobs(tracker, name_or_id, jrun_dir, status_filter)
    else:
        result = find_job_by_name(jrun_dir, name_or_id)
        if result:
            job_id, _ = result
            _show_single_job(job_id, tracker, jrun_dir)
        else:
            _show_single_job(name_or_id, tracker, jrun_dir)


def _show_all_jobs(tracker: dict, status_filter: set | None = None):
    settings, jrun_dir = load_settings()
    jobs = tracker.get("jobs", {})
    if not jobs:
        click.echo("No jobs tracked. Submit some jobs first.")
        return

    for jid, info in jobs.items():
        if info.get("status") == "Pending":
            continue
        output = airsctl.job_list(jid)
        if output:
            s = _parse_job_status(output)
            if s:
                info["status"] = s
                update_job_status(jrun_dir, jid, s)

    if status_filter:
        jobs = {jid: info for jid, info in jobs.items() if info.get("status") in status_filter}

    _print_job_table(jobs, settings)

    # Show scheduler info for active searches
    for sname in tracker.get("searches", {}):
        pid = is_scheduler_running(jrun_dir, sname)
        if pid:
            click.echo(f"\nScheduler active for '{sname}' (pid={pid})")


def _show_search_jobs(tracker: dict, search_name: str, jrun_dir: Path, status_filter: set | None = None):
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
            if tracker["jobs"][jid].get("status") != "Pending":
                output = airsctl.job_list(jid)
                if output:
                    s = _parse_job_status(output)
                    if s:
                        tracker["jobs"][jid]["status"] = s
                        update_job_status(jrun_dir, jid, s)
            jobs[jid] = tracker["jobs"][jid]

    if status_filter:
        jobs = {jid: info for jid, info in jobs.items() if info.get("status") in status_filter}

    _print_job_table(jobs, settings)


def _show_single_job(job_id: str, tracker: dict, jrun_dir):
    output = airsctl.job_list(job_id)
    if not output:
        click.echo(f"No info found for job {job_id}")
        return

    status = _parse_job_status(output)
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
    "pending": "white",
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
    """Try to extract status from airsctl job list output."""
    for line in output.splitlines():
        lower = line.lower()
        for s in ("running", "completed", "succeed", "failed", "stopped", "queued", "starting"):
            if s in lower:
                return s.capitalize()
    return None


@cli.command()
@click.argument("name_or_id")
def stop(name_or_id):
    """Stop a running job or search scheduler by name or ID."""
    settings, jrun_dir = load_settings()
    tracker = load_tracker(jrun_dir)

    # Check if it's a search name — stop scheduler + all active jobs
    if name_or_id in tracker.get("searches", {}):
        stopped_sched = stop_scheduler(jrun_dir, name_or_id)
        if stopped_sched:
            click.echo(f"Stopped scheduler for '{name_or_id}'")

        search = tracker["searches"][name_or_id]
        for jid in search.get("job_ids", []):
            job_info = tracker["jobs"].get(jid, {})
            if job_info.get("status") == "Pending":
                update_job_status(jrun_dir, jid, "Stopped")
            elif not jid.startswith("pending-"):
                _log_airsctl_error(airsctl.job_stop(jid), "job stop")
        click.echo(f"Stopped all jobs in search '{name_or_id}'")
        return

    # Single job
    result = find_job_by_name(jrun_dir, name_or_id)
    if result:
        job_id, job_info = result
        if job_info.get("status") == "Pending":
            update_job_status(jrun_dir, job_id, "Stopped")
            click.echo(f"Cancelled pending job '{name_or_id}'")
            return
        name_or_id = job_id

    _log_airsctl_error(airsctl.job_stop(name_or_id), "job stop")


@cli.command()
@click.argument("name_or_id")
def remove(name_or_id):
    """Remove a job or search: delete config from experiment, cancel on platform, and remove from tracker."""
    settings, jrun_dir = load_settings()
    exp_id = settings.get("experiment_id")
    tracker = load_tracker(jrun_dir)

    if name_or_id in tracker.get("searches", {}):
        # Stop scheduler daemon first
        stop_scheduler(jrun_dir, name_or_id)

        search = tracker["searches"][name_or_id]
        job_ids = search.get("job_ids", [])
        click.echo(f"Removing search '{name_or_id}' ({len(job_ids)} jobs)")
        for jid in job_ids:
            job_info = tracker["jobs"].get(jid, {})
            if jid.startswith("pending-"):
                continue
            _remove_one_job(jid, job_info.get("name"), exp_id)
        remove_search(jrun_dir, name_or_id)
        click.echo(f"Search '{name_or_id}' removed.")
    else:
        result = find_job_by_name(jrun_dir, name_or_id)
        if result:
            job_id, job_info = result
        else:
            job_id = name_or_id
            job_info = tracker.get("jobs", {}).get(job_id, {})

        job_name = job_info.get("name", name_or_id)
        if job_id.startswith("pending-"):
            remove_job(jrun_dir, job_id)
            click.echo(f"Pending job '{job_name}' removed.")
        else:
            _remove_one_job(job_id, job_name, exp_id)
            remove_job(jrun_dir, job_id)
            click.echo(f"Job '{job_name}' removed.")


def _remove_one_job(job_id: str, config_name: str | None, exp_id: str | None):
    """Remove config from experiment and cancel job on platform."""
    if exp_id and config_name:
        _remove_config_from_experiment(exp_id, config_name)
    airsctl.job_cancel(job_id)


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
        airsctl.experiment_modify(tmp_path)
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
