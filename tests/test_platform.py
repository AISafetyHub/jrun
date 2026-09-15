import json
import pytest
from unittest.mock import patch, MagicMock

from jrun.errors import PlatformError
from jrun.models import ExperimentSpec, Job, ResourceConfig
from jrun.platform import PlatformClient


class TestParseJobStatus:
    def test_json_status(self):
        output = json.dumps({"status": "running"})
        assert PlatformClient._parse_job_status(output) == "Running"

    def test_json_status_capitalize(self):
        output = json.dumps({"status": "FAILED"})
        assert PlatformClient._parse_job_status(output) == "Failed"

    def test_text_status_running(self):
        output = "Job abc-123 is currently running\n"
        assert PlatformClient._parse_job_status(output) == "Running"

    def test_text_status_succeed(self):
        output = "Status: Succeed\nDuration: 1h"
        assert PlatformClient._parse_job_status(output) == "Succeed"

    def test_text_status_failed(self):
        output = "The job has failed due to OOM"
        assert PlatformClient._parse_job_status(output) == "Failed"

    def test_text_status_cancelled(self):
        output = "Job was cancelled by user"
        assert PlatformClient._parse_job_status(output) == "Cancelled"

    def test_no_status_found(self):
        assert PlatformClient._parse_job_status("no info here") is None

    def test_empty_string(self):
        assert PlatformClient._parse_job_status("") is None

    def test_invalid_json(self):
        assert PlatformClient._parse_job_status("{broken json") is None


