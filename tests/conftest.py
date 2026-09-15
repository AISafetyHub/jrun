import json

import pytest
from click.testing import CliRunner


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def project_dir(tmp_path):
    jrun_dir = tmp_path / ".jrun"
    jrun_dir.mkdir()
    settings = {"experiment_name": "test-exp", "experiment_id": "eid-123"}
    (jrun_dir / "settings.json").write_text(json.dumps(settings))
    (jrun_dir / "jobs.json").write_text(json.dumps({"searches": {}, "jobs": {}}))
    return tmp_path


SAMPLE_QUEUE = {
    "id": "qid-1",
    "name": "jiuding_airs1_h100",
    "quotaId": "quota-1",
    "projId": "p1",
    "projsetId": "ps1",
    "quotaDetailList": [
        {"resourceDetail": {"acceleratorModel": "J1_ADV_H1-SXM4-80GB"}}
    ],
    "usedQuota": {
        "computing": {
            "J1_ADV_H1-SXM4-80GB": {"total": {"high": 8}, "used": {"high": 3}},
            "cpu": {"total": {"high": 160}, "used": {}},
        }
    },
    "zoneId": "zid-1",
    "clusterId": "cid-1",
    "quotaInfoList": [
        {"clusterDisplayName": "北京上庄",
         "zoneQuotaInfoList": [{"zoneDisplayName": "可用区A"}]}
    ],
}

SPEC_CONFIG = """
experiment:
  name: spec-exp
  image: harbor.x/pytorch:23.08
  queue_name: jiuding_airs1_h100
job:
  name: spec-job
  command: echo hello
"""
