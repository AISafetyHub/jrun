"""HTTP round trips against a real ThreadingHTTPServer on port 0."""

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from jrun_server.access import AuthError, Forbidden
from jrun_server.app import TOKEN_HEADER, make_handler, startup_checks
from jrun_server.runner import JrunError
from jrun_server.service import BadRequest
from server_fakes import FakeRunner


class StubService:
    def __init__(self):
        self.error = None
        self.calls = []

    def _call(self, name, token, body=None):
        self.calls.append((name, token, body))
        if self.error:
            raise self.error
        return {"called": name}

    def whoami(self, token):
        return self._call("whoami", token)

    def submit(self, token, body):
        return self._call("submit", token, body)

    def status(self, token, body):
        return self._call("status", token, body)

    def stop(self, token, body):
        return self._call("stop", token, body)

    def remove(self, token, body):
        return self._call("remove", token, body)


@pytest.fixture
def server():
    service = StubService()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(service))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield service, f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def post(url, body=b"{}", token="tok"):
    request = urllib.request.Request(url, data=body, method="POST")
    if token:
        request.add_header(TOKEN_HEADER, token)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


@pytest.mark.parametrize("path, name", [
    ("/v1/whoami", "whoami"), ("/v1/submit", "submit"), ("/v1/jobs/status", "status"),
    ("/v1/jobs/stop", "stop"), ("/v1/jobs/remove", "remove"),
])
def test_routes_pass_token_and_body(server, path, name):
    service, base = server
    code, data = post(base + path, b'{"job_ids": ["a"]}')
    assert (code, data) == (200, {"called": name})
    assert service.calls[0][1] == "tok"


@pytest.mark.parametrize("error, code", [
    (BadRequest("bad"), 400), (AuthError("who"), 401), (Forbidden("no"), 403),
    (JrunError(["x"], "jrun broke"), 500), (RuntimeError("boom"), 500),
])
def test_errors_map_to_status_codes(server, error, code):
    service, base = server
    service.error = error
    status, data = post(base + "/v1/jobs/status")
    assert status == code
    assert data["error"]


def test_unknown_path_and_bad_json(server):
    _, base = server
    assert post(base + "/v1/nope")[0] == 404
    assert post(base + "/v1/submit", b"not json")[0] == 400
    assert post(base + "/v1/submit", b"[1]")[0] == 400


def test_healthz(server):
    _, base = server
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(base + "/healthz", timeout=5) as response:
        assert json.loads(response.read()) == {"ok": True}


class TestStartupChecks:
    def test_ok(self, settings):
        runner = FakeRunner({("whoami",): {"alias": "staff", "projsets": [{"name": "baai-safety"}]}})
        assert startup_checks(settings, runner, which=lambda b: "/bin/" + b)["alias"] == "staff"

    @pytest.mark.parametrize("missing", ["jrun", "airsctl"])
    def test_missing_binaries(self, settings, missing):
        with pytest.raises(SystemExit, match=missing):
            startup_checks(settings, FakeRunner(),
                           which=lambda b: None if b == missing else "/bin/" + b)

    def test_bad_service_key(self, settings):
        runner = FakeRunner({("whoami",): JrunError(["whoami"], "401")})
        with pytest.raises(SystemExit, match="does not work"):
            startup_checks(settings, runner, which=lambda b: "/bin/" + b)

    def test_service_account_not_in_projset(self, settings):
        runner = FakeRunner({("whoami",): {"alias": "staff", "projsets": []}})
        with pytest.raises(SystemExit, match="not a member of baai-safety"):
            startup_checks(settings, runner, which=lambda b: "/bin/" + b)
