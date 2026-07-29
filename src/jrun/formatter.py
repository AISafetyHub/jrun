import click

from jrun.project import Project


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


class StatusFormatter:
    def __init__(self, project: Project):
        self.project = project

    @property
    def has_links(self) -> bool:
        return self.project.platform_ids is not None

    def colored_status(self, status: str, width: int) -> str:
        color = STATUS_COLORS.get(status.lower())
        padded = f"{status:<{width}}"
        if color:
            return click.style(padded, fg=color)
        return padded

    @staticmethod
    def hyperlink(url: str, text: str) -> str:
        return f"\033]8;;{url}\033\\{text}\033]8;;\033\\"

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
