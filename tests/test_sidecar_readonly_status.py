"""Lane F-1b stage 3: the doctor check really is read-only, the heartbeat's
age has a reader, and "not probed" no longer reads as "not configured".

``docs/reviews/VERIFY_f1b-sidecar.md`` V-4 and V-6:

- **V-4** -- ``restart_if_dead=False`` suppressed the spawn, not the WRITE:
  every ``sidecar_status`` call rewrote ``run/sidecars/<name>.json``, so a
  ``doctor --only sidecar_alive`` run moved the heartbeat it reports. And the
  heartbeat's age had no consumer at all.
- **V-6** -- a dead or never-started sidecar reported
  ``health: {"configured": false, "ok": null}``, which the guide teaches as
  "this sidecar has no health_url", for a sidecar whose config carries one.

Real child processes, a fake health seam, no socket.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from trialerror.sidecar import checks as sidecar_checks
from trialerror.sidecar.supervisor import (
    SIDECARS_TABLE,
    heartbeat_age_s,
    read_state,
    sidecar_state_path,
    sidecar_status,
    start_sidecar,
    stop_sidecar,
)
from trialerror.util.doctor import DoctorContext

SLEEPER = "import time\nwhile True:\n    time.sleep(0.05)\n"
HEALTH_URL = "http://127.0.0.1:1/health"
_MINIMAL_CONFIG = '[program]\nid = "PROG-sidecar-readonly"\n\n'


def _config(*, health_url: str | None = HEALTH_URL, restart: str = "always") -> dict:
    table: dict = {"command": [sys.executable, "-c", SLEEPER], "restart": restart}
    if health_url:
        table["health_url"] = health_url
    return {SIDECARS_TABLE: {"embed": table}}


class FakeHealth:
    def __init__(self, status: int = 200):
        self.status = status
        self.calls: list[tuple[str, float]] = []

    def get(self, url: str, timeout_s: float) -> int:
        self.calls.append((url, timeout_s))
        return self.status


@pytest.fixture
def program_root(tmp_path: Path) -> Path:
    (tmp_path / "trialerror.toml").write_text(_MINIMAL_CONFIG, encoding="utf-8")
    return tmp_path


@pytest.fixture(autouse=True)
def _stop_everything(program_root: Path):
    yield
    try:
        stop_sidecar(program_root, "embed", config=_config(), grace_s=2.0)
    except Exception:  # noqa: BLE001 - nothing to stop
        pass


def _write_config(program_root: Path, toml_text: str) -> None:
    (program_root / "trialerror.toml").write_text(_MINIMAL_CONFIG + toml_text, encoding="utf-8")


def _sidecar_toml(*, health_url: str | None = HEALTH_URL) -> str:
    lines = [
        "[sidecars.embed]",
        f'command = [{sys.executable!r}, "-c", {SLEEPER!r}]',
        'restart = "always"',
    ]
    if health_url:
        lines.append(f'health_url = "{health_url}"')
    return "\n".join(lines).replace("'", '"') + "\n"


# ---------------------------------------------------------------------------
# V-4: no write, and an age somebody reads
# ---------------------------------------------------------------------------


def test_refresh_heartbeat_false_leaves_the_state_file_untouched(program_root):
    config = _config()
    start_sidecar(program_root, "embed", config=config, _http=FakeHealth())
    path = sidecar_state_path(program_root, "embed", config)
    before = path.read_text(encoding="utf-8")
    mtime = path.stat().st_mtime_ns
    time.sleep(0.05)

    status = sidecar_status(
        program_root, "embed", config=config, refresh_heartbeat=False, _http=FakeHealth()
    )
    assert status["running"] is True and status["state"] == "healthy"
    assert path.read_text(encoding="utf-8") == before, "a read-only status wrote the state file"
    assert path.stat().st_mtime_ns == mtime


def test_the_default_status_still_refreshes_the_heartbeat(program_root):
    """The switch must not turn the supervisor's own poll into a no-op."""
    config = _config()
    start_sidecar(program_root, "embed", config=config, _http=FakeHealth())
    first = read_state(program_root, "embed", config)["heartbeat_at"]
    time.sleep(0.02)

    sidecar_status(program_root, "embed", config=config, _http=FakeHealth())
    assert read_state(program_root, "embed", config)["heartbeat_at"] != first


