import base64
import hashlib
import hmac
import io
import json
import urllib.error
import urllib.request

import pytest

from jrun.api import (
    PlatformAPI,
    compute_signature,
    filter_queues,
    queue_location_info,
    save_auth,
    with_project_context,
)
from jrun.errors import ApiError


# Sample row adapted from the queue/select API doc.
SAMPLE_QUEUE = {
    "id": "ef1017d7-59ad-4f00-9cac-a21bdf677973",
    "name": "jiuding_airs1_h100",
    "quotaId": "8273b556-2773-4ba3-8bef-a1bcdfdcd0c3",
    "projId": "b121aae3-2198-4c91-9e72-355aa5a0e050",
    "projsetId": "a8b8f665-03ed-4d6a-9b2e-2cb236a59d83",
    "quotaDetailList": [
        {"resourceDetail": {"acceleratorModel": "J1_ADV_H1-SXM4-80GB", "acceleratorCount": 8}}
    ],
    "usedQuota": {
        "computing": {
            "J1_ADV_H1-SXM4-80GB": {"total": {"high": 8}, "used": {}},
            "cpu": {"total": {"high": 160}, "used": {}},
            "memory": {"total": {"high": 1800}, "used": {}},
        }
    },
    "zoneId": "0922cfc7-8710-4c6d-9660-c9dd448ad1ac",
    "clusterId": "cb363735-bf90-47c3-9d7f-49f4965cbb27",
    "quotaInfoList": [
        {
            "clusterName": "sz2-calc1",
            "clusterDisplayName": "北京上庄",
            "zoneQuotaInfoList": [
                {"zoneId": "0922cfc7-8710-4c6d-9660-c9dd448ad1ac",
                 "zoneName": "sz2-calc1-zonea", "zoneDisplayName": "可用区A"}
            ],
        }
    ],
}


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return json.dumps(self.payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakeOpener:
    """Returns queued payloads (or raises queued exceptions) per open() call."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def open(self, request, timeout=None):
        self.requests.append(request)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        if isinstance(response, FakeResponse):
            return response
        return FakeResponse(response)


def make_http_error(code, payload):
    return urllib.error.HTTPError(
        url="https://x", code=code, msg="err", hdrs=None,
        fp=io.BytesIO(json.dumps(payload).encode()),
    )


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
        api = PlatformAPI(opener=opener)
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
        api = PlatformAPI(opener=opener, auth_file=auth_file,
                          token_file=tmp_path / "missing")

        api.get_userinfo()

        exchange_req, userinfo_req = opener.requests
        assert "Credential=env-ak" in exchange_req.headers["Authorization"]
        assert userinfo_req.headers["Airs-token"] == "tok-env"

    def test_auth_file_used_when_no_env(self, tmp_path):
        auth_file = tmp_path / "auth.json"
        auth_file.write_text(json.dumps({"ak": "file-ak", "sk": "file-sk"}))
        opener = FakeOpener({"token": "tok-file"}, {"data": {}})
        api = PlatformAPI(opener=opener, auth_file=auth_file,
                          token_file=tmp_path / "missing")

        api.get_userinfo()

        assert "Credential=file-ak" in opener.requests[0].headers["Authorization"]

    def test_airsctl_token_file_fallback(self, tmp_path):
        token_file = tmp_path / ".token"
        token_file.write_text("cached-token\n")
        opener = FakeOpener({"data": {"alias": "yzh"}})
        api = PlatformAPI(opener=opener, auth_file=tmp_path / "missing",
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
        api = PlatformAPI(opener=opener, auth_file=tmp_path / "missing",
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
        api = PlatformAPI(opener=opener, auth_file=tmp_path / "missing",
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
        api = PlatformAPI(opener=opener, auth_file=tmp_path / "missing",
                          accesskey_dir=accesskey_dir, token_file=token_file)

        api.get_userinfo()

        assert opener.requests[0].headers["Airs-token"] == "cached-token"

    def test_no_credentials_raises(self, tmp_path):
        api = PlatformAPI(opener=FakeOpener(), auth_file=tmp_path / "missing",
                          accesskey_dir=tmp_path / "no-etc",
                          token_file=tmp_path / "missing2")
        with pytest.raises(ApiError, match="jrun login"):
            api.get_userinfo()

    def test_explicit_ak_sk_take_precedence(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AIRS_AK", "env-ak")
        monkeypatch.setenv("AIRS_SK", "env-sk")
        opener = FakeOpener({"token": "tok-explicit"}, {"data": {}})
        api = PlatformAPI(opener=opener, ak="cli-ak", sk="cli-sk",
                          token_file=tmp_path / "missing")

        api.get_userinfo()

        assert "Credential=cli-ak" in opener.requests[0].headers["Authorization"]

    def test_token_cached_per_instance(self, monkeypatch):
        monkeypatch.setenv("AIRS_AK", "ak")
        monkeypatch.setenv("AIRS_SK", "sk")
        opener = FakeOpener({"token": "tok"}, {"data": {}}, {"data": {}})
        api = PlatformAPI(opener=opener)

        api.get_userinfo()
        api.get_userinfo()

        # One exchange + two userinfo calls.
        assert len(opener.requests) == 3


class TestProxyBypass:
    def test_default_opener_ignores_env_proxies(self, monkeypatch):
        monkeypatch.setenv("http_proxy", "http://proxy:8080")
        api = PlatformAPI()
        assert api._proxy_handler.proxies == {}


class TestHttpErrors:
    def test_http_error_raises_api_error_with_message(self):
        error = make_http_error(401, {"code": "990002", "message": "invalid access key"})
        api = PlatformAPI(opener=FakeOpener(error), ak="ak", sk="sk")
        with pytest.raises(ApiError) as exc_info:
            api.get_userinfo()
        assert exc_info.value.status_code == 401
        assert "invalid access key" in str(exc_info.value)

    def test_invalid_json_raises_api_error(self):
        class BadResponse(FakeResponse):
            def read(self):
                return b"not json"

        opener = FakeOpener(BadResponse(None))
        api = PlatformAPI(opener=opener, ak="ak", sk="sk")
        with pytest.raises(ApiError, match="invalid JSON"):
            api.get_userinfo()


class TestQueues:
    def test_list_queues(self):
        opener = FakeOpener({"token": "t"},
                            {"queueSummaryInfos": [SAMPLE_QUEUE]})
        api = PlatformAPI(opener=opener, ak="ak", sk="sk")
        queues = api.list_queues(proj_id="p1", projset_id="ps1")

        assert queues == [SAMPLE_QUEUE]
        request = opener.requests[1]
        body = json.loads(request.data.decode())
        assert body["projId"] == "p1"
        assert body["projsetId"] == "ps1"
        assert "queueId" not in body
        assert request.headers["Airs-proj-id"] == "p1"
        assert request.headers["Airs-projset-id"] == "ps1"

    def test_list_queues_without_projset_queries_joined_projsets(self):
        # queue/select 403s without projset context; the fallback queries each
        # joined projset and merges the results.
        projsets = {"items": [
            {"projsetInfo": {"id": "ps-1", "name": "safety"}},
            {"projsetInfo": {"id": "ps-2", "name": "other"}},
        ]}
        opener = FakeOpener({"token": "t"}, projsets,
                            {"queueSummaryInfos": [SAMPLE_QUEUE]},
                            {"queueSummaryInfos": [{"name": "q2"}]})
        api = PlatformAPI(opener=opener, ak="ak", sk="sk")
        queues = api.list_queues()

        assert queues == [SAMPLE_QUEUE, {"name": "q2"}]
        projset_req = opener.requests[1]
        assert projset_req.full_url.endswith("/api/v1/projsets/select-joined")
        for req, psid in zip(opener.requests[2:], ("ps-1", "ps-2")):
            body = json.loads(req.data.decode())
            assert body["projsetId"] == psid
            assert req.headers["Airs-projset-id"] == psid

    def test_list_queues_joined_projsets_cached(self):
        projsets = {"items": [{"projsetInfo": {"id": "ps-1"}}]}
        opener = FakeOpener({"token": "t"}, projsets,
                            {"queueSummaryInfos": []}, {"queueSummaryInfos": []})
        api = PlatformAPI(opener=opener, ak="ak", sk="sk")
        api.list_queues()
        api.list_queues()
        projset_reqs = [r for r in opener.requests
                        if r.full_url.endswith("/projsets/select-joined")]
        assert len(projset_reqs) == 1

    def test_list_queues_no_joined_projset_raises(self):
        opener = FakeOpener({"token": "t"}, {"items": []})
        api = PlatformAPI(opener=opener, ak="ak", sk="sk")
        with pytest.raises(ApiError, match="no joined projset"):
            api.list_queues()

    def test_find_queue(self):
        projsets = {"items": [{"projsetInfo": {"id": "ps-1"}}]}
        opener = FakeOpener({"token": "t"}, projsets,
                            {"queueSummaryInfos": [SAMPLE_QUEUE]})
        api = PlatformAPI(opener=opener, ak="ak", sk="sk")
        assert api.find_queue("jiuding_airs1_h100") == SAMPLE_QUEUE

        opener2 = FakeOpener({"token": "t"},
                             {"queueSummaryInfos": [SAMPLE_QUEUE]})
        api2 = PlatformAPI(opener=opener2, ak="ak", sk="sk")
        assert api2.find_queue("nope", projset_id="ps1") is None

    def test_find_queue_resolves_names_and_rejects_ambiguous_location(self):
        rows = [
            {**SAMPLE_QUEUE, "name": "same", "projName": "project",
             "projsetName": "set", "clusterDisplayName": "region-a",
             "zoneDisplayName": "zone-a", "quotaInfoList": []},
            {**SAMPLE_QUEUE, "name": "same", "projName": "project",
             "projsetName": "set", "clusterDisplayName": "region-b",
             "zoneDisplayName": "zone-b", "zoneId": "zone-b", "quotaInfoList": []},
        ]
        opener = FakeOpener({"token": "t"},
                            {"items": [{"projsetInfo": {"id": "ps", "name": "set"}}]},
                            {"queueSummaryInfos": rows},
                            {"queueSummaryInfos": rows})
        api = PlatformAPI(opener=opener, ak="ak", sk="sk")
        with pytest.raises(ValueError, match="multiple locations"):
            api.find_queue("same", project_set_name="set", project_name="project")

        opener = FakeOpener({"token": "t"},
                            {"items": [{"projsetInfo": {"id": "ps", "name": "set"}}]},
                            {"queueSummaryInfos": rows},
                            {"queueSummaryInfos": rows})
        api = PlatformAPI(opener=opener, ak="ak", sk="sk")
        result = api.find_queue(
            "same", project_set_name="set", project_name="project",
            region_name="region-b", zone_name="zone-b",
        )
        assert result["zoneId"] == "zone-b"


class TestQueueFreeResources:
    def test_empty_used_means_all_free(self):
        rows = PlatformAPI.queue_free_resources(SAMPLE_QUEUE)
        assert rows == [{"model": "J1_ADV_H1-SXM4-80GB", "total": 8, "used": 0, "free": 8}]

    def test_used_summed_across_priorities(self):
        queue = {
            "usedQuota": {
                "computing": {
                    "A100": {
                        "total": {"high": 8, "medium": 8},
                        "used": {"high": 3, "low": 1},
                    },
                }
            }
        }
        rows = PlatformAPI.queue_free_resources(queue)
        assert rows == [{"model": "A100", "total": 16, "used": 4, "free": 12}]

    def test_no_quota_returns_empty(self):
        assert PlatformAPI.queue_free_resources({}) == []

    def test_shared_queue_resources(self):
        queue = {
            "acceleratorModel": "H20",
            "quota": {"acceleratorCount": 8},
            "used": {"acceleratorCount": 3},
        }
        assert PlatformAPI.queue_free_resources(queue) == [
            {"model": "H20", "total": 8, "used": 3, "free": 5}
        ]


class TestSharedQueues:
    def test_list_shared_queues_and_counts(self):
        shared = {
            "paging": {"total": 1},
            "sharedQueues": [{
                "queueId": "shared-q", "name": "Public-Pool-H20",
                "acceleratorModel": "H20", "quotaId": "quota",
                "sharedQuotaId": "shared-quota",
            }],
        }
        counts = {"paging": {"total": 1}, "items": [{
            "queue": {"queueId": "shared-q"}, "runningCount": 2,
        }]}
        opener = FakeOpener({"token": "t"}, shared, counts)
        api = PlatformAPI(opener=opener, ak="ak", sk="sk")
        rows = api.list_shared_queues(proj_id="p", projset_id="ps", queue_category="public")
        assert rows[0]["queueCategory"] == "QUEUE_CATEGORY_PUBLIC"
        job_counts = api.list_shared_queue_job_counts(
            proj_id="p", projset_id="ps", queue_category="public"
        )
        assert job_counts[0]["runningCount"] == 2

    def test_shared_location_and_limits(self):
        queue = {
            "queueId": "shared-q", "name": "Public-Pool-H20",
            "quotaId": "quota", "sharedQuotaId": "shared-quota",
            "acceleratorModel": "H20", "clusterId": "cluster",
            "zoneId": "zone", "clusterDisplayName": "cluster-name",
            "zoneDisplayName": "zone-name", "queueCategory": "QUEUE_CATEGORY_IDLE",
        }
        location = queue_location_info(queue)
        assert location["queue_id"] == "shared-q"
        assert location["shared_quota_id"] == "shared-quota"
        assert location["queue_category"] == "QUEUE_CATEGORY_IDLE"


class TestQueueLocationInfo:
    def test_extracts_fields(self):
        location = queue_location_info(SAMPLE_QUEUE)
        assert location["queue_id"] == "ef1017d7-59ad-4f00-9cac-a21bdf677973"
        assert location["queue_name"] == "jiuding_airs1_h100"
        assert location["quota_id"] == "8273b556-2773-4ba3-8bef-a1bcdfdcd0c3"
        assert location["cluster_id"] == "cb363735-bf90-47c3-9d7f-49f4965cbb27"
        assert location["zone_id"] == "0922cfc7-8710-4c6d-9660-c9dd448ad1ac"
        assert location["region_name"] == "北京上庄"
        assert location["zone_name"] == "可用区A"
        assert location["accelerator_model"] == "J1_ADV_H1-SXM4-80GB"
        assert location["proj_id"] == "b121aae3-2198-4c91-9e72-355aa5a0e050"


class TestCreateExperiment:
    USERINFO = {"data": {"id": "319832320808853520", "realName": "Test User"}}

    def test_body_and_response(self):
        opener = FakeOpener({"token": "t"}, self.USERINFO, {"experimentId": "eid-new"})
        api = PlatformAPI(opener=opener, ak="ak", sk="sk")
        exp_id = api.create_experiment(
            name="my-exp", queue=SAMPLE_QUEUE,
            image="harbor.x/pytorch:23.08", description="desc",
        )

        assert exp_id == "eid-new"
        request = opener.requests[2]
        assert request.full_url.endswith("/api/v1/experiment")
        body = json.loads(request.data.decode())
        assert body["name"] == "my-exp"
        assert body["description"] == "desc"
        assert body["projId"] == SAMPLE_QUEUE["projId"]
        assert body["projsetId"] == SAMPLE_QUEUE["projsetId"]
        # creatorId is a required int64 proto field, resolved from userinfo
        assert body["creatorId"] == "319832320808853520"
        assert body["creator"] == "Test User"
        assert len(body["storageInfo"]) == 3

        configs = body["advanceConfigInfos"]
        assert len(configs) == 1
        resource = configs[0]["resourceConfigList"][0]
        assert resource["queueId"] == SAMPLE_QUEUE["id"]
        assert resource["queueName"] == SAMPLE_QUEUE["name"]
        assert resource["quotaId"] == SAMPLE_QUEUE["quotaId"]
        assert resource["basicImage"] == "harbor.x/pytorch:23.08"
        assert resource["imageRegion"] == "PUBLIC"
        assert resource["clusterId"] == SAMPLE_QUEUE["clusterId"]
        assert resource["zoneId"] == SAMPLE_QUEUE["zoneId"]
        assert resource["clusterDisplayName"] == "北京上庄"
        detail = resource["roleInfoList"][0]["resourceRequestDetail"]
        assert detail["acceleratorModel"] == "J1_ADV_H1-SXM4-80GB"

        assert request.headers["Airs-cluster-id"] == SAMPLE_QUEUE["clusterId"]
        assert request.headers["Airs-zone-id"] == SAMPLE_QUEUE["zoneId"]

    def test_explicit_proj_ids_override_queue(self):
        opener = FakeOpener({"token": "t"}, self.USERINFO, {"experimentId": "eid"})
        api = PlatformAPI(opener=opener, ak="ak", sk="sk")
        api.create_experiment(name="e", queue=SAMPLE_QUEUE, image="img",
                              proj_id="p-x", projset_id="ps-x")
        body = json.loads(opener.requests[2].data.decode())
        assert body["projId"] == "p-x"
        assert body["projsetId"] == "ps-x"

    def test_nested_data_response(self):
        opener = FakeOpener({"token": "t"}, self.USERINFO,
                            {"data": {"experimentId": "eid-nested"}})
        api = PlatformAPI(opener=opener, ak="ak", sk="sk")
        assert api.create_experiment(name="e", queue=SAMPLE_QUEUE, image="i") == "eid-nested"

    def test_missing_id_raises(self):
        opener = FakeOpener({"token": "t"}, self.USERINFO, {"unexpected": True})
        api = PlatformAPI(opener=opener, ak="ak", sk="sk")
        with pytest.raises(ApiError, match="no experimentId"):
            api.create_experiment(name="e", queue=SAMPLE_QUEUE, image="i")

    def test_missing_user_id_raises(self):
        opener = FakeOpener({"token": "t"}, {"data": {"realName": "No Id"}})
        api = PlatformAPI(opener=opener, ak="ak", sk="sk")
        with pytest.raises(ApiError, match="no user id"):
            api.create_experiment(name="e", queue=SAMPLE_QUEUE, image="i")

    def test_image_region_override(self):
        opener = FakeOpener({"token": "t"}, self.USERINFO, {"experimentId": "eid"})
        api = PlatformAPI(opener=opener, ak="ak", sk="sk")
        api.create_experiment(name="e", queue=SAMPLE_QUEUE, image="img",
                              image_region="PRIVATE")
        body = json.loads(opener.requests[2].data.decode())
        resource = body["advanceConfigInfos"][0]["resourceConfigList"][0]
        assert resource["imageRegion"] == "PRIVATE"


class TestJobSnapshots:
    def test_filters_in_body(self):
        items = [{"jobId": "j1", "stateName": "Running", "acceleratorUtil": 0.5}]
        opener = FakeOpener({"token": "t"}, {"items": items})
        api = PlatformAPI(opener=opener, ak="ak", sk="sk")
        result = api.job_snapshots(proj_id="p1", projset_id="ps1", cluster_ids=["c1"],
                                   zone_ids=["z1"], owner="me")

        assert result == items
        body = json.loads(opener.requests[1].data.decode())
        assert body["clusterIds"] == ["c1"]
        assert body["zoneIds"] == ["z1"]
        assert body["ownerName"] == "me"
        assert body["projId"] == "p1"
        assert body["projsetId"] == "ps1"

    def test_defaults_to_first_joined_projset(self):
        # job-snapshots/select 403s without projset context; fall back to the
        # account's first joined projset.
        projsets = {"items": [{"projsetInfo": {"id": "ps-auto"}}]}
        opener = FakeOpener({"token": "t"}, projsets, {"items": [{"jobId": "j1"}]})
        api = PlatformAPI(opener=opener, ak="ak", sk="sk")

        assert api.job_snapshots() == [{"jobId": "j1"}]
        body = json.loads(opener.requests[2].data.decode())
        assert body["projsetId"] == "ps-auto"
        assert opener.requests[2].headers["Airs-projset-id"] == "ps-auto"

    def test_job_snapshots_all_paginates(self):
        page1 = [{"jobId": f"j{i}"} for i in range(3)]
        page2 = [{"jobId": "j-last"}]
        opener = FakeOpener({"token": "t"}, {"items": page1}, {"items": page2})
        api = PlatformAPI(opener=opener, ak="ak", sk="sk")

        result = api.job_snapshots_all(projset_id="ps1", page_size=3)

        assert [i["jobId"] for i in result] == ["j0", "j1", "j2", "j-last"]
        assert len(opener.requests) == 3  # one token exchange + two pages
        page2_body = json.loads(opener.requests[2].data.decode())
        assert page2_body["paging"]["page"] == 2

    def test_job_snapshots_all_stops_on_short_page(self):
        opener = FakeOpener({"token": "t"}, {"items": [{"jobId": "j1"}]})
        api = PlatformAPI(opener=opener, ak="ak", sk="sk")

        assert len(api.job_snapshots_all(projset_id="ps1", page_size=100)) == 1
        assert len(opener.requests) == 2  # no second page requested


class TestSaveAuth:
    def test_writes_file_with_owner_only_permissions(self, tmp_path):
        path = save_auth("ak", "sk", path=tmp_path / "auth.json")
        assert json.loads(path.read_text()) == {"ak": "ak", "sk": "sk"}
        assert (path.stat().st_mode & 0o777) == 0o600

    def test_creates_parent_dirs(self, tmp_path):
        path = save_auth("ak", "sk", path=tmp_path / "sub" / "dir" / "auth.json")
        assert path.exists()


class TestExperimentJobs:
    def test_body_shape(self):
        items = [{"id": "j1", "configName": "c1", "status": "Running"}]
        opener = FakeOpener({"token": "t"}, {"jobInfos": items})
        api = PlatformAPI(opener=opener, ak="ak", sk="sk")

        assert api.experiment_jobs("eid-1") == items
        request = opener.requests[1]
        assert request.full_url.endswith("/api/v1/job/select")
        body = json.loads(request.data.decode())
        assert body["experimentId"] == "eid-1"
        assert body["experimentType"] == 1
        assert body["archiveType"] == "ACTIVE_TYPE"
        assert body["paging"] == {"page": 1, "pageSize": 100, "sort": "", "order": ""}
        assert request.headers["Airs-token"] == "t"

    def test_proj_headers_passed(self):
        opener = FakeOpener({"token": "t"}, {"jobInfos": []})
        api = PlatformAPI(opener=opener, ak="ak", sk="sk")
        api.experiment_jobs("eid-1", proj_id="p1", projset_id="ps1")
        request = opener.requests[1]
        assert request.headers["Airs-proj-id"] == "p1"
        assert request.headers["Airs-projset-id"] == "ps1"

    def test_missing_jobinfos_returns_empty(self):
        opener = FakeOpener({"token": "t"}, {"unexpected": True})
        api = PlatformAPI(opener=opener, ak="ak", sk="sk")
        assert api.experiment_jobs("eid-1") == []

    def test_all_paginates_until_short_page(self):
        page1 = [{"id": f"j{i}"} for i in range(2)]
        page2 = [{"id": "j-last"}]
        opener = FakeOpener({"token": "t"}, {"jobInfos": page1}, {"jobInfos": page2})
        api = PlatformAPI(opener=opener, ak="ak", sk="sk")

        result = api.experiment_jobs_all("eid-1", page_size=2, projset_id="ps1")

        assert [j["id"] for j in result] == ["j0", "j1", "j-last"]
        assert len(opener.requests) == 3  # one token exchange + two pages
        page2_body = json.loads(opener.requests[2].data.decode())
        assert page2_body["paging"]["page"] == 2

    def test_all_stops_on_short_first_page(self):
        opener = FakeOpener({"token": "t"}, {"jobInfos": [{"id": "j1"}]})
        api = PlatformAPI(opener=opener, ak="ak", sk="sk")
        assert len(api.experiment_jobs_all("eid-1", page_size=100, projset_id="ps1")) == 1
        assert len(opener.requests) == 2  # no second page requested


class TestDeleteExperiment:
    USERINFO = {"data": {"id": "319832320808853520"}}

    def test_body_shape(self):
        opener = FakeOpener({"token": "t"}, self.USERINFO, {"code": "ok"})
        api = PlatformAPI(opener=opener, ak="ak", sk="sk")

        api.delete_experiment("eid-1", "my-exp", projset_id="ps1")

        request = opener.requests[2]
        assert request.full_url.endswith("/api/v1/experiment/delete")
        body = json.loads(request.data.decode())
        assert body["experimentItems"] == [
            {"experimentId": "eid-1", "experimentName": "my-exp",
             "creatorId": "319832320808853520", "archive": 1}
        ]

    def test_proj_headers_passed(self):
        opener = FakeOpener({"token": "t"}, self.USERINFO, {"code": "ok"})
        api = PlatformAPI(opener=opener, ak="ak", sk="sk")
        api.delete_experiment("eid-1", "my-exp", proj_id="p1", projset_id="ps1")
        request = opener.requests[2]
        assert request.headers["Airs-proj-id"] == "p1"
        assert request.headers["Airs-projset-id"] == "ps1"

    def test_missing_user_id_raises(self):
        opener = FakeOpener({"token": "t"}, {"data": {"realName": "No Id"}})
        api = PlatformAPI(opener=opener, ak="ak", sk="sk")
        with pytest.raises(ApiError, match="no user id"):
            api.delete_experiment("eid-1", "my-exp")

    def test_default_projset_fallback(self):
        projsets = {"items": [{"projsetInfo": {"id": "ps-auto"}}]}
        opener = FakeOpener({"token": "t"}, self.USERINFO, projsets, {"code": "ok"})
        api = PlatformAPI(opener=opener, ak="ak", sk="sk")

        api.delete_experiment("eid-1", "my-exp")

        request = opener.requests[3]
        assert request.full_url.endswith("/api/v1/experiment/delete")
        assert request.headers["Airs-projset-id"] == "ps-auto"


class TestExperimentJobsProjsetFallback:
    PROJSETS = {"items": [
        {"projsetInfo": {"id": "ps-1"}},
        {"projsetInfo": {"id": "ps-2"}},
    ]}

    def test_probes_joined_projsets_until_one_succeeds(self):
        jobs = [{"id": "j1", "status": "Running"}]
        opener = FakeOpener(
            {"token": "t"},
            self.PROJSETS,
            make_http_error(403, {"message": "permission denied"}),  # ps-1 probe
            {"jobInfos": [{"id": "probe"}]},                          # ps-2 probe
            {"jobInfos": jobs},                                       # page 1
        )
        api = PlatformAPI(opener=opener, ak="ak", sk="sk")

        result = api.experiment_jobs_all("eid-1")

        assert result == jobs
        probe2 = json.loads(opener.requests[3].data.decode())
        assert probe2["paging"]["pageSize"] == 1  # probe is a minimal page
        # the real page-1 query reuses the winning projset
        page1_request = opener.requests[4]
        assert page1_request.headers["Airs-projset-id"] == "ps-2"
        assert json.loads(page1_request.data.decode())["paging"]["page"] == 1

    def test_all_projsets_403_raises_last_error(self):
        opener = FakeOpener(
            {"token": "t"},
            self.PROJSETS,
            make_http_error(403, {"message": "denied-1"}),
            make_http_error(403, {"message": "denied-2"}),
        )
        api = PlatformAPI(opener=opener, ak="ak", sk="sk")

        with pytest.raises(ApiError, match="denied-2"):
            api.experiment_jobs_all("eid-1")

    def test_non_403_probe_error_raises_immediately(self):
        opener = FakeOpener(
            {"token": "t"},
            self.PROJSETS,
            make_http_error(500, {"message": "boom"}),
        )
        api = PlatformAPI(opener=opener, ak="ak", sk="sk")

        with pytest.raises(ApiError, match="boom"):
            api.experiment_jobs_all("eid-1")
        assert len(opener.requests) == 3  # no further projsets probed

    def test_explicit_projset_skips_probing(self):
        opener = FakeOpener({"token": "t"}, {"jobInfos": [{"id": "j1"}]})
        api = PlatformAPI(opener=opener, ak="ak", sk="sk")

        assert api.experiment_jobs_all("eid-1", projset_id="ps-x") == [{"id": "j1"}]
        assert len(opener.requests) == 2  # token + single page, no select-joined

    def test_no_joined_projset_raises(self):
        opener = FakeOpener({"token": "t"}, {"items": []})
        api = PlatformAPI(opener=opener, ak="ak", sk="sk")

        with pytest.raises(ApiError, match="no joined projset"):
            api.experiment_jobs_all("eid-1")


def _authed(*responses):
    opener = FakeOpener(*responses)
    api = PlatformAPI(opener=opener)
    api._token = "tok"
    return api, opener


def _body(request):
    return json.loads(request.data.decode())


PROJSETS = {"items": [{"projsetInfo": {"id": "ps-1", "name": "baai-safety"}},
                      {"projsetInfo": {"id": "ps-2", "name": "other"}}]}


class TestSharedQueueEndpoints:
    def test_list_shared_queues_unwraps_and_tags_category(self):
        api, opener = _authed({"sharedQueues": [
            {"queue": {"id": "q1", "name": "Public-Pool-A"}},
            {"id": "q2", "name": "Public-Pool-B"},
        ]})
        rows = api.list_shared_queues(projset_id="ps-1", queue_category="idle")
        assert [r["name"] for r in rows] == ["Public-Pool-A", "Public-Pool-B"]
        assert all(r["queueCategory"] == "QUEUE_CATEGORY_IDLE" for r in rows)
        body = _body(opener.requests[0])
        assert body["queueCategory"] == "QUEUE_CATEGORY_IDLE"
        assert body["projsetId"] == "ps-1"

    def test_job_counts_default_projset_and_body(self):
        api, opener = _authed(PROJSETS, {"items": [{"runningCount": 2}]})
        rows = api.list_shared_queue_job_counts(queue_category="public")
        assert rows == [{"runningCount": 2}]
        body = _body(opener.requests[1])
        assert body["projsetId"] == "ps-1"
        assert body["queueCategory"] == "QUEUE_CATEGORY_PUBLIC"
        assert body["onlyQueueing"] is False

    def test_shared_queue_limits_parses_ints(self):
        api, opener = _authed(PROJSETS, {"items": [
            {"configKey": "max_jobs_per_projset", "configValue": "4"},
            {"configKey": "note", "configValue": "text"},
            {"configValue": "ignored"},
        ]})
        assert api.shared_queue_limits() == {"max_jobs_per_projset": 4, "note": "text"}
        assert _body(opener.requests[1])["projsetId"] == "ps-1"

    def test_find_queue_shared_uses_shared_list(self):
        api, opener = _authed({"sharedQueues": [{"id": "q1", "name": "Public-Pool-A"}]})
        queue = api.find_queue("Public-Pool-A", queue_category="public")
        assert queue["id"] == "q1"
        assert opener.requests[0].full_url.endswith("/api/v1/shared-queue/list")


class TestResolveProjectContext:
    def test_no_names(self):
        api, opener = _authed()
        assert api.resolve_project_context() == (None, None)
        assert opener.requests == []

    def test_project_set_name_only(self):
        api, _ = _authed(PROJSETS)
        assert api.resolve_project_context("baai-safety") == (None, "ps-1")

    def test_unknown_project_set_lists_available(self):
        api, _ = _authed(PROJSETS)
        with pytest.raises(ValueError, match="available: baai-safety, other"):
            api.resolve_project_context("nope")

    def test_project_name_resolved_from_queue_rows(self):
        api, _ = _authed(PROJSETS, {"queueSummaryInfos": [
            {"name": "q", "projName": "research", "projsetName": "baai-safety",
             "projId": "p-9", "projsetId": "ps-1"},
        ]})
        assert api.resolve_project_context("baai-safety", "research") == ("p-9", "ps-1")

    def test_unknown_project_name_raises(self):
        api, _ = _authed(PROJSETS, {"queueSummaryInfos": [
            {"name": "q", "projName": "research", "projId": "p", "projsetId": "ps-1"}]})
        with pytest.raises(ValueError, match="project_name 'nope' not found"):
            api.resolve_project_context("baai-safety", "nope")


class TestPlacementHelpers:
    def test_list_category_queues_dispatch(self):
        api, opener = _authed({"sharedQueues": [{"id": "q1", "name": "S"}]},
                              {"queueSummaryInfos": [{"id": "d1", "name": "D"}]})
        assert [q["name"] for q in api.list_category_queues("idle", projset_id="ps")] == ["S"]
        assert [q["name"] for q in api.list_category_queues(None, projset_id="ps")] == ["D"]
        assert opener.requests[0].full_url.endswith("/api/v1/shared-queue/list")
        assert opener.requests[1].full_url.endswith("/queue/select")

    def test_shared_queue_budgets(self):
        api, opener = _authed(
            {"items": [{"configKey": "max_jobs_per_projset", "configValue": "4"},
                       {"configKey": "max_idle_jobs_per_projset", "configValue": "5"}]},
            {"items": [{"runningCount": 2, "queueingCount": 1}, {"runningCount": 0}]},
            {"items": [{"runningCount": 7}]},
        )
        assert api.shared_queue_budgets(projset_id="ps") == {
            "QUEUE_CATEGORY_PUBLIC": 1, "QUEUE_CATEGORY_IDLE": 0}
        assert _body(opener.requests[1])["queueCategory"] == "QUEUE_CATEGORY_PUBLIC"
        assert _body(opener.requests[2])["queueCategory"] == "QUEUE_CATEGORY_IDLE"

    def test_shared_queue_budgets_without_limits_are_unlimited(self):
        api, opener = _authed({"items": []})
        assert api.shared_queue_budgets(projset_id="ps") == {
            "QUEUE_CATEGORY_PUBLIC": None, "QUEUE_CATEGORY_IDLE": None}
        assert len(opener.requests) == 1  # no job-count calls needed

    def test_filter_queues(self):
        rows = [
            {"name": "a", "projsetName": "ps", "projName": "p", "clusterDisplayName": "r1",
             "zoneDisplayName": "z1"},
            {"name": "b", "projsetName": "other", "projName": "p", "clusterDisplayName": "r1"},
            {"name": "c", "projsetName": "ps", "projName": "p", "clusterDisplayName": "r2"},
        ]
        assert [q["name"] for q in filter_queues(rows, project_set_name="ps")] == ["a", "c"]
        assert [q["name"] for q in filter_queues(rows, region_name="r1", zone_name="z1")] == ["a"]
        # Shared pools carry no project names: project filters do not drop them.
        shared = [{"name": "s", "clusterDisplayName": "r1"}]
        assert filter_queues(shared, project_set_name="ps", project_name="p") == shared

    def test_with_project_context_fills_only_missing(self):
        queue = with_project_context({"projId": "own"}, "p1", "ps1", "proj", "set")
        assert queue == {"projId": "own", "projsetId": "ps1", "projName": "proj",
                         "projsetName": "set"}
