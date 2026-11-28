"""Run the ``jrun`` CLI as a subprocess and parse its ``--json`` output.

The service never imports jrun: every platform action is a ``jrun`` command
run in the caller's workspace. Commands run either as the service account
(``AIRS_AK``/``AIRS_SK`` from the service environment) or, for access
checks, as the caller (``AIRS_TOKEN=<caller token>``, with the service key
removed from the environment).
"""

import json
import os
import subprocess
from pathlib import Path


class JrunError(Exception):
    """A jrun command failed (non-zero exit or unparsable output)."""

    def __init__(self, args: list[str], message: str, returncode: int | None = None):
        self.args_list = args
        self.returncode = returncode
        super().__init__(message)


class JrunRunner:
    def __init__(self, jrun_bin: str = "jrun", base_env: dict | None = None,
                 timeout: int = 900, run=subprocess.run):
        self.jrun_bin = jrun_bin
        self.base_env = dict(os.environ if base_env is None else base_env)
        self.timeout = timeout
        self._run = run

    def env_for(self, token: str | None) -> dict:
        env = dict(self.base_env)
        # Never leak the developer's/host proxy into platform calls.
        for name in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
            env.pop(name, None)
        if token is None:
            env.pop("AIRS_TOKEN", None)
        else:
            # Act strictly as the caller: drop the service key.
            for name in ("AIRS_AK", "AIRS_SK"):
                env.pop(name, None)
            env["AIRS_TOKEN"] = token
        return env

    def run_json(self, args: list[str], *, cwd: Path | None = None,
                 token: str | None = None) -> dict:
        """Run ``jrun <args>`` (args must include ``--json``) and parse stdout."""
        command = [self.jrun_bin] + list(args)
        try:
            result = self._run(command, cwd=str(cwd) if cwd else None,
                               env=self.env_for(token), capture_output=True, text=True,
                               timeout=self.timeout)
        except subprocess.TimeoutExpired as e:
            raise JrunError(args, f"jrun {' '.join(args[:2])} timed out") from e
        if result.returncode != 0:
            raise JrunError(args, _tail(result.stderr) or f"jrun exited {result.returncode}",
                            result.returncode)
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError as e:
            raise JrunError(args, f"jrun printed no JSON: {_tail(result.stdout)}") from e
        if not isinstance(data, dict):
            raise JrunError(args, "jrun JSON result is not an object")
        return data


def _tail(text: str | None, lines: int = 5) -> str:
    """The last lines of jrun's stderr (its ``Error: ...`` line is last)."""
    return "\n".join((text or "").strip().splitlines()[-lines:])