class TestPlatformClientCommands:
    @patch("subprocess.run")
    def test_experiment_list_with_id(self, mock_run):
        mock_run.return_value = MagicMock(stdout='{"data": []}', returncode=0)
        client = PlatformClient()
        client.experiment_list(exp_id="eid-1")
        mock_run.assert_called_once_with(
            ["airsctl", "experiment", "list", "-e", "eid-1"],
            capture_output=True, text=True,
        )

    @patch("subprocess.run")
    def test_experiment_list_with_name(self, mock_run):
        mock_run.return_value = MagicMock(stdout="", returncode=0)
        client = PlatformClient()
        client.experiment_list(exp_name="my-exp")
        mock_run.assert_called_once_with(
            ["airsctl", "experiment", "list", "-N", "my-exp"],
            capture_output=True, text=True,
        )

    @patch("subprocess.run")
    def test_job_stop(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        client = PlatformClient()
        client.job_stop("job-id-123")
        mock_run.assert_called_once_with(
            ["airsctl", "job", "stop", "-j", "job-id-123"],
            capture_output=True, text=True,
        )

    @patch("subprocess.run")
    def test_job_run_with_exp_and_config(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        client = PlatformClient()
        client.job_run(exp_id="eid", config_name="cfg")
        mock_run.assert_called_once_with(
            ["airsctl", "job", "run", "-e", "eid", "-n", "cfg"],
            capture_output=True, text=True,
        )

    @patch("subprocess.run")
    def test_job_run_with_exp_name_only(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        client = PlatformClient()
        client.job_run(exp_name="my-exp", config_name="cfg")
        mock_run.assert_called_once_with(
            ["airsctl", "job", "run", "-N", "my-exp", "-n", "cfg"],
            capture_output=True, text=True,
        )

    @patch("subprocess.run")
    def test_job_run_prefers_exp_id_over_name(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        client = PlatformClient()
        client.job_run(exp_name="stale-exp", exp_id="eid", config_name="cfg")
        mock_run.assert_called_once_with(
            ["airsctl", "job", "run", "-e", "eid", "-n", "cfg"],
            capture_output=True, text=True,
        )

    @patch("subprocess.run")
    def test_get_job_status_success(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0, stdout=json.dumps({"status": "running"})
        )
        client = PlatformClient()
        assert client.get_job_status("j1") == "Running"

    @patch("subprocess.run")
    def test_get_job_status_failure(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="")
        client = PlatformClient()
        assert client.get_job_status("j1") is None

    @patch("subprocess.run")
    def test_get_job_info_json(self, mock_run):
        payload = {"status": "running", "cluster_name": "dx-calc1"}
        mock_run.return_value = MagicMock(returncode=0, stdout=json.dumps(payload))
        info = PlatformClient().get_job_info("j1")
        assert info["status"] == "Running"
        assert info["cluster_name"] == "dx-calc1"

    @patch("subprocess.run")
    def test_get_job_info_text_fallback(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="Job j1 is running")
        info = PlatformClient().get_job_info("j1")
        assert info == {"status": "Running"}

    @patch("subprocess.run")
    def test_get_job_info_invalid_output(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="no job data")
        info = PlatformClient().get_job_info("j1")
        assert info is None

    @patch.object(PlatformClient, "job_run")
    @patch.object(PlatformClient, "experiment_modify")
    @patch.object(PlatformClient, "experiment_list")
    def test_submit_uses_experiment_id_for_run(
        self, mock_list, mock_modify, mock_job_run
    ):
        experiment = {
            "advance_config_infos": [
                {
                    "config_name": "base",
                    "command": "echo old",
                    "conf_id": "conf-id",
                    "resource_config_list": [],
                }
            ]
        }
        job_id = "11111111-1111-1111-1111-111111111111"
        mock_list.return_value = MagicMock(
            returncode=0, stdout=json.dumps(experiment), stderr=""
        )
        mock_job_run.return_value = MagicMock(
            returncode=0, stdout=f"Job {job_id} submitted", stderr=""
        )

        result = PlatformClient().submit_job(
            Job(name="target", command="echo new"),
            exp_name="stale-exp",
            exp_id="eid",
        )

        assert result == job_id
        mock_modify.assert_called_once()
        mock_job_run.assert_called_once_with(
            exp_id="eid", config_name="target"
        )


class TestRetryOnUnauthenticated:
    @patch("time.sleep")
    @patch("subprocess.run")
    def test_retry_succeeds_after_unauthenticated(self, mock_run, mock_sleep):
        fail_result = MagicMock(returncode=1, stderr="Unauthenticated(990002)", stdout="")
        success_result = MagicMock(returncode=0, stderr="", stdout='{"status": "running"}')
        mock_run.side_effect = [fail_result, success_result]

        client = PlatformClient()
        result = client._run(["job", "list"], check=True)

        assert result.returncode == 0
        assert mock_run.call_count == 2
        mock_sleep.assert_called_once_with(35)

    @patch("time.sleep")
    @patch("subprocess.run")
    def test_retry_fails_raises_error(self, mock_run, mock_sleep):
        fail_result = MagicMock(returncode=1, stderr="Unauthenticated(990002)", stdout="")
        mock_run.side_effect = [fail_result, fail_result]

        client = PlatformClient()
        with pytest.raises(PlatformError):
            client._run(["job", "list"], check=True)

        assert mock_run.call_count == 2
        mock_sleep.assert_called_once_with(35)

    @patch("time.sleep")
    @patch("subprocess.run")
    def test_no_retry_on_other_errors(self, mock_run, mock_sleep):
        fail_result = MagicMock(returncode=1, stderr="some other error", stdout="")
        mock_run.return_value = fail_result

        client = PlatformClient()
        with pytest.raises(PlatformError):
            client._run(["job", "list"], check=True)

        assert mock_run.call_count == 1
        mock_sleep.assert_not_called()

    @patch("time.sleep")
    @patch("subprocess.run")
    def test_no_retry_when_check_false_but_still_retries_unauthenticated(self, mock_run, mock_sleep):
        fail_result = MagicMock(returncode=1, stderr="Unauthenticated(990002)", stdout="")
        success_result = MagicMock(returncode=0, stderr="", stdout="ok")
        mock_run.side_effect = [fail_result, success_result]

        client = PlatformClient()
        result = client._run(["job", "list"], check=False)

        assert mock_run.call_count == 2
        mock_sleep.assert_called_once_with(35)


class TestCountExperimentConfigs:
    @patch("subprocess.run")
    def test_returns_config_count(self, mock_run):
        exp_data = {"advance_config_infos": [{"config_name": f"cfg_{i}"} for i in range(5)]}
        mock_run.return_value = MagicMock(returncode=0, stdout=json.dumps(exp_data), stderr="")
        client = PlatformClient()
        assert client.count_experiment_configs("exp-123") == 5

    @patch("subprocess.run")
    def test_returns_zero_for_empty(self, mock_run):
        exp_data = {"advance_config_infos": []}
        mock_run.return_value = MagicMock(returncode=0, stdout=json.dumps(exp_data), stderr="")
        client = PlatformClient()
        assert client.count_experiment_configs("exp-123") == 0

    @patch("subprocess.run")
    def test_returns_none_on_failure(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="error")
        client = PlatformClient()
        assert client.count_experiment_configs("exp-123") is None

    @patch("subprocess.run")
    def test_returns_none_on_invalid_json(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="not json", stderr="")
        client = PlatformClient()
        assert client.count_experiment_configs("exp-123") is None


class TestSubmitJobSpecOverrides:
    def _make_experiment(self):
        return {
            "advance_config_infos": [
                {
                    "config_name": "base",
                    "command": "echo old",
                    "resource_config_list": [
                        {
                            "priority": "medium",
                            "basic_image": "old-image:1",
                            "queue_id": "old-q",
                            "queue_name": "old-queue",
                            "role_info_list": [
                                {"name": "Master", "replicas": 1,
                                 "resource_request_detail": {}}
                            ],
                        }
                    ],
                }
            ]
        }

    def _submit(self, mock_list, mock_modify, mock_job_run, job, spec, queue_location):
        mock_list.return_value = MagicMock(
            returncode=0, stdout=json.dumps(self._make_experiment()), stderr=""
        )
        captured = {}

        def fake_modify(path):
            captured.update(json.loads(open(path).read()))
            return MagicMock(returncode=0, stdout="", stderr="")

        mock_modify.side_effect = fake_modify
        mock_job_run.return_value = MagicMock(
            returncode=0, stdout="Job 11111111-1111-1111-1111-111111111111 ok", stderr=""
        )
        PlatformClient().submit_job(job, exp_name=None, exp_id="eid",
                                    spec=spec, queue_location=queue_location)
        return captured["advance_config_infos"][-1]["resource_config_list"][0]

    @patch.object(PlatformClient, "job_run")
    @patch.object(PlatformClient, "experiment_modify")
    @patch.object(PlatformClient, "experiment_list")
    def test_spec_overrides_image_and_queue(self, mock_list, mock_modify, mock_job_run):
        from jrun.models import ExperimentSpec
        spec = ExperimentSpec(name="e", image="new-image:2", queue_name="q1")
        location = {"queue_id": "qid-1", "queue_name": "q1", "quota_id": "quota-1",
                    "cluster_id": "cid", "zone_id": "zid",
                    "cluster_display_name": "北京", "zone_display_name": "A"}
        res = self._submit(mock_list, mock_modify, mock_job_run,
                           Job(name="j", command="echo hi"), spec, location)
        assert res["basic_image"] == "new-image:2"
        # airsctl modify JSON needs the RepoTypes enum as an integer
        assert res["image_region"] == 1
        assert res["queue_id"] == "qid-1"
        assert res["queue_name"] == "q1"
        assert res["quota_id"] == "quota-1"
        assert res["cluster_id"] == "cid"
        assert res["zone_id"] == "zid"
        assert res["cluster_display_name"] == "北京"

    @patch.object(PlatformClient, "job_run")
    @patch.object(PlatformClient, "experiment_modify")
    @patch.object(PlatformClient, "experiment_list")
    def test_spec_applies_without_resource_config(self, mock_list, mock_modify, mock_job_run):
        """Spec overrides apply even when the job has no resource_config."""
        from jrun.models import ExperimentSpec
        spec = ExperimentSpec(name="e", image="new-image:2", queue_name="q1")
        res = self._submit(mock_list, mock_modify, mock_job_run,
                           Job(name="j", command="echo hi"), spec, {"queue_id": "qid-1"})
        assert res["basic_image"] == "new-image:2"
        assert res["queue_id"] == "qid-1"
        # Untouched fields keep template values
        assert res["priority"] == "medium"

    @patch.object(PlatformClient, "job_run")
    @patch.object(PlatformClient, "experiment_modify")
    @patch.object(PlatformClient, "experiment_list")
    def test_explicit_cluster_zone_win(self, mock_list, mock_modify, mock_job_run):
        from jrun.models import ExperimentSpec
        spec = ExperimentSpec(name="e", image="img", queue_name="q1",
                              cluster_id="explicit-c", zone_id="explicit-z")
        location = {"queue_id": "qid-1", "cluster_id": "lookup-c", "zone_id": "lookup-z"}
        res = self._submit(mock_list, mock_modify, mock_job_run,
                           Job(name="j", command="echo hi"), spec, location)
        assert res["cluster_id"] == "explicit-c"
        assert res["zone_id"] == "explicit-z"

    @patch.object(PlatformClient, "job_run")
    @patch.object(PlatformClient, "experiment_modify")
    @patch.object(PlatformClient, "experiment_list")
    def test_spec_image_region_private_maps_to_int(self, mock_list, mock_modify, mock_job_run):
        from jrun.models import ExperimentSpec
        spec = ExperimentSpec(name="e", image="img", queue_name="q1",
                              image_region="PRIVATE")
        res = self._submit(mock_list, mock_modify, mock_job_run,
                           Job(name="j", command="echo hi"), spec, {"queue_id": "qid-1"})
        assert res["image_region"] == 2

    @patch.object(PlatformClient, "job_run")
    @patch.object(PlatformClient, "experiment_modify")
    @patch.object(PlatformClient, "experiment_list")
    def test_no_spec_keeps_template(self, mock_list, mock_modify, mock_job_run):
        res = self._submit(mock_list, mock_modify, mock_job_run,
                           Job(name="j", command="echo hi"), None, None)
        assert res["basic_image"] == "old-image:1"
        assert res["queue_id"] == "old-q"


class TestModifyConfigs:
    def _experiment(self):
        return {
            "experiment_id": "eid",
            "experiment_name": "exp",
            "description": "keep me",
            "advance_config_infos": [
                {
                    "config_name": "history-1",
                    "conf_id": "old-conf-1",
                    "command": "old command",
                    "resource_config_list": [{
                        "priority": "medium",
                        "basic_image": "old-image",
                        "queue_name": "old-queue",
                        "role_info_list": [{
                            "name": "Master",
                            "replicas": 1,
                            "resource_request_detail": {},
                        }, {
                            "name": "Worker",
                            "replicas": 9,
                            "resource_request_detail": {},
                        }],
                    }],
                },
                {"config_name": "history-2", "conf_id": "old-conf-2"},
            ],
        }

    @patch.object(PlatformClient, "experiment_modify")
    @patch.object(PlatformClient, "experiment_list")
    def test_replaces_entire_config_list_and_reads_once(self, mock_list, mock_modify):
        experiment = self._experiment()
        mock_list.return_value = MagicMock(
            returncode=0, stdout=json.dumps(experiment), stderr=""
        )
        captured = {}

        def capture(path):
            captured.update(json.loads(open(path).read()))
            return MagicMock(returncode=0, stdout="", stderr="")

        mock_modify.side_effect = capture
        jobs = [
            Job(
                name="current-a", command="echo a", envs={"A": 1},
                resource_config=ResourceConfig(
                    priority="high", accelerator_count=2,
                    cpu_cores=8, mem_gib=32,
                ),
            ),
            Job(name="current-b", command="echo b"),
        ]

        result = PlatformClient().modify_configs(jobs, "eid")

        assert result == ["current-a", "current-b"]
        assert mock_list.call_count == 1
        assert mock_modify.call_count == 1
        assert captured["experiment_name"] == "exp"
        assert captured["description"] == "keep me"
        configs = captured["advance_config_infos"]
        assert [c["config_name"] for c in configs] == ["current-a", "current-b"]
        assert all("conf_id" not in c for c in configs)
        assert configs[0]["command"] == "echo a"
        assert configs[0]["hyper_parameter"] == {"A": "1"}
        resource = configs[0]["resource_config_list"][0]
        assert resource["priority"] == "high"
        assert resource["role_info_list"][0]["resource_request_detail"]["accelerator_count"] == 2
        assert all(r["name"] != "Worker" for r in resource["role_info_list"])

    @patch.object(PlatformClient, "experiment_modify")
    def test_uses_supplied_snapshot_without_list(self, mock_modify):
        captured = {}

        def capture(path):
            captured.update(json.loads(open(path).read()))
            return MagicMock(returncode=0)

        mock_modify.side_effect = capture
        snapshot = self._experiment()
        client = PlatformClient()
        client.modify_configs(
            [Job(name="only", command="echo only")], "eid",
            experiment_data=snapshot,
        )

        assert [c["config_name"] for c in captured["advance_config_infos"]] == ["only"]
        assert mock_modify.call_count == 1

    @patch.object(PlatformClient, "experiment_modify")
    @patch.object(PlatformClient, "experiment_list")
    def test_job_spec_and_queue_location_are_applied_to_each_config(self, mock_list, mock_modify):
        mock_list.return_value = MagicMock(
            returncode=0, stdout=json.dumps(self._experiment()), stderr=""
        )
        captured = {}
        mock_modify.side_effect = lambda path: (
            captured.update(json.loads(open(path).read()))
            or MagicMock(returncode=0)
        )
        spec = ExperimentSpec(
            name="exp", image="new-image", image_region="PRIVATE",
            queue_name="q1", cluster_id="explicit-cluster",
        )
        location = {
            "queue_id": "qid", "queue_name": "q1", "quota_id": "quota",
            "cluster_id": "lookup-cluster", "zone_id": "zone",
        }
        PlatformClient().modify_configs(
            [Job(name="job", command="echo")], "eid", spec=spec,
            queue_locations={"q1": location},
        )
        resource = captured["advance_config_infos"][0]["resource_config_list"][0]
        assert resource["basic_image"] == "new-image"
        assert resource["image_region"] == 2
        assert resource["queue_id"] == "qid"
        assert resource["queue_name"] == "q1"
        assert resource["cluster_id"] == "explicit-cluster"
        assert resource["zone_id"] == "zone"

    @patch.object(PlatformClient, "experiment_modify")
    @patch.object(PlatformClient, "experiment_list")
    def test_modify_failure_prevents_success(self, mock_list, mock_modify):
        mock_list.return_value = MagicMock(
            returncode=0, stdout=json.dumps(self._experiment()), stderr=""
        )
        mock_modify.return_value = MagicMock(
            returncode=1, stdout="", stderr="permission denied"
        )
        with pytest.raises(PlatformError, match="permission denied"):
            PlatformClient().modify_configs(
                [Job(name="job", command="echo")], "eid"
            )


class TestRunConfig:
    @patch.object(PlatformClient, "job_run")
    def test_runs_existing_config_and_parses_id(self, mock_run):
        job_id = "11111111-1111-1111-1111-111111111111"
        mock_run.return_value = MagicMock(
            returncode=0, stdout=f"Job {job_id} submitted", stderr=""
        )
        assert PlatformClient().run_config("eid", "cfg") == job_id
        mock_run.assert_called_once_with(exp_id="eid", config_name="cfg")

    @patch.object(PlatformClient, "job_run")
    def test_run_failure_raises(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="failed")
        with pytest.raises(PlatformError, match="failed"):
            PlatformClient().run_config("eid", "cfg")


class TestFindExperiment:
    @patch("subprocess.run")
    def test_finds_by_name(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps({"experiment_id": "eid-1", "experiment_name": "my-exp"}),
        )
        client = PlatformClient()
        exp = client.find_experiment("my-exp")
        assert exp["experiment_id"] == "eid-1"
        mock_run.assert_called_once_with(
            ["airsctl", "experiment", "list", "-N", "my-exp"],
            capture_output=True, text=True,
        )

    @patch("subprocess.run")
    def test_finds_by_uuid(self, mock_run):
        eid = "c347c52e-5772-4cfc-b03b-cf3b0499f79e"
        mock_run.return_value = MagicMock(
            returncode=0, stdout=json.dumps({"experiment_id": eid}))
        client = PlatformClient()
        assert client.find_experiment(eid)["experiment_id"] == eid
        mock_run.assert_called_once_with(
            ["airsctl", "experiment", "list", "-e", eid],
            capture_output=True, text=True,
        )

    @patch("subprocess.run")
    def test_not_found_returns_none(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="not found")
        assert PlatformClient().find_experiment("missing") is None

    @patch("subprocess.run")
    def test_invalid_json_returns_none(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="{broken")
        assert PlatformClient().find_experiment("exp") is None

    @patch("subprocess.run")
    def test_non_dict_json_returns_none(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout='["a"]')
        assert PlatformClient().find_experiment("exp") is None
