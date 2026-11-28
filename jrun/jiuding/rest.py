"""Raw REST client for the jiuding platform (platform-multi.baai.ac.cn).

One thin method per endpoint: it takes the request body plus the header
context (project / project set / cluster / zone IDs) and returns the parsed
JSON response unchanged. No jrun models or business rules live here; the
jrun-side logic built on these calls is ``jrun.api.PlatformAPI``.

docs/platform-api.md is the reference for every endpoint.

Auth: AK/SK exchanged for a token via HMAC-SHA256 signing. Credentials
resolve in order:
  0. an explicit token argument, then the AIRS_TOKEN environment variable
     (a ready token, used as-is; the remote service runs jrun this way to
     act with a caller's own permissions)
  1. explicit ak/sk arguments, then AIRS_AK / AIRS_SK environment variables
  2. ~/.jrun/auth.json (written by `jrun login`)
  3. /etc/accesskey/{user-ak,user-sk} (base64-encoded, mounted on jiuding pods)
  4. /tmp/.airs/.token (airsctl's cached token, used as-is)

A token exchanged from AK/SK is re-exchanged shortly before its
``expiresIn`` runs out, so long-running processes keep working.
"""

import base64
import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from jrun.errors import ApiError

PLATFORM_API_BASE = "https://platform-multi.baai.ac.cn"
TOKEN_EXCHANGE_PATH = "/api/v1/users/token/exchange"
USERINFO_PATH = "/api/v1/users/token/userinfo"
QUEUE_SELECT_PATH = "/api/v1/queue/select"
SHARED_QUEUE_LIST_PATH = "/api/v1/shared-queue/list"
SHARED_QUEUE_JOB_COUNT_PATH = "/api/v1/shared-queue/job-count"
SHARED_QUEUE_CONFIG_PATH = "/api/v1/shared-queue/config"
PROJSETS_SELECT_JOINED_PATH = "/api/v1/projsets/select-joined"
EXPERIMENT_PATH = "/api/v1/experiment"
EXPERIMENT_DELETE_PATH = "/api/v1/experiment/delete"
JOB_SELECT_PATH = "/api/v1/job/select"
JOB_SNAPSHOTS_PATH = "/api/v1/job-snapshots/select"

DEFAULT_AUTH_FILE = Path.home() / ".jrun" / "auth.json"
ETC_ACCESSKEY_DIR = Path("/etc/accesskey")
AIRSCTL_TOKEN_FILE = Path("/tmp/.airs/.token")

_SIGNATURE_ALGORITHM = "AIRS-HMAC-SHA256"
# Re-exchange an AK/SK token this many seconds before it expires.
TOKEN_REFRESH_MARGIN = 300
DEFAULT_TOKEN_LIFETIME = 7200


def compute_signature(timestamp: str, sk: str) -> str:
    """Compute the AIRS-HMAC-SHA256 signature for the token exchange."""
    canonical_request = f"POST\n{TOKEN_EXCHANGE_PATH}\n{timestamp}"
    canonical_hash = hashlib.sha256(canonical_request.encode()).hexdigest()
    string_to_sign = f"{_SIGNATURE_ALGORITHM}\n{timestamp}\n{canonical_hash}"
    return hmac.new(sk.encode(), string_to_sign.encode(), hashlib.sha256).hexdigest()


def save_auth(ak: str, sk: str, path: Path | None = None) -> Path:
    """Write AK/SK to ~/.jrun/auth.json with owner-only permissions."""
    path = path or DEFAULT_AUTH_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"ak": ak, "sk": sk}, indent=2) + "\n")
    os.chmod(path, 0o600)
    return path


def paging(page: int = 1, page_size: int = 100) -> dict:
    """The ``paging`` object every list endpoint takes."""
    return {"order": "", "page": page, "pageSize": page_size, "sort": "", "total": 0}


