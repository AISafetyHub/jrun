import json
from unittest.mock import MagicMock, patch

from jrun.cli import cli
from tests.conftest import SAMPLE_QUEUE


class TestQueues:
    @staticmethod
    def _use_real_quota_parser(mock_api_cls):
        from jrun.api import PlatformAPI as RealAPI
        mock_api_cls.queue_free_resources = RealAPI.queue_free_resources

    @patch("jrun.commands.inspect.PlatformAPI")
    def test_queues_grouped_by_cluster(self, mock_api_cls, runner, tmp_path, monkeypatch):
        self._use_real_quota_parser(mock_api_cls)
        monkeypatch.chdir(tmp_path)  # no .jrun: proj ids fall back to None
        mock_api = MagicMock()
        mock_api.list_queues.return_value = [SAMPLE_QUEUE]
        mock_api_cls.return_value = mock_api

        result = runner.invoke(cli, ["queues"])

        assert result.exit_code == 0
        assert "Cluster: 北京上庄" in result.output
        assert "jiuding_airs1_h100" in result.output
        assert "J1_ADV_H1-SXM4-80GB" in result.output
        # total 8, used 3, free 5
        assert "5" in result.output

    @patch("jrun.commands.inspect.PlatformAPI")
    def test_queues_cjk_location_keeps_model_column_aligned(self, mock_api_cls, runner, tmp_path, monkeypatch):
        from jrun.api import PlatformAPI as RealAPI
        mock_api_cls.queue_free_resources = RealAPI.queue_free_resources
        monkeypatch.chdir(tmp_path)
        mock_api = MagicMock()
        queue = dict(SAMPLE_QUEUE)
        queue["quotaInfoList"] = [{
            "clusterDisplayName": "北京-大兴",
            "zoneQuotaInfoList": [{"zoneDisplayName": "可用区A"}],
        }]
        mock_api.list_queues.return_value = [queue]
        mock_api_cls.return_value = mock_api

        result = runner.invoke(cli, ["queues", "--category", "dedicated"])

        assert result.exit_code == 0, result.output
        lines = result.output.splitlines()
        header = next(line for line in lines if "MODEL" in line and "CATEGORY" in line)
        row = next(line for line in lines if "jiuding_airs1_h100" in line)
        from jrun.formatter import display_width
        assert display_width(header[:header.index("MODEL")]) == display_width(
            row[:row.index("J1_ADV_H1-SXM4-80GB")]
        )

    @patch("jrun.commands.inspect.PlatformAPI")
    def test_queues_ids_flag(self, mock_api_cls, runner, tmp_path, monkeypatch):
        self._use_real_quota_parser(mock_api_cls)
        monkeypatch.chdir(tmp_path)
        mock_api = MagicMock()
        mock_api.list_queues.return_value = [SAMPLE_QUEUE]
        mock_api_cls.return_value = mock_api

        result = runner.invoke(cli, ["queues", "--ids"])
        assert "qid-1" in result.output
        assert "cid-1" in result.output
        assert "zid-1" in result.output

    @patch("jrun.commands.inspect.PlatformAPI")
    def test_queues_api_error(self, mock_api_cls, runner, tmp_path, monkeypatch):
        from jrun.errors import ApiError
        monkeypatch.chdir(tmp_path)
        mock_api = MagicMock()
        mock_api.list_queues.side_effect = ApiError("url", 401, "Unauthenticated")
        mock_api_cls.return_value = mock_api
        result = runner.invoke(cli, ["queues"])
        assert result.exit_code == 1
        assert "Unauthenticated" in result.output

    @patch("jrun.commands.inspect.PlatformAPI")
    def test_public_queue_cards_and_category(self, mock_api_cls, runner, tmp_path, monkeypatch):
        from jrun.api import PlatformAPI as RealAPI
        mock_api_cls.queue_free_resources = RealAPI.queue_free_resources
        monkeypatch.chdir(tmp_path)
        mock_api = MagicMock()
        mock_api.list_queues.return_value = []
        mock_api.list_shared_queues.return_value = [{
            "queueId": "shared-q", "name": "Public-Pool-H20",
            "queueCategory": "QUEUE_CATEGORY_PUBLIC",
            "acceleratorModel": "H20", "clusterId": "c", "zoneId": "z",
            "clusterDisplayName": "cluster", "zoneDisplayName": "zone",
            "quota": {"acceleratorCount": 8}, "used": {"acceleratorCount": 3},
        }]
        mock_api.list_shared_queue_job_counts.return_value = [{
            "queue": {"queueId": "shared-q"}, "runningCount": 1,
        }]
        mock_api.shared_queue_limits.return_value = {"max_jobs_per_projset": 4}
        mock_api_cls.return_value = mock_api

        result = runner.invoke(cli, ["queues", "--category", "public"])

        assert result.exit_code == 0, result.output
        assert "public" in result.output
        assert "Public-Pool-H20" in result.output
        assert "1/4" in result.output
        mock_api.list_queues.assert_not_called()

    @patch("jrun.commands.inspect.PlatformAPI")
    def test_default_lists_all_categories_with_idle_limit(self, mock_api_cls, runner,
                                                          tmp_path, monkeypatch):
        from jrun.api import PlatformAPI as RealAPI
        mock_api_cls.queue_free_resources = RealAPI.queue_free_resources
        monkeypatch.chdir(tmp_path)
        mock_api = MagicMock()
        mock_api.list_queues.return_value = [SAMPLE_QUEUE]
        shared = {"queueId": "shared-q", "name": "Public-Pool-H20", "clusterDisplayName": "c",
                  "quota": {"acceleratorCount": 8}, "used": {"acceleratorCount": 3}}
        mock_api.list_shared_queues.side_effect = lambda **kw: [
            {**shared, "queueCategory": "QUEUE_CATEGORY_" + kw["queue_category"].upper()}]
        mock_api.list_shared_queue_job_counts.return_value = [
            {"queue": {"queueId": "shared-q"}, "runningCount": 2}]
        mock_api.shared_queue_limits.return_value = {
            "max_jobs_per_projset": 4, "max_idle_jobs_per_projset": 5}
        mock_api_cls.return_value = mock_api

        result = runner.invoke(cli, ["queues"])

        assert result.exit_code == 0, result.output
        assert "jiuding_airs1_h100" in result.output
        assert "2/4" in result.output and "2/5" in result.output
        assert {c.kwargs["queue_category"] for c in mock_api.list_shared_queues.call_args_list} \
            == {"public", "idle"}

    @patch("jrun.commands.inspect.PlatformAPI")
    def test_count_errors_do_not_hide_queues(self, mock_api_cls, runner, tmp_path, monkeypatch):
        from jrun.api import PlatformAPI as RealAPI
        from jrun.errors import ApiError
        mock_api_cls.queue_free_resources = RealAPI.queue_free_resources
        monkeypatch.chdir(tmp_path)
        mock_api = MagicMock()
        mock_api.list_shared_queues.return_value = [{"queueId": "s", "name": "Public-Pool-X"}]
        mock_api.list_shared_queue_job_counts.side_effect = ApiError("u", 403, "forbidden")
        mock_api_cls.return_value = mock_api
        result = runner.invoke(cli, ["queues", "--category", "idle"])
        assert result.exit_code == 0, result.output
        assert "Public-Pool-X" in result.output


