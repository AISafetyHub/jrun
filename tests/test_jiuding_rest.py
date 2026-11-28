"""Raw REST client (jrun.jiuding.rest): auth, HTTP, and endpoint shapes."""

import base64
import hashlib
import hmac
import json

import pytest

from jrun.errors import ApiError
from jrun.jiuding.rest import JiudingRest
from jrun.jiuding.rest import compute_signature, paginate, paging
from tests.http_fakes import FakeOpener, FakeResponse, make_http_error


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv("AIRS_AK", raising=False)
    monkeypatch.delenv("AIRS_SK", raising=False)
    monkeypatch.delenv("JRUN_API_BASE", raising=False)


class TestSignature:
    def test_matches_doc_algorithm(self):
        ts = "20260514T055649Z"
        sk = "sk-test"
        # Independently recompute per the doc to pin the algorithm.
        canonical = f"POST\n/api/v1/users/token/exchange\n{ts}"
        canonical_hash = hashlib.sha256(canonical.encode()).hexdigest()
        sts = f"AIRS-HMAC-SHA256\n{ts}\n{canonical_hash}"
        expected = hmac.new(sk.encode(), sts.encode(), hashlib.sha256).hexdigest()
        assert compute_signature(ts, sk) == expected


class TestExchangeToken:
    def test_constructs_signed_request(self):
        opener = FakeOpener({"token": "tok-1"})
        api = JiudingRest(opener=opener)
        result = api.exchange_token("ak-x", "sk-y")

        assert result["token"] == "tok-1"
        request = opener.requests[0]
        assert request.get_method() == "POST"
        assert request.full_url.endswith("/api/v1/users/token/exchange")
        timestamp = request.headers["X-airs-date"]
        auth = request.headers["Authorization"]
        assert auth.startswith("AIRS-HMAC-SHA256 Credential=ak-x, Signature=")
        signature = auth.split("Signature=")[1]
        assert signature == compute_signature(timestamp, "sk-y")


