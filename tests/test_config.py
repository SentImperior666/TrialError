from pathlib import Path

import pytest

import trialerror.util.config as config_mod
from trialerror.util.config import (
    ConfigError,
    ProgramRootIsHarnessError,
    configured_path_value,
    find_program_root,
    load_config,
    resolve_configured_path,
    resolve_program_id,
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
# L8 part F (F1): the resolver refuses the harness's own repository as a
# fallback program root, unless it was explicitly given.
# ---------------------------------------------------------------------------


def _make_fake_harness_repo(tmp_path: Path, monkeypatch) -> Path:
    """A directory shaped like a checkout of the running ``trialerror``
    package (a ``trialerror/__init__.py`` beside a ``trialerror.toml``),
    without faking the real import -- monkeypatches the module constant
    :func:`find_program_root` reads instead."""
    repo = tmp_path / "fake_checkout"
    (repo / "trialerror").mkdir(parents=True)
    (repo / "trialerror" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "trialerror.toml").write_text(VALID_TOML, encoding="utf-8")
    monkeypatch.setattr(config_mod, "_HARNESS_PACKAGE_PARENT", repo)
    return repo


def test_find_program_root_refuses_the_harness_checkout(tmp_path, monkeypatch):
    repo = _make_fake_harness_repo(tmp_path, monkeypatch)
    with pytest.raises(ProgramRootIsHarnessError) as exc_info:
        find_program_root(repo)
    assert exc_info.value.code == "program_root_is_harness"
    assert "TRIALERROR_PROGRAM_ROOT" in str(exc_info.value)


def test_find_program_root_refuses_a_worktree_of_the_harness(tmp_path, monkeypatch):
    """A git worktree is just another checkout with the same files on disk
    -- the refusal is structural (trialerror/__init__.py beside
    trialerror.toml), not a git-specific check, so a worktree needs no
    special-casing to be caught the same way."""
    repo = tmp_path / "worktree" / "l8-some-branch"
    (repo / "trialerror").mkdir(parents=True)
    (repo / "trialerror" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "trialerror.toml").write_text(VALID_TOML, encoding="utf-8")
    monkeypatch.setattr(config_mod, "_HARNESS_PACKAGE_PARENT", repo)
    with pytest.raises(ProgramRootIsHarnessError):
        find_program_root(repo)


def test_find_program_root_env_override_bypasses_the_refusal(tmp_path, monkeypatch):
    """TRIALERROR_PROGRAM_ROOT was GIVEN, not discovered -- it is never
    subject to the harness refusal, even when it happens to point at the
    harness's own repo (the operator asked for it on purpose)."""
    repo = _make_fake_harness_repo(tmp_path, monkeypatch)
    monkeypatch.setenv("TRIALERROR_PROGRAM_ROOT", str(repo))
    assert find_program_root(repo) == repo


def test_find_program_root_allow_env_bypasses_the_refusal(tmp_path, monkeypatch):
    repo = _make_fake_harness_repo(tmp_path, monkeypatch)
    monkeypatch.setenv("TRIALERROR_ALLOW_HARNESS_PROGRAM_ROOT", "1")
    assert find_program_root(repo) == repo


def test_find_program_root_refuse_harness_false_opts_out(tmp_path, monkeypatch):
    """A caller for which a program root is optional (``probes status``)
    passes ``refuse_harness=False`` and gets the path back instead of an
    exception."""
    repo = _make_fake_harness_repo(tmp_path, monkeypatch)
    assert find_program_root(repo, refuse_harness=False) == repo


def test_find_program_root_does_not_refuse_an_unrelated_program(tmp_path, monkeypatch):
    """A directory with its own trialerror.toml that is NOT the harness
    checkout (no trialerror/__init__.py beside it) is an ordinary program
    root and is returned normally -- this is the common case, exercised by
    every other test in this suite that uses the ``program_root`` fixture."""
    monkeypatch.setattr(config_mod, "_HARNESS_PACKAGE_PARENT", tmp_path / "not_a_real_checkout")
    (tmp_path / "trialerror.toml").write_text(VALID_TOML, encoding="utf-8")
    assert find_program_root(tmp_path) == tmp_path.resolve()


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


# ---------------------------------------------------------------------------
# resolve_program_id (B-2 fix round)
# ---------------------------------------------------------------------------


def test_resolve_program_id_reads_the_declared_id(tmp_path):
    (tmp_path / "trialerror.toml").write_text(VALID_TOML, encoding="utf-8")
    assert resolve_program_id(tmp_path) == "origin-project"


def test_resolve_program_id_falls_back_to_the_root_with_no_toml(tmp_path):
    assert resolve_program_id(tmp_path) == str(tmp_path)


def test_resolve_program_id_falls_back_on_unparseable_toml(tmp_path):
    (tmp_path / "trialerror.toml").write_text("not valid toml [[[", encoding="utf-8")
    assert resolve_program_id(tmp_path) == str(tmp_path)


def test_resolve_program_id_two_different_roots_give_two_different_ids(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    assert resolve_program_id(a) != resolve_program_id(b)
