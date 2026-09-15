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


class ApiError(JrunError):
    """Platform REST API call failed."""

    def __init__(self, url: str, status_code: int | None = None, message: str = ""):
        self.url = url
        self.status_code = status_code
        self.message = message
        msg = f"{url} failed"
        if status_code is not None:
            msg += f" (HTTP {status_code})"
        if message:
            msg += f": {message}"
        super().__init__(msg)
