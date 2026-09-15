"""REST API client for the jiuding platform (platform-multi.baai.ac.cn).

Complements PlatformClient (which wraps the airsctl CLI) with endpoints that
airsctl does not expose: experiment creation, queue quota listing, and job
resource-utilization snapshots.

Auth: AK/SK exchanged for a token via HMAC-SHA256 signing (see
docs: /api/v1/users/token/exchange). Credentials resolve in order:
  1. AIRS_AK / AIRS_SK environment variables
  2. ~/.jrun/auth.json (written by `jrun login`)
  3. /etc/accesskey/{user-ak,user-sk} (base64-encoded, mounted on jiuding pods)
  4. /tmp/.airs/.token (airsctl's cached token, used as-is)
"""

import base64
import hashlib
import hmac
import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from jrun.errors import ApiError

PLATFORM_API_BASE = "https://platform-multi.baai.ac.cn"
TOKEN_EXCHANGE_PATH = "/api/v1/users/token/exchange"
USERINFO_PATH = "/api/v1/users/token/userinfo"
QUEUE_SELECT_PATH = "/api/v1/queue/select"
PROJSETS_SELECT_JOINED_PATH = "/api/v1/projsets/select-joined"
EXPERIMENT_PATH = "/api/v1/experiment"
EXPERIMENT_DELETE_PATH = "/api/v1/experiment/delete"
JOB_SELECT_PATH = "/api/v1/job/select"
JOB_SNAPSHOTS_PATH = "/api/v1/job-snapshots/select"

DEFAULT_AUTH_FILE = Path.home() / ".jrun" / "auth.json"
ETC_ACCESSKEY_DIR = Path("/etc/accesskey")
AIRSCTL_TOKEN_FILE = Path("/tmp/.airs/.token")

_SIGNATURE_ALGORITHM = "AIRS-HMAC-SHA256"


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


