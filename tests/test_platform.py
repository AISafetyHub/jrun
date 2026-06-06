import json
import pytest
from unittest.mock import patch, MagicMock

from jrun.errors import PlatformError
from jrun.platform import PlatformClient


class TestParseJobStatus:
    def test_json_status(self):
        output = json.dumps({"status": "running"})
        assert PlatformClient._parse_job_status(output) == "Running"

    def test_json_status_capitalize(self):
        output = json.dumps({"status": "FAILED"})
        assert PlatformClient._parse_job_status(output) == "Failed"

    def test_text_status_running(self):
        output = "Job abc-123 is currently running\n"
        assert PlatformClient._parse_job_status(output) == "Running"

    def test_text_status_succeed(self):
        output = "Status: Succeed\nDuration: 1h"
        assert PlatformClient._parse_job_status(output) == "Succeed"

    def test_text_status_failed(self):
        output = "The job has failed due to OOM"
        assert PlatformClient._parse_job_status(output) == "Failed"

    def test_text_status_cancelled(self):
        output = "Job was cancelled by user"
        assert PlatformClient._parse_job_status(output) == "Cancelled"

    def test_no_status_found(self):
        assert PlatformClient._parse_job_status("no info here") is None

    def test_empty_string(self):
        assert PlatformClient._parse_job_status("") is None

    def test_invalid_json(self):
        assert PlatformClient._parse_job_status("{broken json") is None


class TestPlatformClientCommands:
    @patch("subprocess.run")
    def test_experiment_list_with_id(self, mock_run):
        mock_run.return_value = MagicMock(stdout='{"data": []}', returncode=0)
        client = PlatformClient()
        client.experiment_list(exp_id="eid-1")
        mock_run.assert_called_once_with(
            ["airsctl", "experiment", "list", "-e", "eid-1"],
            capture_output=True, text=True,
        )

    @patch("subprocess.run")
    def test_experiment_list_with_name(self, mock_run):
        mock_run.return_value = MagicMock(stdout="", returncode=0)
        client = PlatformClient()
        client.experiment_list(exp_name="my-exp")
        mock_run.assert_called_once_with(
            ["airsctl", "experiment", "list", "-N", "my-exp"],
            capture_output=True, text=True,
        )

    @patch("subprocess.run")
    def test_job_stop(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        client = PlatformClient()
        client.job_stop("job-id-123")
        mock_run.assert_called_once_with(
            ["airsctl", "job", "stop", "-j", "job-id-123"],
            capture_output=True, text=True,
        )

    @patch("subprocess.run")
    def test_job_run_with_exp_and_config(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        client = PlatformClient()
        client.job_run(exp_id="eid", config_name="cfg")
        mock_run.assert_called_once_with(
            ["airsctl", "job", "run", "-e", "eid", "-n", "cfg"],
            capture_output=True, text=True,
        )

    @patch("subprocess.run")
    def test_get_job_status_success(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0, stdout=json.dumps({"status": "running"})
        )
        client = PlatformClient()
        assert client.get_job_status("j1") == "Running"

    @patch("subprocess.run")
    def test_get_job_status_failure(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="")
        client = PlatformClient()
        assert client.get_job_status("j1") is None


class TestRetryOnUnauthenticated:
    @patch("time.sleep")
    @patch("subprocess.run")
    def test_retry_succeeds_after_unauthenticated(self, mock_run, mock_sleep):
        fail_result = MagicMock(returncode=1, stderr="Unauthenticated(990002)", stdout="")
        success_result = MagicMock(returncode=0, stderr="", stdout='{"status": "running"}')
        mock_run.side_effect = [fail_result, success_result]

        client = PlatformClient()
        result = client._run(["job", "list"], check=True)

        assert result.returncode == 0
        assert mock_run.call_count == 2
        mock_sleep.assert_called_once_with(35)

    @patch("time.sleep")
    @patch("subprocess.run")
    def test_retry_fails_raises_error(self, mock_run, mock_sleep):
        fail_result = MagicMock(returncode=1, stderr="Unauthenticated(990002)", stdout="")
        mock_run.side_effect = [fail_result, fail_result]

        client = PlatformClient()
        with pytest.raises(PlatformError):
            client._run(["job", "list"], check=True)

        assert mock_run.call_count == 2
        mock_sleep.assert_called_once_with(35)

    @patch("time.sleep")
    @patch("subprocess.run")
    def test_no_retry_on_other_errors(self, mock_run, mock_sleep):
        fail_result = MagicMock(returncode=1, stderr="some other error", stdout="")
        mock_run.return_value = fail_result

        client = PlatformClient()
        with pytest.raises(PlatformError):
            client._run(["job", "list"], check=True)

        assert mock_run.call_count == 1
        mock_sleep.assert_not_called()

    @patch("time.sleep")
    @patch("subprocess.run")
    def test_no_retry_when_check_false_but_still_retries_unauthenticated(self, mock_run, mock_sleep):
        fail_result = MagicMock(returncode=1, stderr="Unauthenticated(990002)", stdout="")
        success_result = MagicMock(returncode=0, stderr="", stdout="ok")
        mock_run.side_effect = [fail_result, success_result]

        client = PlatformClient()
        result = client._run(["job", "list"], check=False)

        assert mock_run.call_count == 2
        mock_sleep.assert_called_once_with(35)
