"""Caller verification and per-job access checks.

A caller authenticates with their own jiuding access token. The service
asks the platform about that token by running jrun *as the caller*
(``AIRS_TOKEN=<token>``):

- ``jrun whoami --json``: the token is valid and the account is active; its
  joined project sets; optional alias allowlist.
- ``jrun queues --category idle --projset-id <id> --json``: the shared
  pools the caller can see in that project set. Public and idle are two
  submission modes of the same physical pools, so a pool the caller sees as
  idle is a pool they have access to (they may just not be allowed to submit
  to it in public mode, which is what the service is for).

Each planned job (from the service-side ``jrun submit --dry-run --json``)
must use an allowed category, an allowlisted project set the caller has
joined, and only pools the caller can see.
"""

import hashlib
import threading
import time
from dataclasses import dataclass, field

from jrun_server.runner import JrunError, JrunRunner
from jrun_server.settings import Settings

SHARED = ("public", "idle")


class AuthError(Exception):
    """Missing/invalid caller token (HTTP 401)."""


class Forbidden(Exception):
    """Verified caller, but the request is not allowed (HTTP 403)."""


@dataclass
class Caller:
    id: str
    alias: str
    identity_type: str = ""
    projsets: dict = field(default_factory=dict)  # name -> id


class AccessControl:
    def __init__(self, runner: JrunRunner, settings: Settings, clock=time.monotonic):
        self.runner = runner
        self.settings = settings
        self._clock = clock
        self._cache: dict[str, tuple[float, Caller]] = {}
        self._lock = threading.Lock()

    # --- identity -------------------------------------------------------------

    def verify(self, token: str | None) -> Caller:
        if not token or not token.strip():
            raise AuthError("missing caller token (X-Jrun-Airs-Token)")
        key = hashlib.sha256(token.encode()).hexdigest()
        now = self._clock()
        with self._lock:
            cached = self._cache.get(key)
            if cached and cached[0] > now:
                return cached[1]
        try:
            info = self.runner.run_json(["whoami", "--json"], token=token)
        except JrunError as e:
            raise AuthError(f"token rejected by the platform: {e}") from e
        if not info.get("id"):
            raise AuthError("platform returned no user for this token")
        if not info.get("active", False):
            raise Forbidden(f"account '{info.get('alias')}' is disabled")
        caller = Caller(
            id=str(info["id"]), alias=info.get("alias") or "",
            identity_type=info.get("identity_type") or "",
            projsets={p.get("name"): p.get("id") for p in info.get("projsets") or []
                      if p.get("name") and p.get("id")},
        )
        if self.settings.users and caller.alias not in self.settings.users:
            raise Forbidden(f"user '{caller.alias}' is not allowed to use this service")
        with self._lock:
            self._cache[key] = (now + self.settings.identity_ttl, caller)
        return caller

    # --- per-job checks -----------------------------------------------------------

    def _visible_queues(self, token: str, projset_id: str, category: str,
                        cache: dict) -> set[str]:
        """Queue names the caller sees (shared pools via their idle view)."""
        view = "idle" if category in SHARED else "dedicated"
        key = (projset_id, view)
        if key not in cache:
            try:
                data = self.runner.run_json(["queues", "--category", view, "--projset-id",
                                             projset_id, "--json"], token=token)
            except JrunError as e:
                raise Forbidden(f"cannot list your {view} queues in this project set: {e}") from e
            cache[key] = {q.get("queue_name") for q in data.get("queues") or []
                          if q.get("queue_name")}
        return cache[key]

    def _service_pools(self, projset_id: str, category: str, models: set[str],
                       cache: dict) -> set[str]:
        """Pools the service account could place a job on (for auto jobs)."""
        key = ("service", projset_id, category)
        if key not in cache:
            try:
                data = self.runner.run_json(["queues", "--category", category,
                                             "--projset-id", projset_id, "--json"])
            except JrunError as e:
                raise Forbidden(f"cannot list the service's {category} queues: {e}") from e
            cache[key] = data.get("queues") or []
        return {q.get("queue_name") for q in cache[key]
                if any(str(r.get("model", "")).lower() in models
                       for r in q.get("resources") or [])}

    def check_jobs(self, caller: Caller, token: str, planned: list[dict]):
        """Raise Forbidden unless every planned job is allowed for the caller."""
        if not planned:
            raise Forbidden("nothing to submit")
        cache: dict = {}
        for job in planned:
            name = job.get("name")
            placement = job.get("placement") or {}
            category = placement.get("queue_category") or job.get("queue_category") or "dedicated"
            if category not in self.settings.categories:
                raise Forbidden(f"job '{name}': {category} queues are not submitted by this "
                                f"service (allowed: {', '.join(self.settings.categories)})")
            projset = job.get("project_set_name")
            if not projset:
                raise Forbidden(f"job '{name}': project_set_name is required")
            if projset not in self.settings.project_sets:
                raise Forbidden(f"job '{name}': project set '{projset}' is not served here "
                                f"(served: {', '.join(self.settings.project_sets)})")
            projset_id = caller.projsets.get(projset)
            if not projset_id:
                raise Forbidden(f"job '{name}': you are not a member of project set "
                                f"'{projset}'")
            visible = self._visible_queues(token, projset_id, category, cache)
            if job.get("auto"):
                models = {str(m).lower() for m, _ in job.get("accelerator_choices") or []}
                if not models:
                    raise Forbidden(f"job '{name}': auto job without accelerator models")
                wanted = self._service_pools(projset_id, category, models, cache)
            else:
                wanted = {placement.get("queue_name") or job.get("queue_name")}
            hidden = sorted(q for q in wanted if q not in visible)
            if hidden or not wanted:
                raise Forbidden(f"job '{name}': you have no access to queue(s) "
                                f"{', '.join(map(str, hidden)) or '(none)'} in '{projset}'")
