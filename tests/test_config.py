import pytest

from trialerror.util.config import (
    ConfigError,
    configured_path_value,
    find_program_root,
    load_config,
    resolve_configured_path,
)

VALID_TOML = """
[program]
id = "origin-project"

[id_prefixes]
ruling = "C"
critic_review = "CR"

[models]
ideation = "top"
mechanical = "small"

[license]
posture = "internal-research"
"""


def test_load_config_parses_valid_toml(tmp_path):
    path = tmp_path / "trialerror.toml"
    path.write_text(VALID_TOML, encoding="utf-8")

    cfg = load_config(path)

    assert cfg.program_id == "origin-project"
    assert cfg.id_prefixes == {"ruling": "C", "critic_review": "CR"}
    assert cfg.models == {"ideation": "top", "mechanical": "small"}
    assert cfg.license_posture == {"posture": "internal-research"}
    assert cfg.paths == {}


def test_load_config_missing_file(tmp_path):
    with pytest.raises(ConfigError):
        load_config(tmp_path / "does_not_exist.toml")


def test_load_config_missing_program_table(tmp_path):
    path = tmp_path / "trialerror.toml"
    path.write_text("[models]\nideation = \"top\"\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path)


def test_load_config_missing_program_id(tmp_path):
    path = tmp_path / "trialerror.toml"
    path.write_text("[program]\nname = \"no id field\"\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path)


def test_load_config_invalid_toml_syntax(tmp_path):
    path = tmp_path / "trialerror.toml"
    path.write_text("[program\nid = broken", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path)


def test_find_program_root_walks_up_from_nested_dir(tmp_path):
    (tmp_path / "trialerror.toml").write_text(VALID_TOML, encoding="utf-8")
    nested = tmp_path / "a" / "b" / "c"
    nested.mkdir(parents=True)

    found = find_program_root(nested)

    assert found == tmp_path.resolve()


def test_find_program_root_returns_none_when_absent(tmp_path):
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    assert find_program_root(nested) is None


# ---------------------------------------------------------------------------
# resolve_configured_path / configured_path_value -- the shared [paths]
# knob-resolution helpers (the import-design notes (internal, not in this export) Sec 5, C-0067(c)(i))
# ---------------------------------------------------------------------------


def test_configured_path_value_default_when_no_paths_table(tmp_path):
    assert configured_path_value(None, "archive_dir", "archive") == "archive"
    assert configured_path_value({}, "archive_dir", "archive") == "archive"
    assert configured_path_value({"paths": {}}, "archive_dir", "archive") == "archive"


def test_configured_path_value_returns_configured_string_unresolved():
    config = {"paths": {"archive_dir": "C:/external/archive"}}
    assert configured_path_value(config, "archive_dir", "archive") == "C:/external/archive"


def test_configured_path_value_ignores_unrelated_keys():
    config = {"paths": {"handoffs_dir": "elsewhere"}}
    assert configured_path_value(config, "archive_dir", "archive") == "archive"


def test_resolve_configured_path_default_joins_onto_program_root(tmp_path):
    assert resolve_configured_path(tmp_path, None, "memory_dir", "memory") == tmp_path / "memory"


def test_resolve_configured_path_relative_override_joins_onto_program_root(tmp_path):
    config = {"paths": {"memory_dir": "shared/memory"}}
    assert resolve_configured_path(tmp_path, config, "memory_dir", "memory") == tmp_path / "shared" / "memory"


def test_resolve_configured_path_absolute_override_replaces_program_root(tmp_path):
    external = tmp_path / "elsewhere" / "memory"
    config = {"paths": {"memory_dir": str(external)}}
    assert resolve_configured_path(tmp_path / "program", config, "memory_dir", "memory") == external
