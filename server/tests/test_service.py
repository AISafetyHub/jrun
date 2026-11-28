import threading

import pytest

from jrun_server.access import AccessControl, Forbidden
from jrun_server.service import BadRequest, Service
from server_fakes import CALLER, CALLER_TOKEN, FakeRunner, queues

CONFIG = {
    "experiment": {"name": "exp", "image": "img", "project_set_name": "baai-safety"},
    "remote": {"url": "http://svc"},
    "search": {"name": "s", "job_template": {
        "name": "t_{lr}", "command": "echo $X",
        "resource_config": {"queue_name": "Public-Pool-H100", "queue_category": "public"},
        "run_options": {"duration_hours": 1, "usage_desc": "x", "queue_priority_mode": "high"}},
        "params": [{"name": "lr", "values": [1, 2]}]},
}
PLAN = {"jobs": [{"name": "t_1", "queue_name": "Public-Pool-H100", "queue_category": "public",
                  "project_set_name": "baai-safety", "auto": False}]}
SUBMITTED = {"jobs": [{"name": "t_1", "job_id": "jid-1", "status": "Submitted"}],
             "experiment": {"name": "intern1-exp", "id": "eid"}, "errors": []}


def submit_responses(args, cwd, token):
    return PLAN if "--dry-run" in args else SUBMITTED


@pytest.fixture
def service(settings):
    runner = FakeRunner({("whoami",): CALLER, ("queues",): queues("Public-Pool-H100"),
                         ("submit",): submit_responses})
    return Service(settings, runner, clock=lambda: 1.0)


class TestSubmit:
    def test_plans_checks_and_submits_in_caller_workspace(self, service, settings):
        result = service.submit(CALLER_TOKEN, {"config": CONFIG, "only": ["t_1"]})
        assert result == SUBMITTED
        runner = service.runner
        dry, real = runner.calls_for("submit")
        workspace = settings.data_dir / "users" / "42"
        assert dry["cwd"] == real["cwd"] == workspace
        assert (workspace / ".jrun" / "jobs.json").exists()
        # planning and submitting run as the service account
        assert dry["token"] is None and real["token"] is None
        assert dry["args"][2:] == ["--dry-run", "--json", "--only=t_1"]
        assert real["args"][2:] == ["--yes", "--json", "--only=t_1", "--overwrite"]
        # the access check ran as the caller, between plan and submit
        names = [c["args"][0] for c in runner.calls]
        assert names == ["whoami", "submit", "queues", "submit"]
        assert runner.calls_for("queues")[0]["token"] == CALLER_TOKEN

    def test_written_config_is_rewritten_and_escaped(self, service):
        service.submit(CALLER_TOKEN, {"config": CONFIG, "only": ["t_1"]})
        path = service.runner.calls_for("submit")[0]["args"][1]
        text = open(path).read()
        assert "intern1-exp" in text and "remote" not in text
        assert "echo $$X" in text
        assert "home: false" in text

    def test_no_overwrite_flag_when_disabled(self, service):
        service.submit(CALLER_TOKEN, {"config": CONFIG, "only": ["t_1"], "overwrite": False})
        assert "--overwrite" not in service.runner.calls_for("submit")[1]["args"]

    def test_forbidden_plan_never_submits(self, service):
        service.runner.responses[("queues",)] = queues("Other-Pool")
        with pytest.raises(Forbidden, match="no access"):
            service.submit(CALLER_TOKEN, {"config": CONFIG, "only": ["t_1"]})
        assert len(service.runner.calls_for("submit")) == 1  # the dry run only

    def test_rewrite_error_is_forbidden_before_any_jrun_submit(self, service):
        bad = {**CONFIG, "experiment": {**CONFIG["experiment"], "platform_overrides": {}}}
        with pytest.raises(Forbidden, match="not allowed"):
            service.submit(CALLER_TOKEN, {"config": bad, "only": ["t_1"]})
        assert service.runner.calls_for("submit") == []

    @pytest.mark.parametrize("only", [None, [], ["--status"], [1], ["a b"]])
    def test_only_is_validated(self, service, only):
        with pytest.raises(BadRequest):
            service.submit(CALLER_TOKEN, {"config": CONFIG, "only": only})