class TestAuthResolution:
    def test_env_vars_take_precedence(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AIRS_AK", "env-ak")
        monkeypatch.setenv("AIRS_SK", "env-sk")
        auth_file = tmp_path / "auth.json"
        auth_file.write_text(json.dumps({"ak": "file-ak", "sk": "file-sk"}))
        opener = FakeOpener({"token": "tok-env"}, {"data": {"alias": "yzh"}})
        api = JiudingRest(opener=opener, auth_file=auth_file,
                          token_file=tmp_path / "missing")

        api.get_userinfo()

        exchange_req, userinfo_req = opener.requests
        assert "Credential=env-ak" in exchange_req.headers["Authorization"]
        assert userinfo_req.headers["Airs-token"] == "tok-env"

    def test_auth_file_used_when_no_env(self, tmp_path):
        auth_file = tmp_path / "auth.json"
        auth_file.write_text(json.dumps({"ak": "file-ak", "sk": "file-sk"}))
        opener = FakeOpener({"token": "tok-file"}, {"data": {}})
        api = JiudingRest(opener=opener, auth_file=auth_file,
                          token_file=tmp_path / "missing")

        api.get_userinfo()

        assert "Credential=file-ak" in opener.requests[0].headers["Authorization"]

    def test_airsctl_token_file_fallback(self, tmp_path):
        token_file = tmp_path / ".token"
        token_file.write_text("cached-token\n")
        opener = FakeOpener({"data": {"alias": "yzh"}})
        api = JiudingRest(opener=opener, auth_file=tmp_path / "missing",
                          accesskey_dir=tmp_path / "no-etc", token_file=token_file)

        api.get_userinfo()

        # No token exchange: the cached token is used directly.
        assert len(opener.requests) == 1
        assert opener.requests[0].headers["Airs-token"] == "cached-token"

    def test_etc_accesskey_base64_decoded(self, tmp_path):
        accesskey_dir = tmp_path / "accesskey"
        accesskey_dir.mkdir()
        (accesskey_dir / "user-ak").write_text(
            base64.b64encode(b"ak-from-etc").decode() + "\n")
        (accesskey_dir / "user-sk").write_text(
            base64.b64encode(b"sk-from-etc").decode())
        opener = FakeOpener({"token": "t"}, {"data": {}})
        api = JiudingRest(opener=opener, auth_file=tmp_path / "missing",
                          accesskey_dir=accesskey_dir,
                          token_file=tmp_path / "missing2")

        api.get_userinfo()

        auth = opener.requests[0].headers["Authorization"]
        assert "Credential=ak-from-etc" in auth

    def test_etc_accesskey_beats_token_file(self, tmp_path):
        accesskey_dir = tmp_path / "accesskey"
        accesskey_dir.mkdir()
        (accesskey_dir / "user-ak").write_text(base64.b64encode(b"ak-etc").decode())
        (accesskey_dir / "user-sk").write_text(base64.b64encode(b"sk-etc").decode())
        token_file = tmp_path / ".token"
        token_file.write_text("cached-token")
        opener = FakeOpener({"token": "fresh-tok"}, {"data": {}})
        api = JiudingRest(opener=opener, auth_file=tmp_path / "missing",
                          accesskey_dir=accesskey_dir, token_file=token_file)

        api.get_userinfo()

        # Exchange happened (etc/accesskey wins), stale cached token ignored.
        assert len(opener.requests) == 2
        assert opener.requests[1].headers["Airs-token"] == "fresh-tok"

    def test_malformed_accesskey_falls_through(self, tmp_path):
        accesskey_dir = tmp_path / "accesskey"
        accesskey_dir.mkdir()
        (accesskey_dir / "user-ak").write_text("not base64!!!")
        (accesskey_dir / "user-sk").write_text("also not")
        token_file = tmp_path / ".token"
        token_file.write_text("cached-token")
        opener = FakeOpener({"data": {}})
        api = JiudingRest(opener=opener, auth_file=tmp_path / "missing",
                          accesskey_dir=accesskey_dir, token_file=token_file)

        api.get_userinfo()

        assert opener.requests[0].headers["Airs-token"] == "cached-token"

    def test_no_credentials_raises(self, tmp_path):
        api = JiudingRest(opener=FakeOpener(), auth_file=tmp_path / "missing",
                          accesskey_dir=tmp_path / "no-etc",
                          token_file=tmp_path / "missing2")
        with pytest.raises(ApiError, match="jrun login"):
            api.get_userinfo()

    def test_explicit_ak_sk_take_precedence(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AIRS_AK", "env-ak")
        monkeypatch.setenv("AIRS_SK", "env-sk")
        opener = FakeOpener({"token": "tok-explicit"}, {"data": {}})
        api = JiudingRest(opener=opener, ak="cli-ak", sk="cli-sk",
                          token_file=tmp_path / "missing")

        api.get_userinfo()

        assert "Credential=cli-ak" in opener.requests[0].headers["Authorization"]

    def test_token_cached_per_instance(self, monkeypatch):
        monkeypatch.setenv("AIRS_AK", "ak")
        monkeypatch.setenv("AIRS_SK", "sk")
        opener = FakeOpener({"token": "tok"}, {"data": {}}, {"data": {}})
        api = JiudingRest(opener=opener)

        api.get_userinfo()
        api.get_userinfo()

        # One exchange + two userinfo calls.
        assert len(opener.requests) == 3

    def test_explicit_token_used_as_is(self, monkeypatch):
        monkeypatch.setenv("AIRS_AK", "ak")
        monkeypatch.setenv("AIRS_SK", "sk")
        opener = FakeOpener({"data": {}})
        api = JiudingRest(opener=opener, token="caller-tok")

        api.get_userinfo()

        # No exchange: the given token wins over AK/SK.
        assert len(opener.requests) == 1
        assert opener.requests[0].headers["Airs-token"] == "caller-tok"

    def test_airs_token_env_beats_ak_sk(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AIRS_TOKEN", "env-tok\n")
        monkeypatch.setenv("AIRS_AK", "ak")
        monkeypatch.setenv("AIRS_SK", "sk")
        api = JiudingRest(opener=FakeOpener(), auth_file=tmp_path / "none.json")

        assert api.token() == "env-tok"

    def test_exchanged_token_renewed_before_expiry(self, monkeypatch):
        monkeypatch.setenv("AIRS_AK", "ak")
        monkeypatch.setenv("AIRS_SK", "sk")
        now = [1000.0]
        opener = FakeOpener({"token": "t1", "expiresIn": "7200"},
                            {"token": "t2", "expiresIn": "7200"})
        api = JiudingRest(opener=opener, clock=lambda: now[0])

        assert api.token() == "t1"
        now[0] += 7200 - 301  # still inside the lifetime minus the margin
        assert api.token() == "t1"
        now[0] += 2  # within 5 minutes of expiry: re-exchange
        assert api.token() == "t2"
        assert len(opener.requests) == 2

    def test_missing_expires_in_defaults_to_two_hours(self, monkeypatch):
        monkeypatch.setenv("AIRS_AK", "ak")
        monkeypatch.setenv("AIRS_SK", "sk")
        now = [0.0]
        opener = FakeOpener({"token": "t1"}, {"token": "t2"})
        api = JiudingRest(opener=opener, clock=lambda: now[0])

        api.token()
        now[0] = 7200 - 299
        assert api.token() == "t2"

    def test_as_is_token_never_expires(self, tmp_path):
        token_file = tmp_path / ".token"
        token_file.write_text("file-tok")
        now = [0.0]
        api = JiudingRest(opener=FakeOpener(), token_file=token_file,
                          auth_file=tmp_path / "none.json",
                          accesskey_dir=tmp_path / "none", clock=lambda: now[0])

        assert api.token() == "file-tok"
        now[0] = 10 ** 9
        assert api.token() == "file-tok"


class TestProxyBypass:
    def test_default_opener_ignores_env_proxies(self, monkeypatch):
        monkeypatch.setenv("http_proxy", "http://proxy:8080")
        api = JiudingRest()
        assert api._proxy_handler.proxies == {}


class TestHttpErrors:
    def test_http_error_raises_api_error_with_message(self):
        error = make_http_error(401, {"code": "990002", "message": "invalid access key"})
        api = JiudingRest(opener=FakeOpener(error), ak="ak", sk="sk")
        with pytest.raises(ApiError) as exc_info:
            api.get_userinfo()
        assert exc_info.value.status_code == 401
        assert "invalid access key" in str(exc_info.value)

    def test_invalid_json_raises_api_error(self):
        class BadResponse(FakeResponse):
            def read(self):
                return b"not json"

        opener = FakeOpener(BadResponse(None))
        api = JiudingRest(opener=opener, ak="ak", sk="sk")
        with pytest.raises(ApiError, match="invalid JSON"):
            api.get_userinfo()




class TestEndpoints:
    @pytest.mark.parametrize("method, path", [
        ("projsets_select_joined", "/api/v1/projsets/select-joined"),
        ("queue_select", "/api/v1/queue/select"),
        ("job_select", "/api/v1/job/select"),
        ("shared_queue_list", "/api/v1/shared-queue/list"),
        ("shared_queue_job_count", "/api/v1/shared-queue/job-count"),
        ("shared_queue_config", "/api/v1/shared-queue/config"),
        ("experiment_create", "/api/v1/experiment"),
        ("experiment_delete", "/api/v1/experiment/delete"),
        ("job_snapshots_select", "/api/v1/job-snapshots/select"),
    ])
    def test_posts_body_with_context_headers(self, method, path):
        opener = FakeOpener({"token": "t"}, {"ok": True})
        client = JiudingRest(opener=opener, ak="ak", sk="sk")
        if method == "projsets_select_joined":
            result = getattr(client, method)({"x": 1})
        else:
            result = getattr(client, method)({"x": 1}, proj_id="p", projset_id="ps",
                                              cluster_id="c", zone_id="z")
        assert result == {"ok": True}  # raw response, unchanged
        request = opener.requests[1]
        assert request.get_method() == "POST"
        assert request.full_url.endswith(path)
        assert json.loads(request.data.decode()) == {"x": 1}
        assert request.headers["Airs-token"] == "t"
        if method != "projsets_select_joined":
            assert request.headers["Airs-proj-id"] == "p"
            assert request.headers["Airs-projset-id"] == "ps"
            assert request.headers["Airs-cluster-id"] == "c"
            assert request.headers["Airs-zone-id"] == "z"

    def test_unset_context_headers_are_omitted(self):
        opener = FakeOpener({"token": "t"}, {})
        JiudingRest(opener=opener, ak="ak", sk="sk").queue_select({})
        headers = opener.requests[1].headers
        assert not any(k.startswith("Airs-proj") for k in headers)

    def test_projsets_select_joined_default_paging(self):
        opener = FakeOpener({"token": "t"}, {})
        JiudingRest(opener=opener, ak="ak", sk="sk").projsets_select_joined()
        assert json.loads(opener.requests[1].data.decode()) == {"paging": paging()}


class TestPaging:
    def test_paging_shape(self):
        assert paging(2, 50) == {"order": "", "page": 2, "pageSize": 50, "sort": "", "total": 0}

    def test_paginate_stops_on_short_page(self):
        pages = {1: [1, 2], 2: [3, 4], 3: [5]}
        calls = []

        def fetch(page):
            calls.append(page)
            return pages[page]

        assert paginate(fetch, page_size=2) == [1, 2, 3, 4, 5]
        assert calls == [1, 2, 3]

    def test_paginate_max_pages(self):
        assert paginate(lambda page: [page], page_size=1, max_pages=3) == [1, 2, 3]
