"""jrun-side logic on top of the jiuding REST API.

The raw endpoints and auth live in ``jrun.jiuding.rest.JiudingRest``; this
module adds what jrun needs on top of them: projset fan-out, project/queue
name resolution, shared-queue submit budgets, queue waiting counts, free-card
parsing, and the experiment-create body.
"""

from jrun.errors import ApiError
from jrun.jiuding.enums import JobState
from jrun.jiuding.rest import (  # noqa: F401  (re-exported for callers/tests)
    EXPERIMENT_DELETE_PATH, EXPERIMENT_PATH, JOB_SELECT_PATH, QUEUE_SELECT_PATH,
    USERINFO_PATH,
    JiudingRest, compute_signature, paginate, paging, save_auth,
)
from jrun.models import (SHARED_CATEGORIES, mount_entries_rest,
                         normalize_queue_category)


class PlatformAPI(JiudingRest):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._joined_projsets: list[dict] | None = None

    def _current_user(self, path: str) -> tuple[str, str]:
        """(creator_id, display name) of the token's user.

        creatorId is a required int64 field on experiment create/delete; the
        server does not derive it from the token.
        """
        user = self.get_userinfo()
        data = user.get("data") if isinstance(user.get("data"), dict) else user
        data = data or {}
        creator_id = data.get("id") or ""
        if not creator_id:
            raise ApiError(path, message=f"no user id in userinfo response: {user}")
        return str(creator_id), data.get("realName") or data.get("alias") or ""

    def whoami(self) -> dict:
        """The token's user and joined project sets, normalized.

        ``active`` is False when userinfo reports a disable time (an active
        account has ``disabledTime`` "0"; see docs/platform-api.md §2).
        """
        user = self.get_userinfo()
        data = user.get("data") if isinstance(user.get("data"), dict) else user
        data = data or {}
        if not data.get("id"):
            raise ApiError(USERINFO_PATH, message=f"no user id in userinfo response: {user}")
        disabled = str(data.get("disabledTime") or "0").strip()
        return {
            "id": str(data["id"]),
            "alias": data.get("alias") or "",
            "real_name": data.get("realName") or "",
            "identity_type": data.get("identityType") or "",
            "active": disabled in ("", "0"),
            "projsets": [{"id": p.get("id"), "name": p.get("name")}
                         for p in self.list_joined_projsets()],
        }

    def list_joined_projsets(self) -> list[dict]:
        """Projsets the current account belongs to (cached per instance)."""
        if self._joined_projsets is None:
            body = {"paging": {"order": "", "page": 1, "pageSize": 100, "sort": "", "total": 0}}
            data = self.projsets_select_joined(body)
            items = data.get("items") or []
            self._joined_projsets = [
                it["projsetInfo"] for it in items if isinstance(it.get("projsetInfo"), dict)
            ]
        return self._joined_projsets

    def _default_projset_id(self) -> str | None:
        """First joined projset ID, for endpoints that 403 without projset context."""
        projsets = self.list_joined_projsets()
        return projsets[0].get("id") if projsets else None

    def resolve_project_context(self, project_set_name: str | None = None,
                                project_name: str | None = None) -> tuple[str | None, str | None]:
        """Resolve readable project names to the IDs required by platform APIs."""
        if not project_set_name and not project_name:
            return None, None
        projsets = self.list_joined_projsets()
        candidates = projsets
        if project_set_name:
            candidates = [p for p in candidates if p.get("name") == project_set_name]
            if not candidates:
                available = ", ".join(sorted(p.get("name", "") for p in projsets))
                raise ValueError(
                    f"project_set_name '{project_set_name}' not found; available: {available}"
                )
        projset_ids = {p.get("id") for p in candidates if p.get("id")}
        if not project_name:
            if len(projset_ids) != 1:
                raise ValueError("project_set_name must identify exactly one project set")
            return None, next(iter(projset_ids))

        rows: list[dict] = []
        if projset_ids:
            for projset_id in projset_ids:
                rows.extend(self._list_queues_one(None, projset_id))
        else:
            rows.extend(self.list_queues())
        matches = [
            row for row in rows
            if row.get("projName") == project_name
            and (not project_set_name or row.get("projsetName") == project_set_name)
        ]
        contexts = {(row.get("projId"), row.get("projsetId")) for row in matches}
        contexts.discard((None, None))
        if len(contexts) != 1:
            available = ", ".join(sorted({row.get("projName", "") for row in rows if row.get("projName")}))
            raise ValueError(
                f"project_name '{project_name}' not found uniquely; available: {available}"
            )
        return next(iter(contexts))

    def _list_queues_one(self, proj_id: str | None,
                         projset_id: str | None) -> list[dict]:
        body: dict = {
            "paging": {"order": "", "page": 1, "pageSize": 100, "sort": "", "total": 0},
        }
        if proj_id:
            body["projId"] = proj_id
        if projset_id:
            body["projsetId"] = projset_id
        data = self.queue_select(body, proj_id=proj_id, projset_id=projset_id)
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

    def list_shared_queues(self, proj_id: str | None = None,
                           projset_id: str | None = None,
                           queue_category: str | None = None,
                           page_size: int = 100) -> list[dict]:
        """List public/idle shared queues visible to a project set.

        The platform exposes one shared-pool list; ``queue_category`` is a
        submission mode and is attached to each normalized row for callers.
        """
        body: dict = {
            "paging": {"order": "", "page": 1, "pageSize": page_size,
                       "sort": "", "total": 0},
        }
        if proj_id:
            body["projId"] = proj_id
        if projset_id:
            body["projsetId"] = projset_id
        if queue_category:
            body["queueCategory"] = normalize_queue_category(queue_category)
        data = self.shared_queue_list(body, proj_id=proj_id,
                          projset_id=projset_id)
        rows = data.get("sharedQueues") or data.get("items") or []
        result = []
        for item in rows:
            queue = item.get("queue") if isinstance(item, dict) and isinstance(item.get("queue"), dict) else item
            if not isinstance(queue, dict):
                continue
            normalized = dict(queue)
            if queue_category:
                    normalized["queueCategory"] = normalize_queue_category(queue_category)
            result.append(normalized)
        return result

    def list_shared_queue_job_counts(self, proj_id: str | None = None,
                                     projset_id: str | None = None,
                                     queue_category: str = "public",
                                     zone_id: str | None = None,
                                     only_queueing: bool = False,
                                     all_projsets: bool = False,
                                     page_size: int = 100) -> list[dict]:
        """Per-queue job counts for public or idle mode.

        Counts cover this project set, or every project set with
        ``all_projsets`` (what the shared pool's FIFO actually sees).
        """
        if projset_id is None:
            projset_id = self._default_projset_id()
        body: dict = {
            "paging": paging(page_size=page_size),
            "queueCategory": normalize_queue_category(queue_category),
            "allProjsets": all_projsets,
            "onlyQueueing": only_queueing,
        }
        if projset_id:
            body["projsetId"] = projset_id
        if zone_id:
            body["zoneId"] = zone_id
        data = self.shared_queue_job_count(body, proj_id=proj_id, projset_id=projset_id)
        return data.get("items") or data.get("sharedQueueJobCounts") or []

    def shared_waiting_counts(self, proj_id: str | None = None,
                              projset_id: str | None = None) -> dict:
        """Jobs waiting for resources on each shared pool, across all project sets.

        ``{(category, queue_id): waiting}`` for public and idle, where waiting =
        jobCount - runningCount - approvingCount (``queueingCount`` misses idle
        jobs in Scheduling; measured 2026-11-25).
        """
        result = {}
        for category in ("QUEUE_CATEGORY_PUBLIC", "QUEUE_CATEGORY_IDLE"):
            rows = self.list_shared_queue_job_counts(
                proj_id=proj_id, projset_id=projset_id, queue_category=category,
                all_projsets=True,
            )
            for row in rows:
                queue = row.get("queue") or {}
                queue_id = queue.get("queueId") or queue.get("id")
                if not queue_id:
                    continue
                waiting = (int(row.get("jobCount") or 0) - int(row.get("runningCount") or 0)
                           - int(row.get("approvingCount") or 0))
                result[(category, queue_id)] = max(waiting, int(row.get("queueingCount") or 0), 0)
        return result

    def queue_waiting_count(self, queue_id: str, proj_id: str | None = None,
                            projset_id: str | None = None) -> int:
        """Number of jobs waiting for resources (Pending/Scheduling) in one queue.

        Uses job/select filtered by queueId (projset-scoped); only the paging
        total is read.
        """
        body = {
            "paging": {"page": 1, "pageSize": 1, "sort": "", "order": ""},
            "queueId": queue_id,
            "experimentType": 1,
            "job_state": list(JobState.WAITING),
        }
        data = self.job_select(body, proj_id=proj_id, projset_id=projset_id)
        total = (data.get("paging") or {}).get("total")
        try:
            return int(total)
        except (TypeError, ValueError):
            return len(data.get("jobInfos") or [])

    def shared_queue_limits(self, proj_id: str | None = None,
                            projset_id: str | None = None) -> dict:
        """Read shared-queue per-project-set limits from platform config."""
        if projset_id is None:
            projset_id = self._default_projset_id()
        data = self.shared_queue_config({
            "projId": proj_id or "",
            "projsetId": projset_id or "",
        }, proj_id=proj_id, projset_id=projset_id)
        result = {}
        for item in data.get("items") or []:
            key = item.get("configKey")
            if not key:
                continue
            value = item.get("configValue")
            try:
                value = int(value)
            except (TypeError, ValueError):
                pass
            result[key] = value
        return result

    def list_category_queues(self, queue_category: str | None,
                             proj_id: str | None = None,
                             projset_id: str | None = None) -> list[dict]:
        """Dedicated queues (queue/select) or the shared pools for public/idle."""
        category = normalize_queue_category(queue_category)
        if category in SHARED_CATEGORIES:
            return self.list_shared_queues(proj_id=proj_id, projset_id=projset_id,
                                           queue_category=category)
        return self.list_queues(proj_id=proj_id, projset_id=projset_id)

    def shared_queue_budgets(self, proj_id: str | None = None,
                             projset_id: str | None = None) -> dict:
        """Remaining public/idle submit slots for a project set.

        ``{category: remaining}`` where remaining = per-projset limit minus the
        running + queueing jobs in that mode; None when no limit is configured.
        """
        limits = self.shared_queue_limits(proj_id=proj_id, projset_id=projset_id)
        budgets = {}
        for category, limit_key in (("QUEUE_CATEGORY_PUBLIC", "max_jobs_per_projset"),
                                    ("QUEUE_CATEGORY_IDLE", "max_idle_jobs_per_projset")):
            limit = limits.get(limit_key)
            if not isinstance(limit, int):
                budgets[category] = None
                continue
            rows = self.list_shared_queue_job_counts(
                proj_id=proj_id, projset_id=projset_id, queue_category=category,
            )
            used = sum(int(row.get("runningCount") or 0) + int(row.get("queueingCount") or 0)
                       for row in rows)
            budgets[category] = max(limit - used, 0)
        return budgets

    def find_queue(self, queue_name: str, proj_id: str | None = None,
                   projset_id: str | None = None,
                   queue_category: str | None = None,
                   project_set_name: str | None = None,
                   project_name: str | None = None,
                   region_name: str | None = None,
                   zone_name: str | None = None) -> dict | None:
        resolved_proj_id, resolved_projset_id = self.resolve_project_context(
            project_set_name, project_name
        )
        queues = self.list_category_queues(
            queue_category, proj_id=proj_id or resolved_proj_id,
            projset_id=projset_id or resolved_projset_id,
        )
        matches = filter_queues(
            [queue for queue in queues if queue.get("name") == queue_name],
            project_set_name=project_set_name, project_name=project_name,
            region_name=region_name, zone_name=zone_name,
        )
        if len(matches) <= 1:
            if not matches:
                return None
            return with_project_context(matches[0], resolved_proj_id, resolved_projset_id,
                                        project_name, project_set_name)
        locations = ", ".join(
            f"{queue_location_info(q).get('region_name') or 'unknown'}/"
            f"{queue_location_info(q).get('zone_name') or 'unknown'}"
            for q in matches
        )
        raise ValueError(
            f"queue '{queue_name}' has multiple locations: {locations}; "
            "set project_set_name/project_name/region_name/zone_name"
        )

    def create_experiment(self, name: str, queue: dict, image: str,
                          proj_id: str | None = None, projset_id: str | None = None,
                          description: str = "", command: str = "sleep 300",
                          envs: dict | None = None, image_region: str = "PUBLIC",
                          priority: str = "high", queue_category: str | None = None,
                          train_frame: str = "PyTorch", restart_policy: str = "Never",
                          mounts: dict | None = None,
                          platform_overrides: dict | None = None) -> str:
        """Create an experiment with one placeholder config. Returns experiment_id.

        ``mounts`` is the config's ``experiment.mounts`` (None mounts every
        default directory read-write, like the platform UI).
        """
        location = queue_location_info(queue)
        envs = {str(k): str(v) for k, v in (envs or {}).items()}
        proj_id = proj_id or queue.get("projId", "")
        projset_id = projset_id or queue.get("projsetId", "")

        creator_id, creator = self._current_user(EXPERIMENT_PATH)

        category = normalize_queue_category(queue_category or queue.get("queueCategory"))
        resource_config = {
            "dataset": {"items": []},
            "id": "",
            "imageName": "",
            "priority": priority,
            "queueId": location["queue_id"],
            "queueName": location["queue_name"],
            "quotaId": location["quota_id"],
            "sharedQuotaId": location.get("shared_quota_id", ""),
            "queueCategory": category,
            "podRestartPolicy": restart_policy,
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
            "clusterDisplayName": location["region_name"],
            "zoneDisplayName": location["zone_name"],
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
            "trainFrame": train_frame,
            "timeType": 60000,
            "duration": 0,
            "duration1": 0,
            "codeConfig": "0",
            "createdTime": "",
            "delivery": False,
            "mirrorType": "",
            "nativeCluster": "",
            "restartPolicy": restart_policy,
            "storageInfo": mount_entries_rest(mounts),
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
            "creatorId": creator_id,
            "profilerInfo": {"enabled": False, "level": "typical"},
            "heteroType": 1,
            "modelStorageInfo": {},
            "flag": False,
        }
        if platform_overrides:
            body.update(platform_overrides)

        data = self.experiment_create(body, proj_id=proj_id, projset_id=projset_id,
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
        data = self.job_select(body, proj_id=proj_id,
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
        return paginate(
            lambda page: self.experiment_jobs(experiment_id, page=page,
                                              page_size=page_size, proj_id=proj_id,
                                              projset_id=projset_id),
            page_size=page_size, max_pages=max_pages,
        )

    def delete_experiment(self, experiment_id: str, experiment_name: str,
                          proj_id: str | None = None,
                          projset_id: str | None = None) -> dict:
        """Delete (archive) one experiment. Returns the raw response."""
        creator_id, _ = self._current_user(EXPERIMENT_DELETE_PATH)
        # experiment/delete needs projset context like the other projset-scoped
        # endpoints; fall back to the account's first joined projset.
        if projset_id is None:
            projset_id = self._default_projset_id()
        body = {
            "experimentItems": [
                {
                    "experimentId": experiment_id,
                    "experimentName": experiment_name,
                    "creatorId": creator_id,
                    "archive": 1,
                }
            ]
        }
        return self.experiment_delete(body, proj_id=proj_id,
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
        data = self.job_snapshots_select(body, proj_id=proj_id, projset_id=projset_id)
        return data.get("items") or []

    def job_snapshots_all(self, proj_id: str | None = None,
                          projset_id: str | None = None,
                          cluster_ids: list[str] | None = None,
                          zone_ids: list[str] | None = None,
                          owner: str | None = None, page_size: int = 100,
                          max_pages: int = 20) -> list[dict]:
        """Fetch all pages of job snapshots."""
        return paginate(
            lambda page: self.job_snapshots(
                proj_id=proj_id, projset_id=projset_id,
                cluster_ids=cluster_ids, zone_ids=zone_ids, owner=owner,
                page=page, page_size=page_size,
            ),
            page_size=page_size, max_pages=max_pages,
        )

    # --- quota parsing ---

    @staticmethod
    def queue_free_resources(queue: dict) -> list[dict]:
        """Summarize per-accelerator-model total/used/free from a queue's usedQuota."""
        if queue.get("quota") and queue.get("used"):
            quota = queue.get("quota") or {}
            used = queue.get("used") or {}
            total = quota.get("acceleratorCount", 0) or 0
            used_count = used.get("acceleratorCount", 0) or 0
            return [{
                "model": queue.get("acceleratorModel") or quota.get("acceleratorModel", ""),
                "total": total,
                "used": used_count,
                "free": total - used_count,
            }]
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
    region_name = (
        queue.get("clusterDisplayName") or queue.get("clusterName") or ""
    )
    zone_name = queue.get("zoneDisplayName") or queue.get("zoneName") or ""
    for cluster_info in queue.get("quotaInfoList") or []:
        region_name = (
            cluster_info.get("clusterDisplayName")
            or cluster_info.get("clusterName")
            or region_name
        )
        for zone_info in cluster_info.get("zoneQuotaInfoList") or []:
            zone_name = (
                zone_info.get("zoneDisplayName")
                or zone_info.get("zoneName")
                or zone_name
            )
            break
        break

    accelerator_model = queue.get("acceleratorModel", "")
    for quota in queue.get("quotaDetailList") or []:
        accelerator_model = (quota.get("resourceDetail") or {}).get("acceleratorModel", "")
        break

    return {
        "queue_id": queue.get("id", "") or queue.get("queueId", ""),
        "queue_name": queue.get("name", ""),
        "quota_id": queue.get("quotaId", ""),
        "shared_quota_id": queue.get("sharedQuotaId", ""),
        "cluster_id": queue.get("clusterId", ""),
        "zone_id": queue.get("zoneId", ""),
        "region_name": region_name,
        "zone_name": zone_name,
        "accelerator_model": accelerator_model,
        "proj_id": queue.get("projId", ""),
        "projset_id": queue.get("projsetId", ""),
        "project_name": queue.get("projName", ""),
        "project_set_name": queue.get("projsetName", ""),
        "queue_category": queue.get("queueCategory", ""),
        "accelerator_count": (queue.get("quota") or {}).get("acceleratorCount"),
        "total": (queue.get("quota") or {}).get("acceleratorCount"),
        "used": (queue.get("used") or {}).get("acceleratorCount"),
    }


def shared_waiting_ahead(counts: dict | None, category: str, queue_id) -> int | None:
    """Jobs waiting ahead of a new public/idle job on one shared pool.

    ``counts`` comes from ``PlatformAPI.shared_waiting_counts`` (None =
    unknown). Shared pools are FIFO; public jobs (priority high) only queue
    behind public ones, idle jobs (low) behind both.
    """
    if counts is None:
        return None
    category = normalize_queue_category(category)
    waiting = counts.get(("QUEUE_CATEGORY_PUBLIC", queue_id), 0)
    if category == "QUEUE_CATEGORY_IDLE":
        waiting += counts.get(("QUEUE_CATEGORY_IDLE", queue_id), 0)
    return waiting


def filter_queues(queues: list[dict], project_set_name: str | None = None,
                  project_name: str | None = None, region_name: str | None = None,
                  zone_name: str | None = None) -> list[dict]:
    """Keep the queue rows that match the given named location.

    Project-set/project filters only apply when the rows carry those names
    (shared pools do not).
    """
    matches = list(queues)
    if project_set_name and any(queue.get("projsetName") for queue in matches):
        matches = [
            queue for queue in matches
            if not queue.get("projsetName") or queue.get("projsetName") == project_set_name
        ]
    if project_name and any(queue.get("projName") for queue in matches):
        matches = [
            queue for queue in matches
            if (queue.get("projName") or "") == project_name
        ]
    if region_name:
        matches = [queue for queue in matches if region_name in _queue_region_names(queue)]
    if zone_name:
        matches = [queue for queue in matches if zone_name in _queue_zone_names(queue)]
    return matches


def with_project_context(queue: dict, proj_id: str | None, projset_id: str | None,
                         project_name: str | None, project_set_name: str | None) -> dict:
    """Copy of a queue row with missing project IDs/names filled in."""
    result = dict(queue)
    for key, value in (("projId", proj_id), ("projsetId", projset_id),
                       ("projName", project_name), ("projsetName", project_set_name)):
        if value and not result.get(key):
            result[key] = value
    return result


def _queue_region_names(queue: dict) -> set[str]:
    names = {
        str(queue.get(key)) for key in ("clusterName", "clusterDisplayName")
        if queue.get(key)
    }
    for cluster_info in queue.get("quotaInfoList") or []:
        names.update(
            str(cluster_info.get(key)) for key in ("clusterName", "clusterDisplayName")
            if cluster_info.get(key)
        )
    return names


def _queue_zone_names(queue: dict) -> set[str]:
    names = {
        str(queue.get(key)) for key in ("zoneName", "zoneDisplayName")
        if queue.get(key)
    }
    for cluster_info in queue.get("quotaInfoList") or []:
        for zone_info in cluster_info.get("zoneQuotaInfoList") or []:
            names.update(
                str(zone_info.get(key)) for key in ("zoneName", "zoneDisplayName")
                if zone_info.get(key)
            )
    return names


def _sum_priorities(priority_map: dict | None) -> float:
    return sum(v for v in (priority_map or {}).values() if isinstance(v, (int, float)))
