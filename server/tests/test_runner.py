import subprocess
from types import SimpleNamespace

import pytest

from jrun_server.runner import JrunError, JrunRunner

ENV = {"AIRS_AK": "ak", "AIRS_SK": "sk", "AIRS_TOKEN": "stale", "https_proxy": "http://p",
       "PATH": "/bin"}


def fake_run(stdout="{}", returncode=0, stderr=""):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)
    return run, calls


def test_service_env_keeps_key_drops_token_and_proxy():
    env = JrunRunner(base_env=ENV).env_for(None)
    assert (env["AIRS_AK"], env["AIRS_SK"]) == ("ak", "sk")
    assert "AIRS_TOKEN" not in env and "https_proxy" not in env


def test_caller_env_drops_service_key():
    env = JrunRunner(base_env=ENV).env_for("caller")
    assert env["AIRS_TOKEN"] == "caller"
    assert "AIRS_AK" not in env and "AIRS_SK" not in env


def test_run_json_parses_stdout_and_passes_cwd(tmp_path):
    run, calls = fake_run('{"ok": 1}')
    result = JrunRunner("jrun", base_env=ENV, run=run).run_json(["whoami", "--json"],
                                                               cwd=tmp_path, token="t")
    assert result == {"ok": 1}
    command, kwargs = calls[0]
    assert command == ["jrun", "whoami", "--json"]
    assert kwargs["cwd"] == str(tmp_path)
    assert kwargs["env"]["AIRS_TOKEN"] == "t"


def test_nonzero_exit_reports_stderr_tail():
    run, _ = fake_run("", 1, "progress\nError: no such queue")
    with pytest.raises(JrunError, match="no such queue") as exc:
        JrunRunner(base_env=ENV, run=run).run_json(["queues", "--json"])
    assert exc.value.returncode == 1


def test_non_json_output_is_an_error():
    run, _ = fake_run("hello")
    with pytest.raises(JrunError, match="no JSON"):
        JrunRunner(base_env=ENV, run=run).run_json(["x"])


def test_timeout_is_an_error():
    def run(command, **kwargs):
        raise subprocess.TimeoutExpired(command, 1)
    with pytest.raises(JrunError, match="timed out"):
        JrunRunner(base_env=ENV, run=run).run_json(["submit", "x"])
