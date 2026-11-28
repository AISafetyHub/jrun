import pytest
import yaml

from jrun_server.rewrite import (EXPERIMENT_KEYS, JOB_KEYS, RESOURCE_KEYS, ROLE_KEYS,
                                 RewriteError, dump_config, rewrite_config)

PUBLIC = ["public"]


def config(**job_rc):
    return {
        "experiment": {"name": "exp", "image": "harbor/x:1", "project_set_name": "baai-safety"},
        "remote": {"url": "http://svc"},
        "job": {"name": "j", "command": "echo $HOME",
                "resource_config": {"queue_name": "pool", "queue_category": "public", **job_rc},
                "run_options": {"duration_hours": 1}},
    }


def test_prefixes_experiment_and_forces_home_off():
    cfg = rewrite_config(config(), "intern1", PUBLIC)
    assert cfg["experiment"]["name"] == "intern1-exp"
    assert cfg["experiment"]["mounts"] == {"home": False}
    assert "remote" not in cfg


def test_keeps_other_mounts_and_prefix_is_idempotent():
    c = config()
    c["experiment"].update(name="intern1-exp", mounts={"home": "rw", "projset": "ro"})
    cfg = rewrite_config(c, "intern1", PUBLIC)
    assert cfg["experiment"]["name"] == "intern1-exp"
    assert cfg["experiment"]["mounts"] == {"home": False, "projset": "ro"}


def test_input_is_not_mutated():
    c = config()
    rewrite_config(c, "intern1", PUBLIC)
    assert c["experiment"]["name"] == "exp" and "remote" in c


def test_alias_is_sanitized():
    cfg = rewrite_config(config(), "a b/../c", PUBLIC)
    assert cfg["experiment"]["name"] == "a-b-c-exp"


def test_category_list_is_narrowed():
    cfg = rewrite_config(config(queue_name="auto", queue_category=["dedicated", "public", "idle"]),
                         "u", PUBLIC)
    assert cfg["job"]["resource_config"]["queue_category"] == "public"


@pytest.mark.parametrize("category", ["idle", "dedicated", ["dedicated", "idle"]])
def test_disallowed_category_rejected(category):
    with pytest.raises(RewriteError, match="not submitted by this service|none of"):
        rewrite_config(config(queue_category=category), "u", PUBLIC)


def test_missing_category_means_dedicated_and_is_rejected():
    c = config()
    del c["job"]["resource_config"]["queue_category"]
    with pytest.raises(RewriteError, match="dedicated"):
        rewrite_config(c, "u", PUBLIC)


def test_experiment_level_category_narrowed():
    c = config()
    del c["job"]["resource_config"]["queue_category"]
    c["experiment"]["queue_category"] = ["idle", "public"]
    cfg = rewrite_config(c, "u", PUBLIC)
    assert cfg["experiment"]["queue_category"] == "public"


@pytest.mark.parametrize("where, key", [
    ("experiment", "platform_overrides"),
    ("experiment", "model_storage_info"),
    ("experiment", "storage_info"),
    ("job", "platform_overrides"),
    ("job", "dataset"),
    ("job", "code"),
    ("job", "git_prefix"),
    ("rc", "platform_overrides"),
    ("rc", "some_raw_platform_field"),
    ("role", "platform_overrides"),
    ("top", "extra"),
])
def test_unlisted_keys_rejected(where, key):
    c = config()
    if where == "experiment":
        c["experiment"][key] = {}
    elif where == "job":
        c["job"][key] = {}
    elif where == "rc":
        c["job"]["resource_config"][key] = {}
    elif where == "role":
        c["job"]["resource_config"]["roles"] = [{"name": "Master", key: {}}]
    else:
        c[key] = {}
    with pytest.raises(RewriteError, match="not allowed"):
        rewrite_config(c, "u", PUBLIC)


@pytest.mark.parametrize("where", ["experiment", "job"])
def test_private_image_region_rejected(where):
    c = config()
    c[where]["image_region"] = "PRIVATE"
    with pytest.raises(RewriteError, match="PUBLIC"):
        rewrite_config(c, "u", PUBLIC)


def test_search_template_checked():
    c = config()
    c["search"] = {"name": "s", "job_template": c.pop("job"),
                   "params": [{"name": "lr", "values": [1, 2]}]}
    c["search"]["job_template"]["platform_overrides"] = {"x": 1}
    with pytest.raises(RewriteError, match="search.job_template"):
        rewrite_config(c, "u", PUBLIC)


def test_requires_experiment_name():
    c = config()
    c["experiment"] = {"image": "x"}
    with pytest.raises(RewriteError, match="experiment"):
        rewrite_config(c, "u", PUBLIC)


def test_dump_escapes_dollars_so_service_env_is_never_substituted():
    c = config()
    c["job"]["command"] = "echo $AIRS_SK ${HOME} $$X"
    text = dump_config(rewrite_config(c, "u", PUBLIC))
    assert "$AIRS_SK" not in text.replace("$$AIRS_SK", "")
    # jrun turns $$ back into $: the caller's literal text survives exactly.
    assert yaml.safe_load(text.replace("$$", "$"))["job"]["command"] == "echo $AIRS_SK ${HOME} $$X"


def test_allowlists_match_jrun_models():
    """Every allowlisted key must be a real jrun field (no silent typos)."""
    models = pytest.importorskip("jrun.models")
    from dataclasses import fields
    names = lambda cls: {f.name for f in fields(cls)}  # noqa: E731
    assert EXPERIMENT_KEYS <= names(models.ExperimentSpec)
    assert JOB_KEYS - {"commands"} <= names(models.Job)
    assert RESOURCE_KEYS <= names(models.ResourceConfig)
    assert ROLE_KEYS <= names(models.RoleConfig)
    for excluded in ("platform_overrides", "model_storage_info"):
        assert excluded not in EXPERIMENT_KEYS | JOB_KEYS | RESOURCE_KEYS | ROLE_KEYS
