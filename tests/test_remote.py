"""Remote-service client (jrun.remote)."""

import json

import pytest

from jrun.errors import ApiError
from jrun.remote import (PROTOCOL_HEADER, TOKEN_HEADER, RemoteClient, RemoteRegistry)
from tests.http_fakes import FakeOpener, make_http_error


def client(*responses):
    opener = FakeOpener(*responses)
    return RemoteClient("http://svc:8080/", lambda: "caller-token", opener=opener), opener


class TestRemoteClient:
    def test_sends_token_protocol_and_json(self):
        c, opener = client({"jobs": []})
        c.jobs()
        request = opener.requests[0]
        assert request.full_url == "http://svc:8080/v1/jobs/status"
        assert request.get_method() == "POST"
        assert request.headers[TOKEN_HEADER.capitalize()] == "caller-token"
        assert request.headers[PROTOCOL_HEADER.capitalize()] == "1"

    def test_submit_body(self):
        c, opener = client({"jobs": [{"job_id": "x"}]})
        result = c.submit({"experiment": {"name": "e"}}, only=["a", "b"])
        body = json.loads(opener.requests[0].data)
        assert body == {"config": {"experiment": {"name": "e"}}, "only": ["a", "b"],
                        "overwrite": True}
        assert result["jobs"][0]["job_id"] == "x"
        assert opener.requests[0].full_url.endswith("/v1/submit")

    @pytest.mark.parametrize("method, path", [("stop", "/v1/jobs/stop"),
                                              ("remove", "/v1/jobs/remove")])
    def test_job_id_endpoints(self, method, path):
        c, opener = client({})
        getattr(c, method)(["j1", "j2"])
        assert opener.requests[0].full_url.endswith(path)
        assert json.loads(opener.requests[0].data) == {"job_ids": ["j1", "j2"]}

    def test_whoami(self):
        c, opener = client({"alias": "intern1"})
        assert c.whoami()["alias"] == "intern1"
        assert opener.requests[0].full_url.endswith("/v1/whoami")

    def test_http_error_uses_service_message(self):
        c, _ = client(make_http_error(403, {"error": "no access to queue X"}))
        with pytest.raises(ApiError, match="HTTP 403.*no access to queue X"):
            c.jobs()

    def test_unreachable(self):
        import urllib.error
        c, _ = client(urllib.error.URLError("refused"))
        with pytest.raises(ApiError, match="unreachable"):
            c.jobs()

    def test_non_object_response(self):
        c, _ = client([1, 2])
        with pytest.raises(ApiError, match="non-object"):
            c.jobs()


class TestRemoteRegistry:
    def test_one_client_per_url_and_lazy_token(self):
        tokens = []

        def provider():
            tokens.append(1)
            return "tok"
        made = []
        registry = RemoteRegistry(token_provider=provider,
                                  client_factory=lambda url, tp: made.append((url, tp)) or url)
        assert registry.get("http://a/") == "http://a"
        assert registry.get("http://a") == "http://a"
        assert len(made) == 1
        assert tokens == []  # not resolved until a request is made
        assert made[0][1]() == "tok" and made[0][1]() == "tok"
        assert tokens == [1]  # resolved once, then reused

    def test_default_token_comes_from_local_credentials(self, monkeypatch):
        monkeypatch.setenv("AIRS_TOKEN", "local-tok")
        registry = RemoteRegistry(client_factory=lambda url, tp: tp)
        assert registry.get("http://a")() == "local-tok"
