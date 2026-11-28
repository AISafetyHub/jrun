import json
import os

import pytest
from unittest.mock import patch, MagicMock

from jrun.errors import PlatformError
from jrun.models import (ExperimentSpec, Job, ResourceConfig, RoleConfig, RunOptions,
                         job_target)
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
    def test_job_run_maps_run_options_to_flags(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        PlatformClient().job_run(
            "eid", "cfg",
            run_options=RunOptions(duration_hours=8, enable_checkpoint=True,
                                   queue_priority_mode="high", usage_desc="research"),
        )
        mock_run.assert_called_once_with(
            ["airsctl", "job", "run", "-e", "eid", "-n", "cfg",
             "--duration-hours", "8", "--enable-checkpoint",
             "--queue-priority-mode", "high", "--usage-desc", "research"],
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
        client._run(["job", "list"], check=False)

        assert mock_run.call_count == 2
        mock_sleep.assert_called_once_with(35)


class TestGetExperiment:
    @patch("subprocess.run")
    def test_reads_once_and_caches(self, mock_run):
        exp_data = {"experiment_id": "eid",
                    "advance_config_infos": [{"config_name": f"cfg_{i}"} for i in range(5)]}
        mock_run.return_value = MagicMock(returncode=0, stdout=json.dumps(exp_data), stderr="")
        client = PlatformClient()
        first = client.get_experiment("eid")
        assert len(PlatformClient.experiment_configs(first)) == 5
        first["advance_config_infos"].clear()  # callers get copies
        assert len(client.experiment_configs(client.get_experiment("eid"))) == 5
        assert mock_run.call_count == 1

    @pytest.mark.parametrize("returncode,stdout", [
        (1, ""), (0, "not json"), (0, "[1]"), (0, '{"no_id": true}'),
    ])
    @patch("subprocess.run")
    def test_bad_responses(self, mock_run, returncode, stdout):
        mock_run.return_value = MagicMock(returncode=returncode, stdout=stdout, stderr="err")
        assert PlatformClient().get_experiment("eid") is None
        with pytest.raises(PlatformError):
            PlatformClient().get_experiment("eid", check=True)


def _template_experiment():
    return {
        "experiment_id": "eid",
        "advance_config_infos": [{
            "config_name": "base",
            "command": "echo old",
            "resource_config_list": [{
                "priority": "medium",
                "basic_image": "old-image:1",
                "queue_id": "old-q",
                "queue_name": "old-queue",
                "role_info_list": [{
                    "name": "Master", "replicas": 1, "resource_region": 4,
                    "resource_request_detail": {"accelerator_resource_name": "nvidia.com/gpu"},
                }],
            }],
        }],
    }


def _modify(job, spec=None, location=None, experiment=None):
    """Run modify_configs on a snapshot and return the written document."""
    captured = {}

    def capture(path):
        captured.update(json.loads(open(path).read()))
        return MagicMock(returncode=0)

    target_key = job_target(job, spec).location_key
    with patch.object(PlatformClient, "experiment_modify", side_effect=capture):
        PlatformClient().modify_configs(
            [job], "eid", spec=spec,
            queue_locations={target_key: location} if location else None,
            experiment_data=experiment or _template_experiment(),
        )
    return captured


def _resource(document):
    return document["advance_config_infos"][0]["resource_config_list"][0]


class TestBuildJobConfig:
    LOCATION = {"queue_id": "qid-1", "queue_name": "q1", "quota_id": "quota-1",
                "cluster_id": "cid", "zone_id": "zid",
                "region_name": "北京", "zone_name": "A",
                "accelerator_model": "H100"}

    def test_spec_overrides_image_and_queue(self):
        spec = ExperimentSpec(name="e", image="new-image:2", queue_name="q1")
        res = _resource(_modify(Job(name="j", command="echo hi"), spec, self.LOCATION))
        assert res["basic_image"] == "new-image:2"
        # airsctl modify JSON needs enums as integers
        assert res["image_region"] == 1
        assert res["queue_category"] == 0
        assert (res["queue_id"], res["queue_name"], res["quota_id"]) == ("qid-1", "q1", "quota-1")
        assert (res["cluster_id"], res["zone_id"]) == ("cid", "zid")
        assert (res["cluster_display_name"], res["zone_display_name"]) == ("北京", "A")
        detail = res["role_info_list"][0]["resource_request_detail"]
        assert detail["accelerator_model"] == "H100"
        assert detail["accelerator_resource_name"] == "nvidia.com/gpu"
        assert res["priority"] == "medium"  # untouched template value

    @pytest.mark.parametrize("category,expected", [("idle", "low"), ("public", "high")])
    def test_shared_job_gets_required_priority(self, category, expected):
        # Regression: public/idle reject a priority inherited from the template
        # config (QueueCategoryPriorityMismatch), e.g. a public job submitted
        # after an idle config became the template.
        spec = ExperimentSpec(name="e", image="img", queue_name="q1",
                              queue_category=category)
        assert _resource(_modify(Job(name="j"), spec, self.LOCATION))["priority"] == expected

    def test_dedicated_keeps_template_priority(self):
        spec = ExperimentSpec(name="e", image="img", queue_name="q1")
        assert _resource(_modify(Job(name="j"), spec, self.LOCATION))["priority"] == "medium"

    def test_explicit_cluster_zone_win(self):
        spec = ExperimentSpec(name="e", image="img", queue_name="q1",
                              region_name="explicit-region", zone_name="explicit-zone")
        res = _resource(_modify(Job(name="j"), spec, self.LOCATION))
        assert res["cluster_display_name"] == "explicit-region"
        assert res["zone_display_name"] == "explicit-zone"

    def test_image_region_private_maps_to_int(self):
        spec = ExperimentSpec(name="e", image="img", image_region="PRIVATE")
        assert _resource(_modify(Job(name="j"), spec))["image_region"] == 2

    def test_job_image_wins_over_spec(self):
        spec = ExperimentSpec(name="e", image="spec-img")
        res = _resource(_modify(Job(name="j", image="job-img"), spec))
        assert res["basic_image"] == "job-img"

    def test_no_spec_keeps_template(self):
        res = _resource(_modify(Job(name="j", command="echo hi")))
        assert res["basic_image"] == "old-image:1"
        assert res["queue_id"] == "old-q"
        assert "queue_category" not in res

    def test_explicit_accelerator_model_beats_queue_lookup(self):
        job = Job(name="j", resource_config=ResourceConfig(
            queue_name="q1", accelerator_model="A100", accelerator_count=2))
        res = _resource(_modify(job, None, self.LOCATION))
        detail = res["role_info_list"][0]["resource_request_detail"]
        assert detail["accelerator_model"] == "A100"
        assert detail["accelerator_count"] == 2

    def test_resource_region_applies_to_master_role(self):
        job = Job(name="j", resource_config=ResourceConfig(resource_region=1))
        res = _resource(_modify(job))
        assert res["role_info_list"][0]["resource_region"] == 1
        assert "resource_region" not in res

    def test_roles_inherit_top_level_resources(self):
        job = Job(name="j", resource_config=ResourceConfig(
            cpu_cores=8, accelerator_count=4,
            roles=[RoleConfig(name="Master"),
                   RoleConfig(name="Worker", replicas=3, accelerator_count=8,
                              extra_label_info={"k": "v"})],
        ))
        roles = _resource(_modify(job))["role_info_list"]
        assert [(r["name"], r["replicas"]) for r in roles] == [("Master", 1), ("Worker", 3)]
        assert roles[0]["resource_request_detail"]["accelerator_count"] == 4
        assert roles[1]["resource_request_detail"]["accelerator_count"] == 8
        assert roles[1]["resource_request_detail"]["cpu_cores"] == 8
        # template detail and resource_region are inherited by every role
        assert roles[1]["resource_request_detail"]["accelerator_resource_name"] == "nvidia.com/gpu"
        assert roles[1]["resource_region"] == 4
        assert roles[1]["extra_label_info"] == {"k": "v"}

    def test_envs_and_job_fields(self):
        experiment = _template_experiment()
        experiment["advance_config_infos"][0]["hyper_parameter_present"] = []
        job = Job(name="j", envs={"A": 1}, git_branch="dev",
                  platform_overrides={"git_branch": "raw", "slots_per_worker": 2})
        config = _modify(job, experiment=experiment)["advance_config_infos"][0]
        assert config["hyper_parameter"] == {"A": "1"}
        assert config["hyper_parameter_present"] == [{"key": "A", "value": "1"}]
        assert config["git_branch"] == "dev"  # typed field beats raw override
        assert config["slots_per_worker"] == 2

    def test_experiment_level_fields_written_to_document(self):
        spec = ExperimentSpec(name="e", train_frame="TensorFlow",
                              platform_overrides={"hetero_type": 2})
        document = _modify(Job(name="j"), spec)
        assert document["train_frame"] == "TensorFlow"
        assert document["hetero_type"] == 2


class TestMounts:
    """experiment.mounts -> snake_case storage_info in the modify document."""

    STORAGE = [
        {"directory_type": 5, "access_mode": 1, "user_id": 3198, "medium": 1},
        {"directory_type": 4, "access_mode": 1, "medium": 1},
        {"directory_type": 2, "access_mode": 1, "medium": 1},
    ]

    def _experiment(self, storage=None):
        experiment = _template_experiment()
        experiment["creator_id"] = 4242
        if storage is not None:
            experiment["storage_info"] = storage
        return experiment

    def test_unset_mounts_leave_document_untouched(self):
        document = _modify(Job(name="j"), ExperimentSpec(name="e"),
                           experiment=self._experiment(self.STORAGE))
        assert document["storage_info"] == self.STORAGE

    def test_drop_and_read_only_keep_platform_fields(self):
        spec = ExperimentSpec.from_dict({"name": "e", "mounts": {"project": False,
                                                                 "projset": "ro"}})
        document = _modify(Job(name="j"), spec, experiment=self._experiment(self.STORAGE))
        assert document["storage_info"] == [
            {"directory_type": 5, "access_mode": 1, "user_id": 3198, "medium": 1},
            {"directory_type": 2, "access_mode": 0, "medium": 1},
        ]

    def test_remount_adds_entries_snake_case(self):
        # Experiment created with nothing mounted; mounting again needs fresh
        # entries (home gets the creator as user_id).
        spec = ExperimentSpec.from_dict({"name": "e", "mounts": {"home": "rw"}})
        document = _modify(Job(name="j"), spec, experiment=self._experiment([]))
        assert document["storage_info"] == [
            {"directory_type": 5, "medium": 1, "user_id": 4242, "access_mode": 1},
            {"directory_type": 4, "medium": 1, "access_mode": 1},
            {"directory_type": 2, "medium": 1, "access_mode": 1},
        ]
        assert all("directoryType" not in e for e in document["storage_info"])

    def test_all_unmounted_writes_empty_list(self):
        spec = ExperimentSpec.from_dict({"name": "e", "mounts": {
            "home": False, "project": False, "projset": False}})
        document = _modify(Job(name="j"), spec, experiment=self._experiment(self.STORAGE))
        assert document["storage_info"] == []


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
            queue_name="q1", region_name="explicit-region",
        )
        location = {
            "queue_id": "qid", "queue_name": "q1", "quota_id": "quota",
            "cluster_id": "lookup-cluster", "zone_id": "zone",
        }
        PlatformClient().modify_configs(
            [Job(name="job", command="echo")], "eid", spec=spec,
            queue_locations={job_target(Job(name="job"), spec).location_key: location},
        )
        resource = captured["advance_config_infos"][0]["resource_config_list"][0]
        assert resource["basic_image"] == "new-image"
        assert resource["image_region"] == 2
        assert resource["queue_id"] == "qid"
        assert resource["queue_name"] == "q1"
        assert resource["cluster_display_name"] == "explicit-region"
        assert resource["zone_id"] == "zone"

    @patch("subprocess.run")
    def test_modify_failure_raises_and_cleans_temp_file(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="permission denied")
        with pytest.raises(PlatformError, match="permission denied"):
            PlatformClient().modify_configs(
                [Job(name="job", command="echo")], "eid",
                experiment_data=self._experiment(),
            )
        args = mock_run.call_args[0][0]
        assert args[:4] == ["airsctl", "experiment", "modify", "-f"]
        assert not os.path.exists(args[4])

    @pytest.mark.parametrize("jobs,match", [
        ([], "no jobs"),
        ([Job(name="a"), Job(name="a")], "duplicate job names: a"),
        ([Job(name=" ")], "non-empty name"),
    ])
    def test_invalid_job_sets_rejected(self, jobs, match):
        with pytest.raises(PlatformError, match=match):
            PlatformClient().modify_configs(jobs, "eid", experiment_data=self._experiment())

    def test_template_required(self):
        with pytest.raises(PlatformError, match="no existing config"):
            PlatformClient().modify_configs(
                [Job(name="a")], "eid",
                experiment_data={"experiment_id": "eid", "advance_config_infos": []},
            )

    @patch.object(PlatformClient, "experiment_modify")
    @patch.object(PlatformClient, "experiment_list")
    def test_public_queue_roles_and_overrides(self, mock_list, mock_modify):
        mock_list.return_value = MagicMock(
            returncode=0, stdout=json.dumps(self._experiment()), stderr=""
        )
        captured = {}
        mock_modify.side_effect = lambda path: (
            captured.update(json.loads(open(path).read())) or MagicMock(returncode=0)
        )
        job = Job(
            name="public-job", command="echo public",
            resource_config=ResourceConfig(
                queue_name="Public-Pool-H20", queue_category="public",
                roles=[RoleConfig(name="Master", replicas=1, accelerator_count=2,
                                  resource_region="ACCELERATOR_SUITE")],
                platform_overrides={"customResourceField": "value"},
            ),
            run_options=RunOptions(duration_hours=2, usage_desc="benchmark",
                                   queue_priority_mode="normal"),
            platform_overrides={"customConfigField": "value"},
        )
        PlatformClient().modify_configs(
            [job], "eid", queue_locations={
                "QUEUE_CATEGORY_PUBLIC|||Public-Pool-H20||": {
                    "queue_id": "shared-q", "queue_name": "Public-Pool-H20",
                    "quota_id": "quota", "shared_quota_id": "shared-quota",
                    "queue_category": "QUEUE_CATEGORY_PUBLIC",
                    "cluster_id": "cluster", "zone_id": "zone",
                }
            }, experiment_data=self._experiment()
        )
        resource = captured["advance_config_infos"][0]["resource_config_list"][0]
        assert resource["queue_category"] == 1
        assert resource["shared_quota_id"] == "shared-quota"
        assert resource["cluster_id"] == "cluster"
        assert resource["customResourceField"] == "value"
        assert resource["role_info_list"][0]["resource_request_detail"]["accelerator_count"] == 2
        assert captured["advance_config_infos"][0]["customConfigField"] == "value"


class TestRunConfig:
    @patch("subprocess.run")
    def test_runs_existing_config_and_parses_id(self, mock_run):
        job_id = "11111111-1111-1111-1111-111111111111"
        mock_run.return_value = MagicMock(returncode=0, stdout=f"Job {job_id} submitted", stderr="")
        assert PlatformClient().run_config("eid", "cfg") == job_id
        mock_run.assert_called_once_with(
            ["airsctl", "job", "run", "-e", "eid", "-n", "cfg"],
            capture_output=True, text=True,
        )

    @patch("subprocess.run")
    def test_run_failure_raises(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="failed")
        with pytest.raises(PlatformError, match="failed"):
            PlatformClient().run_config("eid", "cfg")

    @patch("subprocess.run")
    def test_unparseable_output_raises_instead_of_faking_an_id(self, mock_run):
        # Regression: the config name used to be returned as the "job ID",
        # silently corrupting the tracker.
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
        with pytest.raises(PlatformError, match="could not find a job ID"):
            PlatformClient().run_config("eid", "cfg")

    @patch("subprocess.run")
    def test_public_queue_options_validated(self, mock_run):
        with pytest.raises(PlatformError, match="public queue requires"):
            PlatformClient().run_config("eid", "cfg", queue_category="public")
        mock_run.assert_not_called()

    @pytest.mark.parametrize("output,expected", [
        ('{"job_id": "abc"}', "abc"),
        ('{"data": {"jobId": "nested"}}', "nested"),
        ("job_id: plain-1", "plain-1"),
        ("Job 22222222-2222-2222-2222-222222222222 created", "22222222-2222-2222-2222-222222222222"),
        ("", None),
        ('{"status": "ok"}', None),
    ])
    def test_parse_job_id(self, output, expected):
        assert PlatformClient._parse_job_id(output) == expected


class TestRemoveConfigs:
    @patch.object(PlatformClient, "experiment_modify")
    @patch("subprocess.run")
    def test_removes_named_configs_only(self, mock_run, mock_modify):
        experiment = {"experiment_id": "eid", "advance_config_infos": [
            {"config_name": "a"}, {"config_name": "b"}, {"config_name": "c"}]}
        mock_run.return_value = MagicMock(returncode=0, stdout=json.dumps(experiment))
        captured = {}
        mock_modify.side_effect = lambda path: captured.update(json.loads(open(path).read()))
        PlatformClient().remove_configs("eid", {"a", "c"})
        assert [c["config_name"] for c in captured["advance_config_infos"]] == ["b"]

    @patch.object(PlatformClient, "experiment_modify")
    @patch("subprocess.run")
    def test_no_match_no_modify(self, mock_run, mock_modify):
        experiment = {"experiment_id": "eid", "advance_config_infos": [{"config_name": "a"}]}
        mock_run.return_value = MagicMock(returncode=0, stdout=json.dumps(experiment))
        PlatformClient().remove_configs("eid", {"zzz"})
        mock_modify.assert_not_called()


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

    @patch("subprocess.run")
    def test_name_mismatch_returns_none(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps({"experiment_id": "eid-1", "experiment_name": "my-exp-2"}),
        )
        assert PlatformClient().find_experiment("my-exp") is None

    @patch("subprocess.run")
    def test_by_name_caches_detail_for_submit(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout=json.dumps(
            {"experiment_id": "eid-1", "experiment_name": "my-exp", "advance_config_infos": []}))
        client = PlatformClient()
        client.find_experiment("my-exp")
        client.get_experiment("eid-1")
        assert mock_run.call_count == 1


class TestOffPlatformContext:
    @patch("subprocess.run")
    def test_set_context_clears_experiment_cache(self, mock_run):
        exp_data = {"experiment_id": "eid", "advance_config_infos": []}
        mock_run.return_value = MagicMock(returncode=0, stdout=json.dumps(exp_data), stderr="")
        client = PlatformClient(gateway_url="http://gw")
        client.get_experiment("eid")
        client.set_context("p2", "ps2")  # experiment lookups are project-scoped
        client.get_experiment("eid")
        assert mock_run.call_count == 2
        assert mock_run.call_args.args[0][-4:] == ["-P", "ps2", "-p", "p2"]
        assert client.context == ("p2", "ps2")