def _waiting_cell(output, queue_name):
    """Last column (WAITING) of the row naming ``queue_name``."""
    row = next(line for line in output.splitlines() if queue_name in line)
    return row.split()[-1]


class TestQueuesWaiting:
    @staticmethod
    def _api(mock_api_cls, dedicated=(), shared=()):
        from jrun.api import PlatformAPI as RealAPI
        mock_api_cls.queue_free_resources = RealAPI.queue_free_resources
        mock_api = MagicMock()
        mock_api.list_queues.return_value = list(dedicated)
        mock_api.list_shared_queues.side_effect = lambda **kw: [
            {**q, "queueCategory": "QUEUE_CATEGORY_" + kw["queue_category"].upper()}
            for q in shared]
        mock_api.list_shared_queue_job_counts.return_value = []
        mock_api.shared_queue_limits.return_value = {}
        mock_api.shared_waiting_counts.return_value = {}
        mock_api.queue_waiting_count.return_value = 0
        mock_api_cls.return_value = mock_api
        return mock_api

    @patch("jrun.commands.inspect.PlatformAPI")
    def test_fifo_dedicated_queue_shows_waiting_jobs(self, mock_api_cls, runner):
        api = self._api(mock_api_cls, dedicated=[{**SAMPLE_QUEUE, "scheduleStrategy": 0}])
        api.queue_waiting_count.return_value = 3
        result = runner.invoke(cli, ["queues", "--category", "dedicated"])
        assert result.exit_code == 0, result.output
        assert "WAITING" in result.output
        assert _waiting_cell(result.output, "jiuding_airs1_h100") == "3"
        api.queue_waiting_count.assert_called_once_with("qid-1", proj_id="p1", projset_id="ps1")

    @patch("jrun.commands.inspect.PlatformAPI")
    def test_traverse_queue_is_not_counted(self, mock_api_cls, runner):
        api = self._api(mock_api_cls, dedicated=[{**SAMPLE_QUEUE, "scheduleStrategy": 1}])
        result = runner.invoke(cli, ["queues", "--category", "dedicated"])
        assert _waiting_cell(result.output, "jiuding_airs1_h100") == "-"
        api.queue_waiting_count.assert_not_called()

    @patch("jrun.commands.inspect.PlatformAPI")
    def test_shared_pool_waiting_is_priority_aware(self, mock_api_cls, runner):
        pool = {"queueId": "s", "name": "Public-Pool-A100", "clusterDisplayName": "c",
                "quota": {"acceleratorCount": 8}, "used": {"acceleratorCount": 3}}
        api = self._api(mock_api_cls, shared=[pool])
        api.shared_waiting_counts.return_value = {
            ("QUEUE_CATEGORY_PUBLIC", "s"): 2, ("QUEUE_CATEGORY_IDLE", "s"): 5}
        public = runner.invoke(cli, ["queues", "--category", "public"])
        idle = runner.invoke(cli, ["queues", "--category", "idle"])
        assert _waiting_cell(public.output, "Public-Pool-A100") == "2"
        assert _waiting_cell(idle.output, "Public-Pool-A100") == "7"

    @patch("jrun.commands.inspect.PlatformAPI")
    def test_waiting_errors_show_dash(self, mock_api_cls, runner):
        from jrun.errors import ApiError
        api = self._api(mock_api_cls, dedicated=[{**SAMPLE_QUEUE, "scheduleStrategy": 2}])
        api.queue_waiting_count.side_effect = ApiError("job/select", 403, "forbidden")
        result = runner.invoke(cli, ["queues", "--category", "dedicated"])
        assert result.exit_code == 0, result.output
        assert _waiting_cell(result.output, "jiuding_airs1_h100") == "-"


