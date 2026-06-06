class JrunError(Exception):
    """Base exception for jrun."""
    pass


class PlatformError(JrunError):
    """airsctl command failed."""

    def __init__(self, command: str, returncode: int, stderr: str = "", stdout: str = ""):
        self.command = command
        self.returncode = returncode
        self.stderr = stderr
        self.stdout = stdout
        detail = stderr.strip() or stdout.strip()
        msg = f"airsctl {command} failed (rc={returncode})"
        if detail:
            msg += f": {detail}"
        super().__init__(msg)


class ConfigError(JrunError):
    """Config loading or validation error."""
    pass
