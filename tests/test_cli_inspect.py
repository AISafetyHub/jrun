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
