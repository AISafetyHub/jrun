import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))  # jrun_server without installing
sys.path.insert(0, str(HERE))         # server_fakes

from jrun_server.settings import Settings  # noqa: E402


@pytest.fixture
def settings(tmp_path):
    return Settings(project_sets=["baai-safety"], data_dir=tmp_path / "data")
