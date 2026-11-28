"""Raw wrapper around the ``airsctl`` CLI (see docs/airsctl.md).

One method per airsctl subcommand jrun uses; each returns the
``CompletedProcess``. No jrun models or document handling live here; the
jrun-side logic is ``jrun.platform.PlatformClient``.

airsctl prints and accepts snake_case JSON documents.

Off-platform use: on a jiuding pod airsctl reads its gateway and project
context from /etc/downwardapi/annotations. Elsewhere (e.g. the remote-submit
service container) set ``JRUN_AIRSCTL_GATEWAY`` (e.g.
``http://platform-multi.baai.ac.cn``); every call then gets ``-u <gateway>``
plus ``-P <projset> -p <project>`` from the current context
(``set_context`` / ``JRUN_AIRSCTL_PROJSET_ID`` / ``JRUN_AIRSCTL_PROJ_ID``).
Credentials still come from airsctl's own sources (/etc/accesskey or
/tmp/.airs/.token); its ``-t`` flag is ignored when those exist.
"""

import os
import subprocess
import time

from jrun.errors import PlatformError


class Airsctl:
    _UNAUTHENTICATED_PATTERN = "Unauthenticated"
    _RETRY_DELAY = 35

    def __init__(self, command: str = "airsctl", *, gateway_url: str | None = None,
                 proj_id: str | None = None, projset_id: str | None = None):
        self._cmd = command
        self._gateway = gateway_url or os.environ.get("JRUN_AIRSCTL_GATEWAY") or None
        self._proj_id = proj_id or os.environ.get("JRUN_AIRSCTL_PROJ_ID") or None
        self._projset_id = projset_id or os.environ.get("JRUN_AIRSCTL_PROJSET_ID") or None

    @property
    def off_platform(self) -> bool:
        """True when airsctl is driven through an explicit gateway."""
        return bool(self._gateway)

    @property
    def context(self) -> tuple[str | None, str | None]:
        """Current ``(proj_id, projset_id)`` airsctl context."""
        return self._proj_id, self._projset_id

    def set_context(self, proj_id: str | None, projset_id: str | None):
        """Project context for later calls (only sent off-platform)."""
        if proj_id:
            self._proj_id = proj_id
        if projset_id:
            self._projset_id = projset_id

    def _global_args(self) -> list[str]:
        if not self._gateway:
            return []
        args = ["-u", self._gateway]
        if self._projset_id:
            args += ["-P", self._projset_id]
        if self._proj_id:
            args += ["-p", self._proj_id]
        return args

    # --- subprocess ---

    def _exec(self, command: list[str]) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(command, capture_output=True, text=True)
        except UnicodeDecodeError:
            # Some platform errors contain bytes that are not valid UTF-8.
            raw = subprocess.run(command, capture_output=True, text=False)
            return subprocess.CompletedProcess(
                command, raw.returncode,
                raw.stdout.decode("utf-8", errors="replace"),
                raw.stderr.decode("utf-8", errors="replace"),
            )

    def _run(self, args: list[str], check: bool = True) -> subprocess.CompletedProcess:
        """Run ``airsctl <args>``; retry once after an Unauthenticated error."""
        command = [self._cmd] + args + self._global_args()
        result = self._exec(command)
        if result.returncode != 0 and self._is_unauthenticated(result):
            time.sleep(self._RETRY_DELAY)
            result = self._exec(command)
        if check and result.returncode != 0:
            raise PlatformError(" ".join(args[:2]), result.returncode,
                                result.stderr, result.stdout)
        return result

    @classmethod
    def _is_unauthenticated(cls, result: subprocess.CompletedProcess) -> bool:
        output = (result.stderr or "") + (result.stdout or "")
        return cls._UNAUTHENTICATED_PATTERN in output

    # --- subcommands ---

    def experiment_list(self, exp_name: str | None = None, exp_id: str | None = None,
                        check: bool = True) -> subprocess.CompletedProcess:
        """``experiment list`` (all), ``-e <id>`` or ``-N <name>`` (detail document)."""
        args = ["experiment", "list"]
        if exp_id:
            args += ["-e", exp_id]
        elif exp_name:
            args += ["-N", exp_name]
        return self._run(args, check=check)

    def experiment_modify(self, config_path: str) -> subprocess.CompletedProcess:
        """``experiment modify -f <file>``: full experiment document replacement."""
        return self._run(["experiment", "modify", "-f", config_path])

    def job_list(self, job_id: str | None = None, check: bool = False) -> subprocess.CompletedProcess:
        args = ["job", "list"]
        if job_id:
            args += ["-j", job_id]
        return self._run(args, check=check)

    def job_run(self, exp_id: str, config_name: str, *,
                duration_hours: float | None = None,
                enable_checkpoint: bool = False,
                queue_priority_mode: str | None = None,
                usage_desc: str | None = None) -> subprocess.CompletedProcess:
        """``job run -e <id> -n <config>`` plus the public-queue flags that are set."""
        args = ["job", "run", "-e", exp_id, "-n", config_name]
        if duration_hours is not None:
            args += ["--duration-hours", str(duration_hours)]
        if enable_checkpoint:
            args.append("--enable-checkpoint")
        if queue_priority_mode:
            args += ["--queue-priority-mode", queue_priority_mode]
        if usage_desc:
            args += ["--usage-desc", usage_desc]
        return self._run(args)

    def job_stop(self, job_id: str) -> subprocess.CompletedProcess:
        return self._run(["job", "stop", "-j", job_id])

    def job_cancel(self, job_id: str) -> subprocess.CompletedProcess:
        return self._run(["job", "cancel", "-j", job_id])