class TestLifecycle:
    def test_status_lists_caller_workspace(self, service, settings):
        service.runner.responses[("job", "status")] = {"jobs": [{"job_id": "a"}], "searches": {}}
        assert service.status(CALLER_TOKEN, {}) == {"jobs": [{"job_id": "a"}]}
        call = service.runner.calls_for("job", "status")[0]
        assert call["cwd"] == settings.data_dir / "users" / "42"
        assert call["args"] == ["job", "status", "--json", "-n", "0"]

    def test_stop_per_job(self, service):
        service.runner.responses[("job", "stop")] = (
            lambda args, cwd, token: {"results": {args[2]: {"ok": True, "status": "Running"}}})
        result = service.stop(CALLER_TOKEN, {"job_ids": ["a", "b"]})
        assert result == {"results": {"a": {"ok": True, "status": "Running"},
                                      "b": {"ok": True, "status": "Running"}}}

    def test_stop_unknown_job_reported(self, service):
        service.runner.responses[("job", "stop")] = (
            lambda args, cwd, token: {"results": {args[2]: {"ok": False, "missing": True}}})
        result = service.stop(CALLER_TOKEN, {"job_ids": ["other-users-job"]})
        assert result["results"]["other-users-job"]["missing"] is True

    def test_remove_merges_results(self, service):
        def remove(args, cwd, token):
            jid = args[2]
            if jid == "gone":
                return {"removed": [], "still_active": {}, "missing": [jid]}
            if jid == "busy":
                return {"removed": [], "still_active": {jid: "Running"}, "missing": []}
            return {"removed": [jid], "still_active": {}, "missing": []}
        service.runner.responses[("job", "remove")] = remove
        result = service.remove(CALLER_TOKEN, {"job_ids": ["ok", "gone", "busy"]})
        assert result == {"removed": ["ok"], "still_active": {"busy": "Running"},
                          "missing": ["gone"]}

    @pytest.mark.parametrize("ids", [None, [], ["-s"], ["a;rm -rf /"]])
    def test_ids_validated(self, service, ids):
        with pytest.raises(BadRequest):
            service.stop(CALLER_TOKEN, {"job_ids": ids})

    def test_callers_get_separate_workspaces(self, settings):
        other = {**CALLER, "id": "99", "alias": "intern2"}
        runner = FakeRunner({("whoami",): lambda a, c, token: CALLER if token == "t1" else other,
                             ("job", "status"): {"jobs": []}})
        service = Service(settings, runner)
        service.status("t1", {})
        service.status("t2", {})
        cwds = [c["cwd"].name for c in runner.calls_for("job", "status")]
        assert cwds == ["42", "99"]


def test_requests_of_one_caller_are_serialized(settings):
    """modify replaces the experiment's whole config list: no interleaving."""
    inside, overlap = [0], [False]
    gate = threading.Event()

    def slow_status(args, cwd, token):
        inside[0] += 1
        overlap[0] |= inside[0] > 1
        gate.wait(0.05)
        inside[0] -= 1
        return {"jobs": []}

    runner = FakeRunner({("whoami",): CALLER, ("job", "status"): slow_status})
    service = Service(settings, runner, AccessControl(runner, settings))
    threads = [threading.Thread(target=service.status, args=(CALLER_TOKEN, {}))
               for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not overlap[0]


def test_whoami_lists_served_projsets(service):
    info = service.whoami(CALLER_TOKEN)
    assert info["alias"] == "intern1"
    assert info["project_sets"] == ["baai-safety"]
    assert info["categories"] == ["public"]
