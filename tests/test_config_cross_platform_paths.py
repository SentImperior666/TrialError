"""``[paths]`` values written for the *other* operating system.

The bug these pin: :class:`pathlib.Path` decides "is this absolute?" against
the host, so ``Path("C:/research/corpus").is_absolute()`` is ``False`` on
Linux. ``resolve_configured_path`` joined that onto the program root and
returned ``<program_root>/C:/research/corpus`` -- no exception, no warning, a
directory happily created in the wrong place. The Windows-era docs and the
``trialerror program init`` template both hand users exactly such values, so
this was reachable by copy-paste, and it fails as a *wrong path* rather than
a missing one, which is the hard kind to debug.

The mirror case is real too: a POSIX ``/srv/corpus`` on Windows has no drive,
so ``PureWindowsPath`` also calls it relative. Both directions are covered,
each skipped on the platform where the value is legitimately absolute.
"""

from __future__ import annotations

import os

import pytest

from trialerror.ingest.pipeline import resolve_ingest_roots
from trialerror.util.config import ConfigError, configured_path_value, foreign_absolute_kind

WINDOWS_VALUES = ["C:/research/corpus", "C:\\research\\corpus", "\\\\fileserver\\share\\corpus"]
POSIX_VALUES = ["/srv/research/corpus", "/home/researcher/corpus"]


# ---------------------------------------------------------------------------
# the detector
# ---------------------------------------------------------------------------
@pytest.mark.skipif(os.name == "nt", reason="these values are genuinely absolute on Windows")
@pytest.mark.parametrize("value", WINDOWS_VALUES)
def test_windows_absolute_values_are_flagged_on_posix(value):
    assert foreign_absolute_kind(value) == "Windows"


@pytest.mark.skipif(os.name != "nt", reason="these values are genuinely absolute on POSIX")
@pytest.mark.parametrize("value", POSIX_VALUES)
def test_posix_absolute_values_are_flagged_on_windows(value):
    assert foreign_absolute_kind(value) == "POSIX"


@pytest.mark.parametrize("value", ["corpus", "raw", "sub/dir", "archive"])
def test_relative_values_are_never_flagged(value):
    """Relative paths are the common case and must stay untouched on both
    platforms -- the guard is about absoluteness, not about slashes."""
    assert foreign_absolute_kind(value) is None


def test_native_absolute_value_is_not_flagged(tmp_path):
    """Whatever `tmp_path` is, it is absolute *here*, which is the whole
    point: the guard must not fire on legitimate absolute overrides."""
    assert foreign_absolute_kind(str(tmp_path)) is None


# ---------------------------------------------------------------------------
# the two call sites
# ---------------------------------------------------------------------------
@pytest.mark.skipif(os.name == "nt", reason="C:/... is a real absolute path on Windows")
def test_resolve_configured_path_refuses_a_windows_path_on_posix(tmp_path):
    config = {"paths": {"archive_dir": "C:/research/archive"}}
    with pytest.raises(ConfigError) as exc:
        from trialerror.util.config import resolve_configured_path

        resolve_configured_path(tmp_path, config, "archive_dir", "archive")

    message = str(exc.value)
    assert "archive_dir" in message
    assert "Windows" in message
    # The old behaviour was a silently-joined path; make sure we are not
    # merely reporting the mangled result back to the user.
    assert str(tmp_path / "C:") not in message


@pytest.mark.skipif(os.name == "nt", reason="C:/... is a real absolute path on Windows")
def test_resolve_ingest_roots_refuses_a_windows_root_on_posix(tmp_path):
    config = {"paths": {"ingest_roots": ["raw", "C:/research/inbox"]}}
    with pytest.raises(ConfigError) as exc:
        resolve_ingest_roots(tmp_path, config)
    assert "ingest_roots" in str(exc.value)


def test_native_absolute_override_still_resolves(tmp_path):
    """Regression guard: the new check must not break the documented ability
    to point a knob at an absolute location outside the program root."""
    from trialerror.util.config import resolve_configured_path

    external = tmp_path / "external-archive"
    config = {"paths": {"archive_dir": str(external)}}
    assert resolve_configured_path(tmp_path / "program", config, "archive_dir", "archive") == external


def test_relative_override_still_joins_onto_the_program_root(tmp_path):
    from trialerror.util.config import resolve_configured_path

    config = {"paths": {"archive_dir": "cold-storage"}}
    resolved = resolve_configured_path(tmp_path, config, "archive_dir", "archive")
    assert resolved == tmp_path / "cold-storage"


def test_configured_path_value_still_returns_the_raw_string(tmp_path):
    """The *value* helper deliberately does no validation -- callers persist
    its output verbatim. Only the resolving helper judges the platform."""
    config = {"paths": {"archive_dir": "C:/research/archive"}}
    assert configured_path_value(config, "archive_dir", "archive") == "C:/research/archive"