class PlatformAPI:
    def __init__(self, base_url: str | None = None, opener=None,
                 auth_file: Path | None = None, token_file: Path | None = None,
                 accesskey_dir: Path | None = None,
                 ak: str | None = None, sk: str | None = None):
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
        self._token: str | None = None
        self._joined_projsets: list[dict] | None = None

    # --- auth ---

    def exchange_token(self, ak: str, sk: str) -> dict:
        """Exchange AK/SK for an access token."""
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        signature = compute_signature(timestamp, sk)
        authorization = f"{_SIGNATURE_ALGORITHM} Credential={ak}, Signature={signature}"
        return self._request("POST", TOKEN_EXCHANGE_PATH, headers={
            "Authorization": authorization,
            "X-Airs-Date": timestamp,
        })

    def _resolve_token(self) -> str:
        if self._token:
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

    # --- endpoints ---

    def get_userinfo(self) -> dict:
        return self._request("GET", USERINFO_PATH,
                             headers={"AIRS-Token": self._resolve_token()})

    def list_joined_projsets(self) -> list[dict]:
        """Projsets the current account belongs to (cached per instance)."""
        if self._joined_projsets is None:
            body = {"paging": {"order": "", "page": 1, "pageSize": 100, "sort": "", "total": 0}}
            data = self._post(PROJSETS_SELECT_JOINED_PATH, body)
            items = data.get("items") or []
            self._joined_projsets = [
                it["projsetInfo"] for it in items if isinstance(it.get("projsetInfo"), dict)
            ]
        return self._joined_projsets

    def _default_projset_id(self) -> str | None:
        """First joined projset ID, for endpoints that 403 without projset context."""
        projsets = self.list_joined_projsets()
        return projsets[0].get("id") if projsets else None

    def _list_queues_one(self, proj_id: str | None,
                         projset_id: str | None) -> list[dict]:
        body: dict = {
            "paging": {"order": "", "page": 1, "pageSize": 100, "sort": "", "total": 0},
        }
        if proj_id:
            body["projId"] = proj_id
        if projset_id:
            body["projsetId"] = projset_id
        data = self._post(QUEUE_SELECT_PATH, body, proj_id=proj_id, projset_id=projset_id)
        return data.get("queueSummaryInfos") or []

    def list_queues(self, proj_id: str | None = None,
                    projset_id: str | None = None) -> list[dict]:
        # queue/select is scoped to a projset and returns 403 without one.
        # When the caller doesn't specify a projset, query every projset the
        # account has joined and merge the results.
        if projset_id:
            return self._list_queues_one(proj_id=proj_id, projset_id=projset_id)
        projsets = self.list_joined_projsets()
        if not projsets:
            raise ApiError(
                QUEUE_SELECT_PATH,
                message="no joined projset found for this account; "
                        "pass projset_id explicitly.",
            )
        queues: list[dict] = []
        for ps in projsets:
            queues.extend(self._list_queues_one(proj_id=proj_id, projset_id=ps.get("id")))
        return queues

    def find_queue(self, queue_name: str, proj_id: str | None = None,
                   projset_id: str | None = None) -> dict | None:
        for queue in self.list_queues(proj_id=proj_id, projset_id=projset_id):
            if queue.get("name") == queue_name:
                return queue
        return None

    def create_experiment(self, name: str, queue: dict, image: str,
                          proj_id: str | None = None, projset_id: str | None = None,
                          description: str = "", command: str = "sleep 300",
                          envs: dict | None = None, image_region: str = "PUBLIC",
                          priority: str = "high") -> str:
        """Create an experiment with one placeholder config. Returns experiment_id."""
        location = queue_location_info(queue)
        envs = {str(k): str(v) for k, v in (envs or {}).items()}
        proj_id = proj_id or queue.get("projId", "")
        projset_id = projset_id or queue.get("projsetId", "")

        # creatorId is a required int64 proto field; the server does not fill it
        # from the token, so resolve it from userinfo.
        user = self.get_userinfo()
        user_data = user.get("data") if isinstance(user.get("data"), dict) else user
        creator_id = (user_data or {}).get("id") or ""
        creator = (user_data or {}).get("realName") or (user_data or {}).get("alias") or ""
        if not creator_id:
            raise ApiError(EXPERIMENT_PATH,
                           message=f"no user id in userinfo response: {user}")

        resource_config = {
            "dataset": {"items": []},
            "id": "",
            "imageName": "",
            "priority": priority,
            "queueId": location["queue_id"],
            "queueName": location["queue_name"],
            "quotaId": location["quota_id"],
            "podRestartPolicy": "Never",
            "restartScope": "FailedInstanceOnly",
            "roleInfoList": [
                {
                    "name": "Master",
                    "replicas": 1,
                    "resourceRegion": "CUSTOMIZED_ACCELERATOR",
                    "resourceRequestDetail": {
                        "acceleratorModel": location["accelerator_model"],
                        "acceleratorCount": 0,
                        "cpuCores": 2,
                        "memGib": 2,
                        "sharedMemGib": 1,
                        "rdmaSharedCount": 0,
                    },
                }
            ],
            "basicImage": image,
            "imageRegion": image_region,
            "zoneId": location["zone_id"],
            "clusterId": location["cluster_id"],
            "clusterDisplayName": location["cluster_display_name"],
            "zoneDisplayName": location["zone_display_name"],
            "modelStorageInfo": {"repoType": "PRIVATE", "modelVersionId": ""},
            "modelStorageInfoItems1": [],
            "modelStorageInfoItems": [],
            "sourceType": "ONLINE_MODEL",
            "selectedNodes": [],
        }

        body = {
            "projId": proj_id,
            "projsetId": projset_id,
            "name": name,
            "description": description,
            "experimentType": "1",
            "trainFrame": "PyTorch",
            "timeType": 60000,
            "duration": 0,
            "duration1": 0,
            "codeConfig": "0",
            "createdTime": "",
            "delivery": False,
            "mirrorType": "",
            "nativeCluster": "",
            "restartPolicy": "",
            "storageInfo": [
                {"directoryType": 5, "accessMode": 1},
                {"directoryType": 4, "accessMode": 1},
                {"directoryType": 2, "accessMode": 1},
            ],
            "advanceConfigInfos": [
                {
                    "codeConfig": "0",
                    "configName": "config1",
                    "gitPrefix": "",
                    "gitee": "",
                    "gitBranch": "",
                    "gitCommit": "",
                    "command": command,
                    "hyperParameterPresent": [
                        {"key": k, "value": v} for k, v in envs.items()
                    ],
                    "hyperParameter": envs,
                    "slotsPerWorker": 0,
                    "code": "",
                    "codePath": "",
                    "queueName": "",
                    "resourceConfigList": [resource_config],
                    "queueId": "",
                }
            ],
            "experimentId": "",
            "nameSpace": "",
            "creator": creator,
            "creatorId": str(creator_id),
            "profilerInfo": {"enabled": False, "level": "typical"},
            "heteroType": 1,
            "modelStorageInfo": {},
            "flag": False,
        }

        data = self._post(EXPERIMENT_PATH, body, proj_id=proj_id, projset_id=projset_id,
                          cluster_id=location["cluster_id"], zone_id=location["zone_id"])
        experiment_id = data.get("experimentId") or (data.get("data") or {}).get("experimentId")
        if not experiment_id:
            raise ApiError(EXPERIMENT_PATH, message=f"no experimentId in response: {data}")
        return experiment_id

    def experiment_jobs(self, experiment_id: str, page: int = 1,
                        page_size: int = 100, proj_id: str | None = None,
                        projset_id: str | None = None) -> list[dict]:
        """Jobs under one experiment (job/select filtered by experimentId)."""
        body = {
            "paging": {"page": page, "pageSize": page_size, "sort": "", "order": ""},
            "archiveType": "ACTIVE_TYPE",
            "userId": None,
            "experimentType": 1,
            "experimentId": experiment_id,
            "projset_ids": None,
            "proj_ids": None,
            "clusterId": "",
            "zoneId": "",
            "creatorIds": None,
        }
        data = self._post(JOB_SELECT_PATH, body, proj_id=proj_id,
                          projset_id=projset_id)
        return data.get("jobInfos") or []

    def _probe_job_select_projset(self, experiment_id: str,
                                  proj_id: str | None = None) -> str:
        """Find a joined projset whose context lets job/select through (403 otherwise)."""
        projsets = self.list_joined_projsets()
        if not projsets:
            raise ApiError(
                JOB_SELECT_PATH,
                message="no joined projset found for this account; "
                        "pass projset_id explicitly.",
            )
        last_error: ApiError | None = None
        for projset in projsets:
            projset_id = projset.get("id")
            try:
                self.experiment_jobs(experiment_id, page=1, page_size=1,
                                     proj_id=proj_id, projset_id=projset_id)
                return projset_id
            except ApiError as e:
                if e.status_code != 403:
                    raise
                last_error = e
        raise last_error

    def experiment_jobs_all(self, experiment_id: str, page_size: int = 100,
                            max_pages: int = 20, proj_id: str | None = None,
                            projset_id: str | None = None) -> list[dict]:
        """Fetch all pages of jobs under one experiment.

        job/select is projset-scoped and 403s without projset context; when no
        projset_id is given, probe the account's joined projsets and paginate
        with the first one that doesn't 403.
        """
        if projset_id is None:
            projset_id = self._probe_job_select_projset(experiment_id,
                                                        proj_id=proj_id)
        jobs: list[dict] = []
        for page in range(1, max_pages + 1):
            batch = self.experiment_jobs(experiment_id, page=page,
                                         page_size=page_size, proj_id=proj_id,
                                         projset_id=projset_id)
            jobs.extend(batch)
            if len(batch) < page_size:
                break
        return jobs

    def delete_experiment(self, experiment_id: str, experiment_name: str,
                          proj_id: str | None = None,
                          projset_id: str | None = None) -> dict:
        """Delete (archive) one experiment. Returns the raw response."""
        # creatorId is a required field of each experimentItem; the server does
        # not derive it from the token, so resolve it from userinfo (same as
        # create_experiment).
        user = self.get_userinfo()
        user_data = user.get("data") if isinstance(user.get("data"), dict) else user
        creator_id = (user_data or {}).get("id") or ""
        if not creator_id:
            raise ApiError(EXPERIMENT_DELETE_PATH,
                           message=f"no user id in userinfo response: {user}")
        # experiment/delete needs projset context like the other projset-scoped
        # endpoints; fall back to the account's first joined projset.
        if projset_id is None:
            projset_id = self._default_projset_id()
        body = {
            "experimentItems": [
                {
                    "experimentId": experiment_id,
                    "experimentName": experiment_name,
                    "creatorId": str(creator_id),
                    "archive": 1,
                }
            ]
        }
        return self._post(EXPERIMENT_DELETE_PATH, body, proj_id=proj_id,
                          projset_id=projset_id)

    def job_snapshots(self, proj_id: str | None = None, projset_id: str | None = None,
                      cluster_ids: list[str] | None = None, zone_ids: list[str] | None = None,
                      owner: str | None = None, page: int = 1,
                      page_size: int = 100) -> list[dict]:
        # job-snapshots/select is projset-scoped like queue/select.
        if projset_id is None:
            projset_id = self._default_projset_id()
        body: dict = {
            "paging": {"page": page, "pageSize": page_size, "sort": "", "order": ""},
            "projsetId": projset_id,
            "projId": proj_id,
            "queueIds": [],
            "clusterIds": cluster_ids or [],
            "zoneIds": zone_ids or [],
        }
        if owner:
            body["ownerName"] = owner
        data = self._post(JOB_SNAPSHOTS_PATH, body, proj_id=proj_id, projset_id=projset_id)
        return data.get("items") or []

    def job_snapshots_all(self, proj_id: str | None = None,
                          projset_id: str | None = None,
                          cluster_ids: list[str] | None = None,
                          zone_ids: list[str] | None = None,
                          owner: str | None = None, page_size: int = 100,
                          max_pages: int = 20) -> list[dict]:
        """Fetch all pages of job snapshots."""
        items: list[dict] = []
        for page in range(1, max_pages + 1):
            batch = self.job_snapshots(
                proj_id=proj_id, projset_id=projset_id,
                cluster_ids=cluster_ids, zone_ids=zone_ids, owner=owner,
                page=page, page_size=page_size,
            )
            items.extend(batch)
            if len(batch) < page_size:
                break
        return items

    # --- quota parsing ---

    @staticmethod
    def queue_free_resources(queue: dict) -> list[dict]:
        """Summarize per-accelerator-model total/used/free from a queue's usedQuota."""
        computing = (queue.get("usedQuota") or {}).get("computing") or {}
        rows = []
        for model, quota in computing.items():
            if model in ("cpu", "memory"):
                continue
            total = _sum_priorities(quota.get("total"))
            used = _sum_priorities(quota.get("used"))
            rows.append({
                "model": model,
                "total": total,
                "used": used,
                "free": total - used,
            })
        return rows


