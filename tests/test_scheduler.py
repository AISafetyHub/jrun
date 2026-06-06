import os
import signal
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from jrun.scheduler import Scheduler, TERMINAL_STATUSES, ACTIVE_STATUSES


@pytest.fixture
def scheduler(tmp_path):
    jrun_dir = tmp_path / ".jrun"
    jrun_dir.mkdir()
    return Scheduler(
        jrun_dir=jrun_dir,
        search_name="test-search",
        exp_name="exp",
        exp_id="eid-1",
        parallel_trials=2,
        poll_interval=5,
    )


class TestSchedulerPaths:
    def test_scheduler_dir(self, scheduler):
        assert scheduler._scheduler_dir == scheduler.jrun_dir / "scheduler"

    def test_pid_file(self, scheduler):
        assert scheduler._pid_file.name == "test-search.pid"

    def test_log_file(self, scheduler):
        assert scheduler._log_file.name == "test-search.log"

    def test_args_file(self, scheduler):
        assert scheduler._args_file.name == "test-search.args.json"


class TestIsRunning:
    def test_no_pid_file(self, scheduler):
        assert scheduler.is_running() is None

    def test_pid_file_with_dead_process(self, scheduler):
        scheduler._scheduler_dir.mkdir(exist_ok=True)
        scheduler._pid_file.write_text("99999999")
        assert scheduler.is_running() is None
        assert not scheduler._pid_file.exists()

    def test_pid_file_with_live_process(self, scheduler):
        scheduler._scheduler_dir.mkdir(exist_ok=True)
        scheduler._pid_file.write_text(str(os.getpid()))
        assert scheduler.is_running() == os.getpid()

    def test_pid_file_invalid_content(self, scheduler):
        scheduler._scheduler_dir.mkdir(exist_ok=True)
        scheduler._pid_file.write_text("not-a-number")
        assert scheduler.is_running() is None


class TestStop:
    def test_stop_no_process(self, scheduler):
        assert scheduler.stop() is False

    @patch("os.kill")
    def test_stop_running_process(self, mock_kill, scheduler):
        scheduler._scheduler_dir.mkdir(exist_ok=True)
        scheduler._pid_file.write_text(str(os.getpid()))
        mock_kill.side_effect = [None, None]  # first for is_running check, second for SIGTERM
        result = scheduler.stop()
        assert result is True
        assert not scheduler._pid_file.exists()


class TestConstants:
    def test_terminal_statuses(self):
        assert "Failed" in TERMINAL_STATUSES
        assert "Succeed" in TERMINAL_STATUSES
        assert "Completed" in TERMINAL_STATUSES
        assert "Stopped" in TERMINAL_STATUSES
        assert "Cancelled" in TERMINAL_STATUSES

    def test_active_statuses(self):
        assert "Running" in ACTIVE_STATUSES
        assert "Submitted" in ACTIVE_STATUSES
        assert "Queued" in ACTIVE_STATUSES
