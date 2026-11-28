"""A fake remote jrun service for tracking/submission/CLI tests."""

from jrun.errors import ApiError
from jrun.remote import RemoteRegistry

URL = "http://svc:8080"


class FakeRemote:
    def __init__(self, rows=None):
        self.rows = list(rows or [])       # what /v1/jobs/status returns
        self.submits = []
        self.stopped = []
        self.removed = []
        self.submit_result = None
        self.stop_ok = True
        self.remove_result = None
        self.error = None

    def _check(self):
        if self.error:
            raise self.error

    def jobs(self):
        self._check()
        return list(self.rows)

    def submit(self, config, *, only, overwrite=True):
        self._check()
        self.submits.append({"config": config, "only": list(only), "overwrite": overwrite})
        if self.submit_result is not None:
            return self.submit_result
        return {"experiment": {"name": "u-exp", "id": "remote-eid"}, "errors": [],
                "jobs": [{"job_id": f"r-{name}", "name": name, "status": "Submitted",
                          "params": {}, "experiment_id": "remote-eid",
                          "experiment_name": "u-exp"} for name in only]}

    def stop(self, job_ids):
        self._check()
        self.stopped += list(job_ids)
        return {"results": {j: {"ok": self.stop_ok, "status": "Running"} for j in job_ids}}

    def remove(self, job_ids):
        self._check()
        self.removed += list(job_ids)
        if self.remove_result is not None:
            return self.remove_result
        return {"removed": list(job_ids), "still_active": {}, "missing": []}


def registry(fake: FakeRemote) -> RemoteRegistry:
    return RemoteRegistry(token_provider=lambda: "caller-token",
                          client_factory=lambda url, token: fake)


def unreachable() -> ApiError:
    return ApiError(URL, message="remote service unreachable: refused")