class TestGpuUsage:
    SAMPLE_ITEMS = [
        {"jobId": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "ownerName": "dev1",
         "queueName": "q1", "clusterDisplayName": "北京-大兴", "stateName": "Running",
         "acceleratorReq": 8, "acceleratorUtil": 0.723, "fbmemUtil": 0.456,
         "cpuUtil": 0.1, "memUtil": 0.2},
        {"jobId": "bbbbbbbb-bbbb-cccc-dddd-eeeeeeeeeeee", "ownerName": "dev2",
         "queueName": "q2", "clusterName": "dx-calc1", "stateName": "Cancelled",
         "acceleratorReq": 0, "acceleratorUtil": -1, "cpuUtil": -1, "memUtil": -1},
    ]

    def _mock_api(self, mock_api_cls, items):
        mock_api = MagicMock()
        mock_api.job_snapshots_all.return_value = items
        mock_api_cls.return_value = mock_api
        return mock_api

    @patch("jrun.commands.inspect.PlatformAPI")
    def test_running_only_by_default(self, mock_api_cls, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        self._mock_api(mock_api_cls, self.SAMPLE_ITEMS)

        result = runner.invoke(cli, ["gpu-usage"])

        assert result.exit_code == 0
        assert "aaaaaaaa" in result.output
        assert "72.3%" in result.output
        assert "45.6%" in result.output  # GMEM% from fbmemUtil
        assert "GMEM%" in result.output
        assert "bbbbbbbb" not in result.output  # Cancelled filtered out

    @patch("jrun.commands.inspect.PlatformAPI")
    def test_all_flag_includes_finished(self, mock_api_cls, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        self._mock_api(mock_api_cls, self.SAMPLE_ITEMS)

        result = runner.invoke(cli, ["gpu-usage", "--all", "--include-cpu"])
        assert "bbbbbbbb" in result.output
        # acceleratorUtil=-1 renders as '-'
        assert "-" in result.output

    @patch("jrun.commands.inspect.PlatformAPI")
    def test_gpu_only_by_default(self, mock_api_cls, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        cpu_job = {"jobId": "cccccccc-0000-0000-0000-000000000000",
                   "ownerName": "dev3", "queueName": "q1", "stateName": "Running",
                   "acceleratorReq": 0, "acceleratorUtil": -1}
        self._mock_api(mock_api_cls, self.SAMPLE_ITEMS + [cpu_job])

        result = runner.invoke(cli, ["gpu-usage"])
        assert "aaaaaaaa" in result.output
        assert "cccccccc" not in result.output  # CPU-only job hidden

        result = runner.invoke(cli, ["gpu-usage", "--include-cpu"])
        assert "cccccccc" in result.output

    @patch("jrun.commands.inspect.PlatformAPI")
    def test_status_filter(self, mock_api_cls, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        self._mock_api(mock_api_cls, self.SAMPLE_ITEMS)

        result = runner.invoke(cli, ["gpu-usage", "-s", "cancelled", "--include-cpu"])
        assert result.exit_code == 0
        assert "bbbbbbbb" in result.output
        assert "aaaaaaaa" not in result.output

    @patch("jrun.commands.inspect.PlatformAPI")
    def test_sort_by_gpus(self, mock_api_cls, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        items = [
            {"jobId": "11111111-0000-0000-0000-000000000000", "ownerName": "a",
             "queueName": "q", "stateName": "Running", "acceleratorReq": 1,
             "beginTime": "2000"},
            {"jobId": "22222222-0000-0000-0000-000000000000", "ownerName": "b",
             "queueName": "q", "stateName": "Running", "acceleratorReq": 8,
             "beginTime": "1000"},
        ]
        self._mock_api(mock_api_cls, items)

        # Default: newest first (time desc).
        result = runner.invoke(cli, ["gpu-usage"])
        assert result.output.index("11111111") < result.output.index("22222222")

        # Sort by GPU count desc.
        result = runner.invoke(cli, ["gpu-usage", "--sort", "gpus"])
        assert result.output.index("22222222") < result.output.index("11111111")

        # Ascending time: oldest first.
        result = runner.invoke(cli, ["gpu-usage", "--asc"])
        assert result.output.index("22222222") < result.output.index("11111111")

    @patch("jrun.commands.inspect.PlatformAPI")
    def test_fetches_via_job_snapshots_all(self, mock_api_cls, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        mock_api = self._mock_api(mock_api_cls, self.SAMPLE_ITEMS)

        result = runner.invoke(cli, ["gpu-usage"])
        assert result.exit_code == 0
        mock_api.job_snapshots_all.assert_called_once()

    @patch("jrun.commands.inspect.PlatformAPI")
    def test_admin_hint_on_403(self, mock_api_cls, runner, tmp_path, monkeypatch):
        from jrun.errors import ApiError
        monkeypatch.chdir(tmp_path)
        mock_api = MagicMock()
        mock_api.job_snapshots_all.side_effect = ApiError("url", 403, "forbidden")
        mock_api_cls.return_value = mock_api
        result = runner.invoke(cli, ["gpu-usage"])
        assert result.exit_code == 1
        assert "admin token" in result.output


class TestQueuesJson:
    @patch("jrun.commands.inspect.PlatformAPI")
    def test_json_rows(self, mock_api_cls, runner, tmp_path, monkeypatch):
        from jrun.api import PlatformAPI as RealAPI
        mock_api_cls.queue_free_resources = RealAPI.queue_free_resources
        monkeypatch.chdir(tmp_path)
        mock_api = MagicMock()
        mock_api.list_shared_queues.return_value = [{
            "queueId": "shared-q", "name": "Public-Pool-H20",
            "queueCategory": "QUEUE_CATEGORY_IDLE", "acceleratorModel": "H20",
            "clusterId": "c", "zoneId": "z", "clusterDisplayName": "cluster",
            "zoneDisplayName": "zone",
            "quota": {"acceleratorCount": 8}, "used": {"acceleratorCount": 3},
        }]
        mock_api.list_shared_queue_job_counts.return_value = [{
            "queue": {"queueId": "shared-q"}, "runningCount": 2}]
        mock_api.shared_queue_limits.return_value = {"max_idle_jobs_per_projset": 5}
        mock_api.shared_waiting_counts.return_value = {}
        mock_api_cls.return_value = mock_api

        result = runner.invoke(cli, ["queues", "--category", "idle", "--projset-id", "ps-1",
                                     "--json"])

        assert result.exit_code == 0, result.output
        row = json.loads(result.output)["queues"][0]
        assert row["category"] == "idle"
        assert row["queue_name"] == "Public-Pool-H20"
        assert row["queue_id"] == "shared-q"
        assert row["resources"] == [{"model": "H20", "total": 8, "used": 3, "free": 5}]
        assert (row["running"], row["limit"]) == (2, 5)
        assert mock_api.list_shared_queues.call_args.kwargs["projset_id"] == "ps-1"

    @patch("jrun.commands.inspect.PlatformAPI")
    def test_json_empty(self, mock_api_cls, runner):
        mock_api = MagicMock()
        mock_api.list_queues.return_value = []
        mock_api_cls.return_value = mock_api
        result = runner.invoke(cli, ["queues", "--category", "dedicated", "--json"])
        assert json.loads(result.output) == {"queues": []}
