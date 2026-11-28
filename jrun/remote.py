"""Client for a jrun remote-submission service (see docs/remote-service.md).

A config's top-level ``remote:`` block routes some jobs (public by default) to
a service that submits them with its own account. The caller proves who they
are by sending their own jiuding access token in ``X-Jrun-Airs-Token``; the
service checks it (identity, project-set membership, queue visibility)
before acting, and only ever lets a caller see or touch the jobs that the
caller submitted.

No click here: errors are raised as ApiError for the command layer.
"""

import json
import urllib.error
import urllib.request

from jrun.errors import ApiError

PROTOCOL_VERSION = 1
TOKEN_HEADER = "X-Jrun-Airs-Token"
PROTOCOL_HEADER = "X-Jrun-Protocol"

WHOAMI_PATH = "/v1/whoami"
SUBMIT_PATH = "/v1/submit"
JOBS_STATUS_PATH = "/v1/jobs/status"
JOBS_STOP_PATH = "/v1/jobs/stop"
JOBS_REMOVE_PATH = "/v1/jobs/remove"

# Submitting runs a whole `jrun submit` on the service (airsctl calls).
SUBMIT_TIMEOUT = 900
DEFAULT_TIMEOUT = 300


class RemoteClient:
    """JSON-over-HTTP calls to one jrun service, authenticated as the caller."""

    def __init__(self, url: str, token_provider, opener=None):
        self.url = url.rstrip("/")
        self._token_provider = token_provider
        # Same as the platform client: env proxies are bypassed.
        self._opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _post(self, path: str, body: dict | None = None,
              timeout: float = DEFAULT_TIMEOUT) -> dict:
        url = self.url + path
        request = urllib.request.Request(url, data=json.dumps(body or {}).encode(),
                                         method="POST")
        request.add_header("Content-Type", "application/json")
        request.add_header(TOKEN_HEADER, self._token_provider())
        request.add_header(PROTOCOL_HEADER, str(PROTOCOL_VERSION))
        try:
            with self._opener.open(request, timeout=timeout) as response:
                text = response.read().decode()
        except urllib.error.HTTPError as e:
            raise ApiError(url, e.code, _error_message(e.read().decode(errors="replace"))) from e
        except urllib.error.URLError as e:
            raise ApiError(url, message=f"remote service unreachable: {e.reason}") from e
        try:
            data = json.loads(text) if text.strip() else {}
        except json.JSONDecodeError as e:
            raise ApiError(url, message=f"invalid JSON from remote service: {text[:200]}") from e
        if not isinstance(data, dict):
            raise ApiError(url, message="remote service returned a non-object response")
        return data

    def whoami(self) -> dict:
        """The caller as the service sees them: ``{id, alias, ...}``."""
        return self._post(WHOAMI_PATH)

    def submit(self, config: dict, *, only: list[str], overwrite: bool = True) -> dict:
        """Submit the named jobs of ``config`` (the loaded, substituted YAML).

        Returns the service's ``jrun submit --json`` result (``jobs``,
        ``experiment``, ``errors``).
        """
        return self._post(SUBMIT_PATH, {"config": config, "only": list(only),
                                         "overwrite": overwrite},
                          timeout=SUBMIT_TIMEOUT)

    def jobs(self) -> list[dict]:
        """All jobs the caller submitted through the service, freshly refreshed."""
        return list(self._post(JOBS_STATUS_PATH).get("jobs") or [])

    def stop(self, job_ids: list[str]) -> dict:
        """Stop jobs. Returns ``{"results": {job_id: {"ok", "status"?, "error"?}}}``."""
        return self._post(JOBS_STOP_PATH, {"job_ids": list(job_ids)})

    def remove(self, job_ids: list[str]) -> dict:
        """Stop, archive and untrack jobs on the service.

        Returns ``{"removed": [...], "still_active": {id: status},
        "missing": [...]}``; ``missing`` are IDs the service does not track
        for this caller (already gone).
        """
        return self._post(JOBS_REMOVE_PATH, {"job_ids": list(job_ids)},
                          timeout=SUBMIT_TIMEOUT)


class RemoteRegistry:
    """One RemoteClient per service URL, all sending the caller's token.

    The token is resolved lazily (only when a remote call is made) from the
    local credentials via ``PlatformAPI().token()``.
    """

    def __init__(self, token_provider=None, client_factory=RemoteClient):
        self._token_provider = token_provider
        self._factory = client_factory
        self._clients: dict[str, RemoteClient] = {}
        self._token: str | None = None

    def _token_for_request(self) -> str:
        if self._token is None:
            if self._token_provider is not None:
                self._token = self._token_provider()
            else:
                from jrun.api import PlatformAPI
                self._token = PlatformAPI().token()
        return self._token

    def get(self, url: str) -> RemoteClient:
        url = url.rstrip("/")
        if url not in self._clients:
            self._clients[url] = self._factory(url, self._token_for_request)
        return self._clients[url]


def _error_message(body: str) -> str:
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return body.strip()[:500]
    if isinstance(data, dict) and data.get("error"):
        return str(data["error"])
    return body.strip()[:500]
