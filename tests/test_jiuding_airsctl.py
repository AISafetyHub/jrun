"""Raw airsctl wrapper (jrun.jiuding.airsctl)."""

from unittest.mock import MagicMock, patch

import pytest

from jrun.errors import PlatformError
from jrun.jiuding.airsctl import Airsctl


def ok(stdout=""):
    return MagicMock(returncode=0, stdout=stdout, stderr="")


class TestCommands:
    @pytest.mark.parametrize("call, argv", [
        (lambda c: c.experiment_list(), ["experiment", "list"]),
        (lambda c: c.experiment_list(exp_id="eid-1"), ["experiment", "list", "-e", "eid-1"]),
        (lambda c: c.experiment_list(exp_name="my-exp"), ["experiment", "list", "-N", "my-exp"]),
        (lambda c: c.experiment_modify("/tmp/x.json"), ["experiment", "modify", "-f", "/tmp/x.json"]),
        (lambda c: c.job_list(), ["job", "list"]),
        (lambda c: c.job_list("j1"), ["job", "list", "-j", "j1"]),
        (lambda c: c.job_stop("j1"), ["job", "stop", "-j", "j1"]),
        (lambda c: c.job_cancel("j1"), ["job", "cancel", "-j", "j1"]),
        (lambda c: c.job_run("eid", "cfg"), ["job", "run", "-e", "eid", "-n", "cfg"]),
    ])
    @patch("subprocess.run")
    def test_argv(self, mock_run, call, argv):
        mock_run.return_value = ok()
        call(Airsctl())
        mock_run.assert_called_once_with(["airsctl"] + argv, capture_output=True, text=True)

    @patch("subprocess.run")
    def test_job_run_public_flags(self, mock_run):
        mock_run.return_value = ok()
        Airsctl().job_run("eid", "cfg", duration_hours=8, enable_checkpoint=True,
                          queue_priority_mode="high", usage_desc="research")
        assert mock_run.call_args.args[0] == [
            "airsctl", "job", "run", "-e", "eid", "-n", "cfg",
            "--duration-hours", "8", "--enable-checkpoint",
            "--queue-priority-mode", "high", "--usage-desc", "research"]

    @patch("subprocess.run")
    def test_job_run_omits_unset_flags(self, mock_run):
        mock_run.return_value = ok()
        Airsctl().job_run("eid", "cfg", duration_hours=None, enable_checkpoint=False)
        assert mock_run.call_args.args[0] == ["airsctl", "job", "run", "-e", "eid", "-n", "cfg"]


GATEWAY = "http://platform-multi.baai.ac.cn"


class TestOffPlatform:
    @patch("subprocess.run")
    def test_no_global_flags_on_pod(self, mock_run):
        mock_run.return_value = ok()
        client = Airsctl(proj_id="p1", projset_id="ps1")
        client.job_stop("j1")
        # Without a gateway the pod annotations supply context: nothing added.
        assert mock_run.call_args.args[0] == ["airsctl", "job", "stop", "-j", "j1"]
        assert not client.off_platform

    @patch("subprocess.run")
    def test_gateway_and_context_flags(self, mock_run):
        mock_run.return_value = ok()
        Airsctl(gateway_url=GATEWAY, proj_id="p1", projset_id="ps1").job_list("j1")
        assert mock_run.call_args.args[0] == [
            "airsctl", "job", "list", "-j", "j1", "-u", GATEWAY, "-P", "ps1", "-p", "p1"]

    @patch("subprocess.run")
    def test_env_configures_gateway_and_default_context(self, mock_run, monkeypatch):
        monkeypatch.setenv("JRUN_AIRSCTL_GATEWAY", GATEWAY)
        monkeypatch.setenv("JRUN_AIRSCTL_PROJSET_ID", "ps-env")
        monkeypatch.setenv("JRUN_AIRSCTL_PROJ_ID", "p-env")
        mock_run.return_value = ok()
        client = Airsctl()
        client.experiment_list()
        assert client.off_platform
        assert mock_run.call_args.args[0][-6:] == ["-u", GATEWAY, "-P", "ps-env", "-p", "p-env"]

    @patch("subprocess.run")
    def test_set_context_overrides_only_given_ids(self, mock_run):
        mock_run.return_value = ok()
        client = Airsctl(gateway_url=GATEWAY, proj_id="p1", projset_id="ps1")
        client.set_context(None, "ps2")
        client.experiment_list()
        assert mock_run.call_args.args[0][-4:] == ["-P", "ps2", "-p", "p1"]

    @patch("subprocess.run")
    def test_gateway_without_context(self, mock_run):
        mock_run.return_value = ok()
        Airsctl(gateway_url=GATEWAY).experiment_list()
        assert mock_run.call_args.args[0] == ["airsctl", "experiment", "list", "-u", GATEWAY]

    @patch("time.sleep")
    @patch("subprocess.run")
    def test_retry_keeps_global_flags(self, mock_run, mock_sleep):
        mock_run.side_effect = [MagicMock(returncode=1, stdout="", stderr="Unauthenticated"),
                                ok()]
        Airsctl(gateway_url=GATEWAY).job_stop("j1")
        assert mock_run.call_args_list[1].args[0][-2:] == ["-u", GATEWAY]


class TestRun:
    @patch("subprocess.run")
    def test_failure_raises_platform_error(self, mock_run):
        mock_run.return_value = MagicMock(returncode=255, stdout="", stderr="login expired")
        with pytest.raises(PlatformError, match=r"experiment list failed \(rc=255\)"):
            Airsctl().experiment_list()

    @patch("subprocess.run")
    def test_check_false_returns_result(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="nope")
        assert Airsctl().job_list("j1").returncode == 1

    @patch("jrun.jiuding.airsctl.time.sleep")
    @patch("subprocess.run")
    def test_unauthenticated_retried_once(self, mock_run, mock_sleep):
        mock_run.side_effect = [
            MagicMock(returncode=1, stdout="", stderr="rpc error: Unauthenticated"),
            ok("done"),
        ]
        result = Airsctl().job_stop("j1")
        assert result.stdout == "done"
        assert mock_run.call_count == 2
        mock_sleep.assert_called_once_with(Airsctl._RETRY_DELAY)

    @patch("jrun.jiuding.airsctl.time.sleep")
    @patch("subprocess.run")
    def test_other_errors_not_retried(self, mock_run, mock_sleep):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="not found")
        with pytest.raises(PlatformError):
            Airsctl().job_stop("j1")
        assert mock_run.call_count == 1
        mock_sleep.assert_not_called()

    @patch("subprocess.run")
    def test_non_utf8_output_is_reported(self, mock_run):
        mock_run.side_effect = [
            UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid"),
            MagicMock(returncode=1, stdout=b"bad output \xff", stderr=b"error \xff"),
        ]
        with pytest.raises(PlatformError, match="error"):
            Airsctl()._run(["experiment", "modify", "-f", "config.json"])
        assert mock_run.call_count == 2
        assert mock_run.call_args_list[1].kwargs["text"] is False
