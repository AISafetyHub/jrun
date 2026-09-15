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


class TestDisplayHelpers:
    def test_pct(self):
        from jrun.formatter import pct
        assert pct(0.723) == "72.3%"
        assert pct(-1) == "-"
        assert pct(None) == "-"
        assert pct(0) == "0.0%"

    def test_format_ms(self):
        from jrun.formatter import format_ms
        assert format_ms("1788921696462") != "-"
        assert len(format_ms("1788921696462")) == 11  # MM-DD HH:MM
        assert format_ms("0") == "-"
        assert format_ms(None) == "-"
        assert format_ms("garbage") == "-"

    def test_snapshot_sort_key(self):
        from jrun.formatter import snapshot_sort_key
        items = [
            {"beginTime": "1000", "acceleratorReq": 1, "acceleratorUtil": 0.5,
             "ownerName": "b"},
            {"beginTime": "2000", "acceleratorReq": 8, "acceleratorUtil": -1,
             "ownerName": "a"},
        ]
        assert sorted(items, key=snapshot_sort_key("time"))[0]["beginTime"] == "1000"
        assert sorted(items, key=snapshot_sort_key("gpus"))[-1]["acceleratorReq"] == 8
        # -1 utilization sorts before any real value
        assert sorted(items, key=snapshot_sort_key("gpu"))[0]["acceleratorUtil"] == -1
        assert sorted(items, key=snapshot_sort_key("owner"))[0]["ownerName"] == "a"

    def test_print_summary_table(self, formatter, capsys):
        rows = [
            ("my-search (2 jobs)", "1 Running, 1 Succeed", "2024-01-01", None, "search"),
            ("single-job", "Running", "2024-01-02", "jid-1", "job"),
        ]
        formatter.print_summary_table(rows, {})
        out = capsys.readouterr().out
        assert "my-search (2 jobs)" in out
        assert "single-job" in out
        assert "NAME" in out and "STATUS" in out

    def test_pad_cjk_aware(self):
        from jrun.formatter import display_width, pad
        assert display_width("任肇兴") == 6
        assert pad("任肇兴", 12) == "任肇兴      "
        assert pad("abc", 5) == "abc  "


class TestPrintExperimentJobTable:
    def test_rows_and_header(self, capsys):
        from jrun.formatter import print_experiment_job_table
        print_experiment_job_table([
            {"id": "j-1", "configName": "train-lr0.01", "status": "Running",
             "createdTime": "1788362902921", "queueName": "q1"},
        ])
        out = capsys.readouterr().out
        assert "NAME" in out and "STATUS" in out and "SUBMITTED" in out
        assert "QUEUE" in out and "ID" in out
        assert "train-lr0.01" in out and "Running" in out and "q1" in out
        assert "j-1" in out
        # submitted column mirrors `jrun job status` (ISO seconds, not MM-DD)
        from datetime import datetime
        expected = datetime.fromtimestamp(1788362902921 / 1000).isoformat(timespec="seconds")
        assert expected in out

    def test_links_replace_id_column(self, capsys):
        from jrun.formatter import print_experiment_job_table
        print_experiment_job_table(
            [{"id": "j-1", "configName": "train", "status": "Running",
              "createdTime": "1788362902921", "queueName": "q1"}],
            job_urls={"j-1": "https://platform.x/job?id=j-1"},
        )
        out = capsys.readouterr().out
        assert "LINK" in out and "ID" not in out.splitlines()[0]
        assert "https://platform.x/job?id=j-1" in out

    def test_missing_fields_use_placeholders(self, capsys):
        from jrun.formatter import print_experiment_job_table
        print_experiment_job_table([{"id": "j-9"}])
        out = capsys.readouterr().out
        assert "?" in out and "j-9" in out

    def test_empty_prints_nothing(self, capsys):
        from jrun.formatter import print_experiment_job_table
        print_experiment_job_table([])
        assert capsys.readouterr().out == ""
