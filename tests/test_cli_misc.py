import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from jrun.cli import cli


class TestTemplate:
    def test_template_stdout(self, runner):
        result = runner.invoke(cli, ["template"])
        assert result.exit_code == 0
        assert "search:" in result.output or "job:" in result.output

    def test_template_to_file(self, runner, tmp_path):
        out_file = str(tmp_path / "out.yaml")
        result = runner.invoke(cli, ["template", "-f", out_file])
        assert result.exit_code == 0
        assert Path(out_file).exists()


class TestInit:
    def test_init_creates_jrun_dir(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(cli, ["init"])
        assert result.exit_code == 0
        assert (tmp_path / ".jrun" / "settings.json").exists()

    def test_init_writes_empty_settings(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(cli, ["init"])
        assert result.exit_code == 0
        settings = json.loads((tmp_path / ".jrun" / "settings.json").read_text())
        assert settings == {}

    def test_init_rejects_experiment_options(self, runner, tmp_path, monkeypatch):
        """init no longer binds an experiment; -N/-e are gone."""
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(cli, ["init", "-N", "my-exp"])
        assert result.exit_code != 0


class TestLogin:
    @patch("jrun.commands.misc.PlatformAPI")
    def test_login_saves_auth(self, mock_api_cls, runner, tmp_path, monkeypatch):
        monkeypatch.setattr("jrun.jiuding.rest.DEFAULT_AUTH_FILE", tmp_path / ".jrun" / "auth.json")
        mock_api = MagicMock()
        mock_api.get_userinfo.return_value = {"data": {"alias": "yzh"}}
        mock_api_cls.return_value = mock_api

        result = runner.invoke(cli, ["login", "--ak", "ak-1", "--sk", "sk-1"])

        assert result.exit_code == 0
        assert "yzh" in result.output
        mock_api_cls.assert_called_once_with(ak="ak-1", sk="sk-1")
        auth = tmp_path / ".jrun" / "auth.json"
        assert json.loads(auth.read_text()) == {"ak": "ak-1", "sk": "sk-1"}
        assert (auth.stat().st_mode & 0o777) == 0o600

    @patch("jrun.commands.misc.PlatformAPI")
    def test_login_failure(self, mock_api_cls, runner):
        from jrun.errors import ApiError
        mock_api = MagicMock()
        mock_api.get_userinfo.side_effect = ApiError("url", 401, "invalid access key")
        mock_api_cls.return_value = mock_api

        result = runner.invoke(cli, ["login", "--ak", "bad", "--sk", "bad"])
        assert result.exit_code == 1
        assert "Login failed" in result.output

    def test_template_file_in_repo_matches_builtin(self):
        """config/template.yaml must stay in sync with TEMPLATE_CONTENT."""
        from jrun.template import TEMPLATE_CONTENT
        repo_root = Path(__file__).resolve().parent.parent
        assert (repo_root / "config" / "template.yaml").read_text() == TEMPLATE_CONTENT


class TestWhoami:
    INFO = {"id": "42", "alias": "intern1", "real_name": "实习生", "identity_type": "院外",
            "active": True, "projsets": [{"id": "ps-1", "name": "baai-safety"}]}

    @patch("jrun.commands.misc.PlatformAPI")
    def test_text(self, mock_api_cls, runner):
        mock_api_cls.return_value.whoami.return_value = self.INFO
        result = runner.invoke(cli, ["whoami"])
        assert result.exit_code == 0
        assert "intern1 (id=42, 院外)" in result.output
        assert "baai-safety (ps-1)" in result.output

    @patch("jrun.commands.misc.PlatformAPI")
    def test_json(self, mock_api_cls, runner):
        import json
        mock_api_cls.return_value.whoami.return_value = self.INFO
        result = runner.invoke(cli, ["whoami", "--json"])
        assert json.loads(result.output) == self.INFO

    @patch("jrun.commands.misc.PlatformAPI")
    def test_disabled_is_shown(self, mock_api_cls, runner):
        mock_api_cls.return_value.whoami.return_value = {**self.INFO, "active": False}
        assert "(disabled)" in runner.invoke(cli, ["whoami"]).output

    @patch("jrun.commands.misc.PlatformAPI")
    def test_error_is_clean(self, mock_api_cls, runner):
        from jrun.errors import ApiError
        mock_api_cls.return_value.whoami.side_effect = ApiError("u", 401, "bad token")
        result = runner.invoke(cli, ["whoami"])
        assert result.exit_code == 1
        assert "bad token" in result.output and "Traceback" not in result.output
