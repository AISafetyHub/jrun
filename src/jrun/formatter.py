import unicodedata

import click

from jrun.project import Project


def display_width(text: str) -> int:
    """Terminal display width (CJK chars count as 2)."""
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1
               for c in text)


def pad(text: str, width: int) -> str:
    """Left-pad-to-width aware of CJK display width."""
    return text + " " * max(0, width - display_width(text))


def pct(value) -> str:
    """Format a 0-1 utilization ratio as a percentage; '-' when unavailable (-1/None)."""
    if value is None or value < 0:
        return "-"
    return f"{value * 100:.1f}%"


def format_ms(value) -> str:
    """Format a millisecond epoch string as MM-DD HH:MM."""
    try:
        ms = int(value)
    except (TypeError, ValueError):
        return "-"
    if ms <= 0:
        return "-"
    from datetime import datetime
    return datetime.fromtimestamp(ms / 1000).strftime("%m-%d %H:%M")


def format_ms_long(value) -> str:
    """Format a millisecond epoch string as YYYY-MM-DDTHH:MM:SS ('-' when unavailable)."""
    try:
        ms = int(value)
    except (TypeError, ValueError):
        return "-"
    if ms <= 0:
        return "-"
    from datetime import datetime
    return datetime.fromtimestamp(ms / 1000).isoformat(timespec="seconds")


def snapshot_sort_key(sort_by: str):
    """Sort key for job-snapshot rows: time/gpus/gpu/gmem/cpu/mem/owner."""
    def util(key):
        return lambda i: (i.get(key) if (i.get(key) or 0) >= 0 else -1)

    keys = {
        "time": lambda i: int(i.get("beginTime") or 0),
        "gpus": lambda i: i.get("acceleratorReq") or 0,
        "gpu": util("acceleratorUtil"),
        "gmem": util("fbmemUtil"),
        "cpu": util("cpuUtil"),
        "mem": util("memUtil"),
        "owner": lambda i: i.get("creatorName") or i.get("ownerName") or "",
    }
    return keys[sort_by]


STATUS_COLORS = {
    "succeed": "green",
    "completed": "green",
    "running": "blue",
    "starting": "cyan",
    "scheduling": "cyan",
    "queued": "bright_black",
    "submitted": "yellow",
    "failed": "red",
    "stopped": "magenta",
    "cancelled": "magenta",
    "pending": "bright_black",
}


def colored_status(status: str, width: int = 0) -> str:
    """Status string padded to width and colored per STATUS_COLORS."""
    color = STATUS_COLORS.get(status.lower())
    padded = f"{status:<{width}}" if width else status
    if color:
        return click.style(padded, fg=color)
    return padded


def hyperlink(url: str, text: str) -> str:
    return f"\033]8;;{url}\033\\{text}\033]8;;\033\\"


def print_experiment_job_table(jobs: list[dict], job_urls: dict | None = None):
    """Print the `jrun experiment jobs` table, mirroring `jrun job status`.

    Columns: NAME / STATUS (colored) / SUBMITTED / QUEUE, then LINK when
    job_urls contains at least one URL, otherwise the raw job ID column.
    """
    if not jobs:
        return
    job_urls = job_urls or {}
    has_links = any(job_urls.get(info.get("id")) for info in jobs)
    rows = [
        (
            info.get("configName") or "?",
            info.get("status") or "?",
            format_ms_long(info.get("createdTime")),
            info.get("queueName") or "-",
            info.get("id") or "?",
        )
        for info in jobs
    ]
    name_w = max([display_width(r[0]) for r in rows] + [4]) + 2
    status_w = max([len(r[1]) for r in rows] + [6]) + 2
    submitted_w = 20  # format_ms_long output is "YYYY-MM-DDTHH:MM:SS"
    queue_w = max([display_width(r[3]) for r in rows] + [5]) + 2

    header = (f"{'NAME':<{name_w}} {'STATUS':<{status_w}} "
              f"{'SUBMITTED':<{submitted_w}} {'QUEUE':<{queue_w}}")
    header += " LINK" if has_links else " ID"
    click.echo(header)
    click.echo("-" * len(header))
    for name, status, submitted, queue, job_id in rows:
        line = (f"{pad(name, name_w)} {colored_status(status, status_w)} "
                f"{submitted:<{submitted_w}} {pad(queue, queue_w)}")
        if has_links:
            url = job_urls.get(job_id)
            if url:
                line += f" {hyperlink(url, 'url')}"
        else:
            line += f" {job_id}"
        click.echo(line)


class StatusFormatter:
    def __init__(self, project: Project):
        self.project = project

    @property
    def has_links(self) -> bool:
        return self.project.platform_ids is not None

    def colored_status(self, status: str, width: int) -> str:
        return colored_status(status, width)

    @staticmethod
    def hyperlink(url: str, text: str) -> str:
        return hyperlink(url, text)

    def print_job_table(self, jobs: dict):
        if not jobs:
            return

        urls = {
            jid: self.project.build_job_url(
                jid,
                platform=info.get("platform"),
                experiment_name=info.get("experiment_name"),
            )
            for jid, info in jobs.items()
        }
        has_links = any(urls.values())
        max_name_len = max(len(info['name']) for info in jobs.values())
        max_status_len = max(len(info.get('status', '?')) for info in jobs.values())
        name_width = max(max_name_len, 4) + 2
        status_width = max(max_status_len, 6) + 2

        header = f"{'NAME':<{name_width}} {'STATUS':<{status_width}} {'SUBMITTED':<20}"
        if has_links:
            header += " LINK"
        click.echo(header)
        click.echo("-" * len(header))

        for jid, info in jobs.items():
            status = info.get('status', '?')
            line = f"{info['name']:<{name_width}} {self.colored_status(status, status_width)} {info.get('submitted_at', ''):<20}"
            url = urls[jid]
            if url:
                line += f" {self.hyperlink(url, 'url')}"
            click.echo(line)

    def print_summary_table(self, rows: list, job_urls: dict):
        """Print the `jrun job status` overview table.

        rows: list of (name, status_str, submitted_at, job_id, row_type)
        where row_type is "search" or "job".
        """
        has_links = any(job_urls.get(row[3]) for row in rows if row[3])
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
                line = f"{name:<{name_width}} {self.colored_status(status_str, status_width)} {submitted_at:<20}"
                if job_id:
                    url = job_urls.get(job_id)
                    if url:
                        line += f" {self.hyperlink(url, 'url')}"
            click.echo(line)