def test_status_reports_the_heartbeats_age(program_root):
    config = _config()
    start_sidecar(program_root, "embed", config=config, _http=FakeHealth())
    status = sidecar_status(
        program_root, "embed", config=config, refresh_heartbeat=False, _http=FakeHealth()
    )
    assert isinstance(status["heartbeat_age_s"], float)
    assert status["heartbeat_age_s"] >= 0.0


def test_the_age_of_a_planted_old_heartbeat_is_hours(program_root):
    assert heartbeat_age_s({"heartbeat_at": "2020-01-01T00:00:00Z"}) > 3600.0
    assert heartbeat_age_s({"heartbeat_at": "not a timestamp"}) is None
    assert heartbeat_age_s({}) is None
    assert heartbeat_age_s(None) is None


def test_the_doctor_check_writes_nothing(program_root):
    """The V-4 repro, as a test: through the check itself, not the CLI."""
    config = _config()
    start_sidecar(program_root, "embed", config=config, _http=FakeHealth())
    _write_config(program_root, _sidecar_toml())
    path = sidecar_state_path(program_root, "embed", config)
    before = path.read_text(encoding="utf-8")
    mtime = path.stat().st_mtime_ns
    time.sleep(0.05)

    result = sidecar_checks.check_sidecar_alive(DoctorContext(program_root=program_root))
    assert result.status in {"pass", "warn"}
    assert path.read_text(encoding="utf-8") == before, "doctor moved the heartbeat it reports"
    assert path.stat().st_mtime_ns == mtime


def test_the_doctor_detail_carries_the_heartbeat_age(program_root):
    start_sidecar(program_root, "embed", config=_config(), _http=FakeHealth())
    _write_config(program_root, _sidecar_toml())

    result = sidecar_checks.check_sidecar_alive(DoctorContext(program_root=program_root))
    row = result.details["sidecars"]["embed"]
    assert "heartbeat_age_s" in row
    assert row["heartbeat_age_s"] is None or row["heartbeat_age_s"] >= 0.0


# ---------------------------------------------------------------------------
# V-6: "not probed" is not "not configured"
# ---------------------------------------------------------------------------


def test_a_never_started_sidecar_with_a_health_url_says_it_has_one(program_root):
    status = sidecar_status(program_root, "embed", config=_config(), restart_if_dead=False)
    assert status["state"] == "never_started"
    assert status["health"] == {
        "configured": True, "ok": None, "url": HEALTH_URL, "skipped": "never started",
    }


def test_a_dead_sidecar_with_a_health_url_says_it_has_one(program_root):
    config = _config(restart="never")
    start_sidecar(program_root, "embed", config=config, _http=FakeHealth())
    stop_sidecar(program_root, "embed", config=config, grace_s=2.0)
    # A state file for a pid that is gone -- the "dead" branch, not "never
    # started": stop removes the file, so plant it back from the log path.
    start_sidecar(program_root, "embed", config=config, _http=FakeHealth())
    state = read_state(program_root, "embed", config)
    stop_sidecar(program_root, "embed", config=config, grace_s=2.0)
    path = sidecar_state_path(program_root, "embed", config)
    path.write_text(__import__("json").dumps(state), encoding="utf-8")

    status = sidecar_status(program_root, "embed", config=config, restart_if_dead=False)
    assert status["state"] == "dead"
    assert status["health"]["configured"] is True
    assert status["health"]["ok"] is None
    assert status["health"]["skipped"] == "process not running"
    assert status["health"]["url"] == HEALTH_URL


def test_a_sidecar_with_no_health_url_still_reports_unconfigured(program_root):
    """The distinction only works if the other side keeps its meaning."""
    config = _config(health_url=None)
    status = sidecar_status(program_root, "embed", config=config, restart_if_dead=False)
    assert status["health"] == {"configured": False, "ok": None}


def test_the_doctor_detail_distinguishes_the_two(program_root):
    _write_config(program_root, _sidecar_toml())
    result = sidecar_checks.check_sidecar_alive(DoctorContext(program_root=program_root))
    row = result.details["sidecars"]["embed"]
    assert result.status == "warn"
    assert row["health"]["configured"] is True and row["health"]["skipped"]

    _write_config(program_root, _sidecar_toml(health_url=None))
    result = sidecar_checks.check_sidecar_alive(DoctorContext(program_root=program_root))
    assert result.details["sidecars"]["embed"]["health"] == {"configured": False, "ok": None}
