import pytest

from jrun_server.settings import Settings

BASE = {"AIRS_AK": "ak", "AIRS_SK": "sk", "JRUN_SERVE_PROJECT_SETS": "ps-a, ps-b"}


def test_defaults():
    s = Settings.from_env(BASE)
    assert s.project_sets == ["ps-a", "ps-b"]
    assert s.categories == ["public"]
    assert s.users == []
    assert (s.host, s.port, str(s.data_dir)) == ("0.0.0.0", 8080, "/data")


def test_overrides():
    s = Settings.from_env({**BASE, "JRUN_SERVE_USERS": "a,b", "JRUN_SERVE_CATEGORIES": "Public,idle",
                           "JRUN_SERVE_PORT": "9000", "JRUN_SERVE_DATA": "/x"})
    assert s.users == ["a", "b"]
    assert s.categories == ["public", "idle"]
    assert (s.port, str(s.data_dir)) == (9000, "/x")


@pytest.mark.parametrize("env, message", [
    ({"AIRS_AK": "ak", "AIRS_SK": "sk"}, "JRUN_SERVE_PROJECT_SETS"),
    ({"JRUN_SERVE_PROJECT_SETS": "ps"}, "AIRS_AK"),
    ({**BASE, "JRUN_SERVE_CATEGORIES": "vip"}, "unknown categories"),
])
def test_invalid(env, message):
    with pytest.raises(ValueError, match=message):
        Settings.from_env(env)
