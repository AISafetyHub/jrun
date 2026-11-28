import pytest

from jrun_server.access import AccessControl, AuthError, Forbidden
from jrun_server.runner import JrunError
from server_fakes import CALLER, CALLER_TOKEN, FakeRunner, queues


def access(settings, responses=None, clock=None):
    runner = FakeRunner({("whoami",): CALLER, **(responses or {})})
    kwargs = {"clock": clock} if clock else {}
    return AccessControl(runner, settings, **kwargs), runner


def job(**kw):
    row = {"name": "j", "queue_name": "Public-Pool-H100", "queue_category": "public",
           "project_set_name": "baai-safety", "auto": False, "placement": None}
    row.update(kw)
    return row


class TestVerify:
    def test_runs_whoami_as_caller(self, settings):
        ac, runner = access(settings)
        caller = ac.verify(CALLER_TOKEN)
        assert (caller.id, caller.alias) == ("42", "intern1")
        assert caller.projsets == {"baai-safety": "ps-1", "other-ps": "ps-2"}
        assert runner.calls[0]["token"] == CALLER_TOKEN

    def test_missing_token(self, settings):
        with pytest.raises(AuthError, match="missing"):
            access(settings)[0].verify("")

    def test_rejected_token(self, settings):
        ac, _ = access(settings, {("whoami",): JrunError(["whoami"], "HTTP 401")})
        with pytest.raises(AuthError, match="rejected"):
            ac.verify("bad")

    def test_disabled_account(self, settings):
        ac, _ = access(settings, {("whoami",): {**CALLER, "active": False}})
        with pytest.raises(Forbidden, match="disabled"):
            ac.verify(CALLER_TOKEN)

    def test_user_allowlist(self, settings):
        settings.users = ["someone-else"]
        with pytest.raises(Forbidden, match="not allowed"):
            access(settings)[0].verify(CALLER_TOKEN)

    def test_cached_until_ttl(self, settings):
        now = [0.0]
        ac, runner = access(settings, clock=lambda: now[0])
        ac.verify(CALLER_TOKEN)
        ac.verify(CALLER_TOKEN)
        assert len(runner.calls) == 1
        now[0] = settings.identity_ttl + 1
        ac.verify(CALLER_TOKEN)
        assert len(runner.calls) == 2


class TestCheckJobs:
    def check(self, settings, rows, visible=("Public-Pool-H100",), service=None):
        responses = {("queues",): queues(*visible)}
        ac, runner = access(settings, responses)
        if service is not None:
            # service-account listing (no token) vs caller listing (token)
            runner.responses[("queues",)] = (
                lambda args, cwd, token: queues(*visible) if token else service)
        caller = ac.verify(CALLER_TOKEN)
        ac.check_jobs(caller, CALLER_TOKEN, rows)
        return runner

    def test_visible_public_pool_allowed_via_idle_view(self, settings):
        runner = self.check(settings, [job()])
        call = runner.calls_for("queues")[0]
        assert call["args"] == ["queues", "--category", "idle", "--projset-id", "ps-1", "--json"]
        assert call["token"] == CALLER_TOKEN

    def test_one_listing_per_projset(self, settings):
        runner = self.check(settings, [job(name="a"), job(name="b")])
        assert len(runner.calls_for("queues")) == 1

    def test_placed_queue_is_checked(self, settings):
        row = job(placement={"queue_name": "Public-Pool-H100", "queue_category": "public"},
                  queue_name="whatever")
        self.check(settings, [row])

    @pytest.mark.parametrize("row, message", [
        (job(queue_category="idle"), "not submitted by this service"),
        (job(project_set_name=None), "project_set_name is required"),
        (job(project_set_name="not-served"), "not served here"),
        (job(queue_name="Hidden-Pool"), "no access to queue"),
    ])
    def test_rejections(self, settings, row, message):
        with pytest.raises(Forbidden, match=message):
            self.check(settings, [row])

    def test_projset_caller_has_not_joined(self, settings):
        settings.project_sets = ["baai-safety", "foreign"]
        with pytest.raises(Forbidden, match="not a member"):
            self.check(settings, [job(project_set_name="foreign")])

    def test_empty_plan(self, settings):
        with pytest.raises(Forbidden, match="nothing"):
            self.check(settings, [])

    def test_auto_job_needs_every_candidate_pool_visible(self, settings):
        service = queues("Public-Pool-H100", "Public-Pool-H100-B")
        row = job(auto=True, accelerator_choices=[["NVIDIA_H100", 8]])
        with pytest.raises(Forbidden, match="Public-Pool-H100-B"):
            self.check(settings, [row], visible=("Public-Pool-H100",), service=service)
        self.check(settings, [row], visible=("Public-Pool-H100", "Public-Pool-H100-B"),
                   service=service)

    def test_auto_job_ignores_pools_of_other_models(self, settings):
        service = {"queues": [
            {"queue_name": "Public-Pool-H100", "resources": [{"model": "NVIDIA_H100"}]},
            {"queue_name": "Public-Pool-A100", "resources": [{"model": "NVIDIA_A100"}]}]}
        row = job(auto=True, accelerator_choices=[["nvidia_h100", 8]])
        self.check(settings, [row], visible=("Public-Pool-H100",), service=service)

    def test_caller_listing_failure_is_forbidden(self, settings):
        ac, runner = access(settings, {("queues",): JrunError(["queues"], "HTTP 403")})
        caller = ac.verify(CALLER_TOKEN)
        with pytest.raises(Forbidden, match="cannot list"):
            ac.check_jobs(caller, CALLER_TOKEN, [job()])
