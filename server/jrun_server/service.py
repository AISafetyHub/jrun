"""Remote-submission service logic (HTTP-independent, unit-testable).

Each caller gets a private jrun workspace ``<data>/users/<user id>/`` (a
``jrun init`` project). Every jrun command for that caller runs there, so the
workspace's tracker is the authority on which jobs belong to whom: a caller
can only ever see, stop or remove jobs recorded in their own workspace.
"""

import re
import threading
import time
from pathlib import Path

from jrun_server.access import AccessControl, Caller, Forbidden
from jrun_server.rewrite import RewriteError, dump_config, rewrite_config
from jrun_server.runner import JrunError, JrunRunner
from jrun_server.settings import Settings

# Job IDs / names passed on a jrun command line: no option injection.
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
MAX_JOBS_PER_REQUEST = 500


class BadRequest(Exception):
    """Malformed request (HTTP 400)."""


def _ids(body: dict, key: str) -> list[str]:
    values = body.get(key)
    if not isinstance(values, list) or not values:
        raise BadRequest(f"'{key}' must be a non-empty list")
    if len(values) > MAX_JOBS_PER_REQUEST:
        raise BadRequest(f"at most {MAX_JOBS_PER_REQUEST} entries per request")
    for value in values:
        if not isinstance(value, str) or not _ID_RE.match(value):
            raise BadRequest(f"invalid {key} entry: {value!r}")
    return list(dict.fromkeys(values))


class Service:
    def __init__(self, settings: Settings, runner: JrunRunner,
                 access: AccessControl | None = None, clock=time.time):
        self.settings = settings
        self.runner = runner
        self.access = access or AccessControl(runner, settings)
        self._clock = clock
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    # --- workspaces -----------------------------------------------------------------

    def workspace(self, caller: Caller) -> Path:
        path = self.settings.data_dir / "users" / caller.id
        jrun_dir = path / ".jrun"
        if not (jrun_dir / "settings.json").exists():
            (path / "configs").mkdir(parents=True, exist_ok=True)
            jrun_dir.mkdir(parents=True, exist_ok=True)
            (jrun_dir / "settings.json").write_text("{}\n")
            (jrun_dir / "jobs.json").write_text('{"searches": {}, "jobs": {}}\n')
        return path

    def lock(self, caller: Caller) -> threading.Lock:
        """One request at a time per caller (experiment modify replaces the
        whole config list, so a caller's submits must not interleave)."""
        with self._locks_guard:
            return self._locks.setdefault(caller.id, threading.Lock())

    # --- endpoints --------------------------------------------------------------------

    def whoami(self, token: str) -> dict:
        caller = self.access.verify(token)
        return {"id": caller.id, "alias": caller.alias,
                "identity_type": caller.identity_type,
                "project_sets": sorted(set(caller.projsets) & set(self.settings.project_sets)),
                "categories": self.settings.categories}

    def submit(self, token: str, body: dict) -> dict:
        caller = self.access.verify(token)
        only = _ids(body, "only")
        try:
            config = rewrite_config(body.get("config"), caller.alias, self.settings.categories)
        except RewriteError as e:
            raise Forbidden(str(e)) from e
        workspace = self.workspace(caller)
        with self.lock(caller):
            path = workspace / "configs" / f"submit-{int(self._clock() * 1000)}.yaml"
            path.write_text(dump_config(config))
            only_args = [f"--only={name}" for name in only]
            # Plan with the service account (dry run: placement only, read-only).
            plan = self.runner.run_json(["submit", str(path), "--dry-run", "--json",
                                         *only_args], cwd=workspace)
            self.access.check_jobs(caller, token, plan.get("jobs") or [])
            args = ["submit", str(path), "--yes", "--json", *only_args]
            if body.get("overwrite", True):
                args.append("--overwrite")
            return self.runner.run_json(args, cwd=workspace)

    def status(self, token: str, body: dict) -> dict:
        caller = self.access.verify(token)
        workspace = self.workspace(caller)
        with self.lock(caller):
            result = self.runner.run_json(["job", "status", "--json", "-n", "0"],
                                          cwd=workspace)
        return {"jobs": result.get("jobs") or []}

    def stop(self, token: str, body: dict) -> dict:
        caller = self.access.verify(token)
        job_ids = _ids(body, "job_ids")
        workspace = self.workspace(caller)
        results = {}
        with self.lock(caller):
            for job_id in job_ids:
                try:
                    out = self.runner.run_json(["job", "stop", job_id, "--json"],
                                               cwd=workspace)
                except JrunError as e:
                    results[job_id] = {"ok": False, "error": str(e)}
                    continue
                rows = out.get("results") or {}
                results[job_id] = rows.get(job_id) or next(iter(rows.values()), None) or {
                    "ok": False, "error": "not stoppable"}
        return {"results": results}

    def remove(self, token: str, body: dict) -> dict:
        caller = self.access.verify(token)
        job_ids = _ids(body, "job_ids")
        workspace = self.workspace(caller)
        removed, still_active, missing = [], {}, []
        with self.lock(caller):
            for job_id in job_ids:
                try:
                    out = self.runner.run_json(["job", "remove", job_id, "--json"],
                                               cwd=workspace)
                except JrunError as e:
                    still_active[job_id] = f"unknown ({e})"
                    continue
                removed += out.get("removed") or []
                still_active.update(out.get("still_active") or {})
                missing += out.get("missing") or []
        return {"removed": removed, "still_active": still_active, "missing": missing}