def paginate(fetch, page_size: int = 100, max_pages: int = 20) -> list:
    """Call ``fetch(page)`` (returning a list) until a page comes back short."""
    items: list = []
    for page in range(1, max_pages + 1):
        batch = fetch(page)
        items.extend(batch)
        if len(batch) < page_size:
            break
    return items


class JiudingRest:
    def __init__(self, base_url: str | None = None, opener=None,
                 auth_file: Path | None = None, token_file: Path | None = None,
                 accesskey_dir: Path | None = None,
                 ak: str | None = None, sk: str | None = None,
                 token: str | None = None, clock=time.monotonic):
        self._base = (
            base_url or os.environ.get("JRUN_API_BASE") or PLATFORM_API_BASE
        ).rstrip("/")
        # Bypass http_proxy/https_proxy env vars: they break platform access.
        self._proxy_handler = urllib.request.ProxyHandler({})
        self._opener = opener or urllib.request.build_opener(self._proxy_handler)
        self._auth_file = auth_file or DEFAULT_AUTH_FILE
        self._accesskey_dir = accesskey_dir or ETC_ACCESSKEY_DIR
        self._token_file = token_file or AIRSCTL_TOKEN_FILE
        self._ak = ak
        self._sk = sk
        self._given_token = token
        self._clock = clock
        self._token: str | None = None
        # Monotonic deadline after which an exchanged token is renewed;
        # None for tokens used as-is (no known lifetime).
        self._token_expires_at: float | None = None

    # --- auth ---

    def token(self) -> str:
        """The access token this client authenticates with."""
        return self._resolve_token()

    def exchange_token(self, ak: str, sk: str) -> dict:
        """POST /users/token/exchange: AK/SK for an access token."""
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        signature = compute_signature(timestamp, sk)
        authorization = f"{_SIGNATURE_ALGORITHM} Credential={ak}, Signature={signature}"
        return self._request("POST", TOKEN_EXCHANGE_PATH, headers={
            "Authorization": authorization,
            "X-Airs-Date": timestamp,
        })

    def _resolve_token(self) -> str:
        if self._token and (self._token_expires_at is None
                            or self._clock() < self._token_expires_at):
            return self._token
        self._token = None
        self._token_expires_at = None

        given = self._given_token or os.environ.get("AIRS_TOKEN")
        if given:
            self._token = given.strip()
            return self._token

        ak = self._ak or os.environ.get("AIRS_AK")
        sk = self._sk or os.environ.get("AIRS_SK")
        if not (ak and sk) and self._auth_file.is_file():
            try:
                data = json.loads(self._auth_file.read_text())
                ak = ak or data.get("ak")
                sk = sk or data.get("sk")
            except (json.JSONDecodeError, OSError):
                pass

        if not (ak and sk):
            aksk = _read_etc_accesskey(self._accesskey_dir)
            if aksk:
                ak, sk = aksk

        if ak and sk:
            response = self.exchange_token(ak, sk)
            token = response.get("token") or (response.get("data") or {}).get("token")
            if not token:
                raise ApiError(TOKEN_EXCHANGE_PATH, message=f"no token in response: {response}")
            self._token = token
            self._token_expires_at = self._clock() + max(
                _expires_in(response) - TOKEN_REFRESH_MARGIN, 0)
            return self._token

        if self._token_file.is_file():
            token = self._token_file.read_text().strip()
            if token:
                self._token = token
                return self._token

        raise ApiError(
            TOKEN_EXCHANGE_PATH,
            message="no credentials found. Run 'jrun login' or set AIRS_AK/AIRS_SK.",
        )

    # --- HTTP ---

    def _request(self, method: str, path: str, body: dict | None = None,
                 headers: dict | None = None) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self._base + path, data=data, method=method)
        request.add_header("Content-Type", "application/json")
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        try:
            with self._opener.open(request, timeout=30) as response:
                text = response.read().decode()
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:500]
            raise ApiError(path, e.code, _extract_message(detail)) from e
        except urllib.error.URLError as e:
            raise ApiError(path, message=str(e.reason)) from e
        try:
            return json.loads(text) if text.strip() else {}
        except json.JSONDecodeError as e:
            raise ApiError(path, message=f"invalid JSON response: {text[:200]}") from e

    def _post(self, path: str, body: dict, proj_id: str | None = None,
              projset_id: str | None = None, cluster_id: str | None = None,
              zone_id: str | None = None) -> dict:
        headers = {"AIRS-Token": self._resolve_token()}
        if proj_id:
            headers["Airs-Proj-id"] = proj_id
        if projset_id:
            headers["Airs-Projset-id"] = projset_id
        if cluster_id:
            headers["AIRS-Cluster-ID"] = cluster_id
        if zone_id:
            headers["AIRS-Zone-ID"] = zone_id
        return self._request("POST", path, body, headers)

    # --- endpoints (raw request/response) ---

    def get_userinfo(self) -> dict:
        """GET /users/token/userinfo."""
        return self._request("GET", USERINFO_PATH,
                             headers={"AIRS-Token": self._resolve_token()})

    def projsets_select_joined(self, body: dict | None = None) -> dict:
        """POST /projsets/select-joined: project sets the account belongs to."""
        return self._post(PROJSETS_SELECT_JOINED_PATH, body or {"paging": paging()})

    def queue_select(self, body: dict, **context) -> dict:
        """POST /queue/select: dedicated queues (projset-scoped)."""
        return self._post(QUEUE_SELECT_PATH, body, **context)

    def job_select(self, body: dict, **context) -> dict:
        """POST /job/select: jobs filtered by queue or experiment (projset-scoped)."""
        return self._post(JOB_SELECT_PATH, body, **context)

    def shared_queue_list(self, body: dict, **context) -> dict:
        """POST /shared-queue/list: public/idle shared pools."""
        return self._post(SHARED_QUEUE_LIST_PATH, body, **context)

    def shared_queue_job_count(self, body: dict, **context) -> dict:
        """POST /shared-queue/job-count: per-pool job counts for one mode."""
        return self._post(SHARED_QUEUE_JOB_COUNT_PATH, body, **context)

    def shared_queue_config(self, body: dict, **context) -> dict:
        """POST /shared-queue/config: per-project-set shared-queue limits."""
        return self._post(SHARED_QUEUE_CONFIG_PATH, body, **context)

    def experiment_create(self, body: dict, **context) -> dict:
        """POST /experiment."""
        return self._post(EXPERIMENT_PATH, body, **context)

    def experiment_delete(self, body: dict, **context) -> dict:
        """POST /experiment/delete (archive semantics)."""
        return self._post(EXPERIMENT_DELETE_PATH, body, **context)

    def job_snapshots_select(self, body: dict, **context) -> dict:
        """POST /job-snapshots/select: job resource-utilization snapshots."""
        return self._post(JOB_SNAPSHOTS_PATH, body, **context)


def _expires_in(response: dict) -> int:
    """Token lifetime in seconds from an exchange response (default 7200)."""
    value = response.get("expiresIn") or (response.get("data") or {}).get("expiresIn")
    try:
        return int(value)
    except (TypeError, ValueError):
        return DEFAULT_TOKEN_LIFETIME


def _read_etc_accesskey(directory: Path) -> tuple[str, str] | None:
    """Read base64-encoded AK/SK from /etc/accesskey/{user-ak,user-sk}."""
    try:
        ak = base64.b64decode((directory / "user-ak").read_text().strip()).decode()
        sk = base64.b64decode((directory / "user-sk").read_text().strip()).decode()
    except (OSError, ValueError):
        return None
    return (ak, sk) if ak and sk else None


def _extract_message(body: str) -> str:
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return body.strip()
    if isinstance(data, dict):
        for key in ("message", "reason", "msg", "detail"):
            if data.get(key):
                return str(data[key])
    return body.strip()