def queue_location_info(queue: dict) -> dict:
    """Extract submission-relevant location fields from a queue/select row."""
    cluster_display = ""
    zone_display = ""
    for cluster_info in queue.get("quotaInfoList") or []:
        cluster_display = cluster_info.get("clusterDisplayName", "")
        for zone_info in cluster_info.get("zoneQuotaInfoList") or []:
            zone_display = zone_info.get("zoneDisplayName", "")
            break
        break

    accelerator_model = ""
    for quota in queue.get("quotaDetailList") or []:
        accelerator_model = (quota.get("resourceDetail") or {}).get("acceleratorModel", "")
        break

    return {
        "queue_id": queue.get("id", ""),
        "queue_name": queue.get("name", ""),
        "quota_id": queue.get("quotaId", ""),
        "cluster_id": queue.get("clusterId", ""),
        "zone_id": queue.get("zoneId", ""),
        "cluster_display_name": cluster_display,
        "zone_display_name": zone_display,
        "accelerator_model": accelerator_model,
        "proj_id": queue.get("projId", ""),
        "projset_id": queue.get("projsetId", ""),
    }


def _read_etc_accesskey(directory: Path) -> tuple[str, str] | None:
    """Read base64-encoded AK/SK from /etc/accesskey/{user-ak,user-sk}."""
    try:
        ak = base64.b64decode((directory / "user-ak").read_text().strip()).decode()
        sk = base64.b64decode((directory / "user-sk").read_text().strip()).decode()
    except (OSError, ValueError):
        return None
    return (ak, sk) if ak and sk else None


def _sum_priorities(priority_map: dict | None) -> float:
    return sum(v for v in (priority_map or {}).values() if isinstance(v, (int, float)))


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
