import pytest
from unittest.mock import MagicMock, patch
from pathlib import Path

from jrun.formatter import StatusFormatter, STATUS_COLORS
from jrun.project import Project


@pytest.fixture
def formatter(tmp_path):
    project = Project.init("exp", experiment_id="e1", directory=tmp_path)
    return StatusFormatter(project)


@pytest.fixture
def formatter_with_links(tmp_path):
    platform = {"projId": "p1", "projsetId": "ps1", "userId": "u1"}
    project = Project.init("exp", experiment_id="e1", platform=platform, directory=tmp_path)
    return StatusFormatter(project)


class TestStatusFormatter:
    def test_has_links_false(self, formatter):
        assert formatter.has_links is False

    def test_has_links_true(self, formatter_with_links):
        assert formatter_with_links.has_links is True

    def test_colored_status_known(self, formatter):
        result = formatter.colored_status("Running", 10)
        assert len(result) > 0

    def test_colored_status_unknown(self, formatter):
        result = formatter.colored_status("Unknown", 10)
        assert "Unknown" in result

    def test_hyperlink_format(self):
        url = "https://example.com"
        text = "click"
        result = StatusFormatter.hyperlink(url, text)
        assert url in result
        assert text in result
        assert "\033]8;;" in result

    def test_print_job_table_empty(self, formatter, capsys):
        formatter.print_job_table({})
        captured = capsys.readouterr()
        assert captured.out == ""

    def test_print_job_table_with_jobs(self, formatter, capsys):
        jobs = {
            "j1": {"name": "job-1", "status": "Running", "submitted_at": "2024-01-01T00:00:00"},
            "j2": {"name": "job-2", "status": "Failed", "submitted_at": "2024-01-01T00:01:00"},
        }
        formatter.print_job_table(jobs)
        captured = capsys.readouterr()
        assert "NAME" in captured.out
        assert "STATUS" in captured.out
        assert "job-1" in captured.out
        assert "job-2" in captured.out

    def test_print_job_table_with_job_platform(self, formatter, capsys):
        jobs = {
            "j1": {
                "name": "job-1",
                "status": "Succeed",
                "submitted_at": "2026-07-29T14:25:56",
                "experiment_name": "test",
                "platform": {
                    "projId": "p1",
                    "projsetId": "ps1",
                    "userId": "u1",
                    "clusterName": "dx-calc1",
                    "zoneName": "dx-calc1-zonea",
                },
            }
        }
        formatter.print_job_table(jobs)
        output = capsys.readouterr().out
        assert "platform-multi.baai.ac.cn" in output
        assert "clusterName=dx-calc1" in output


class TestStatusColors:
    def test_all_known_statuses_have_colors(self):
        expected = {"succeed", "completed", "running", "starting", "scheduling",
                    "queued", "submitted", "failed", "stopped", "cancelled", "pending"}
        assert set(STATUS_COLORS.keys()) == expected
