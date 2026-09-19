"""Lane F-1b item 4: the sidecar supervisor and its CLI group.

Everything here runs against a REAL child process -- a few lines of Python that
sleeps, or exits immediately, or ignores SIGTERM -- and a FAKE health endpoint.
That combination is deliberate: process lifecycle is the one thing a mock
cannot tell the truth about (a pid either exists or it does not), while a
socket is the one thing this repo's tests must never open.

Nothing here starts a llama-server, loads a GGUF, or touches the live sidecar
this container happens to be running.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

from trialerror.sidecar import supervisor
from trialerror.sidecar.supervisor import (
    SIDECARS_TABLE,
    SidecarConfigError,
    health_probe,
    load_sidecar_spec,
    read_state,
    sidecar_names,
    sidecar_state_path,
    sidecar_status,
    start_sidecar,
    stop_sidecar,
)

#: A child that stays up until it is signalled, with no imports that could be
#: slow. ``sys.executable`` runs it, so there is no dependency on a shell.
SLEEPER = "import time\nwhile True:\n    time.sleep(0.05)\n"

#: ``sys.executable`` spelled for a TOML BASIC string. A native Windows path
#: interpolated verbatim is not the path: every backslash starts an escape
#: sequence, so ``"C:\\Users\\..."`` is a TOMLDecodeError and the config the
#: test just wrote reads as empty. Forward slashes launch the same
#: interpreter on both platforms. Only for text that goes INTO a config
#: file; a command list built in Python keeps plain ``sys.executable``.
EXECUTABLE_FOR_TOML = Path(sys.executable).as_posix()

#: A child that exits at once -- what "the process died" looks like.
QUITTER = "raise SystemExit(3)\n"

#: A child that ignores SIGTERM, so the SIGKILL escalation has something to
#: escalate against.
STUBBORN = (
    "import signal, time\n"
    "signal.signal(signal.SIGTERM, lambda *a: None)\n"
    "while True:\n"
    "    time.sleep(0.05)\n"
)


class FakeHealth:
    """``get(url, timeout_s) -> status``, recording every probe. ``status`` is
    settable per test; ``raise_with`` makes the endpoint unreachable."""

    def __init__(self, status: int = 200, raise_with: Exception | None = None):
        self.status = status
        self.raise_with = raise_with
        self.calls: list[tuple[str, float]] = []

    def get(self, url: str, timeout_s: float) -> int:
        self.calls.append((url, timeout_s))
        if self.raise_with is not None:
            raise self.raise_with
        return self.status


def _config(
    *,
    code: str = SLEEPER,
    restart: str = "always",
    health_url: str | None = "http://127.0.0.1:1/health",
    name: str = "embed",
    env: dict | None = None,
    cwd: str | None = None,
) -> dict:
    table = {
        "command": [sys.executable, "-c", code],
        "restart": restart,
    }
    if health_url:
        table["health_url"] = health_url
    if env:
        table["env"] = env
    if cwd:
        table["cwd"] = cwd
    return {SIDECARS_TABLE: {name: table}}


#: The smallest ``trialerror.toml`` ``load_config`` accepts -- every test that
#: goes through the CLI needs a loadable one, and the sidecar tables are
#: appended to it.
_MINIMAL_CONFIG = '[program]\nid = "PROG-sidecar-test"\n\n'


@pytest.fixture
def program_root(tmp_path: Path) -> Path:
    (tmp_path / "trialerror.toml").write_text(_MINIMAL_CONFIG, encoding="utf-8")
    return tmp_path


@pytest.fixture(autouse=True)
def _reap_everything(program_root: Path):
    """Whatever a test started, stop -- a leaked child outliving the suite is
    the one failure mode a process-spawning test file can inflict on a
    machine."""
    yield
    for name in list(supervisor._OWN_CHILDREN):
        child = supervisor._OWN_CHILDREN.pop(name, None)
        if child is None:
            continue
        try:
            child.kill()
            child.wait(timeout=10)
        except Exception:  # noqa: BLE001 - already gone
            pass


def _wait_until(predicate, *, timeout_s: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------


def test_the_spec_comes_out_of_the_table():
    spec = load_sidecar_spec(
        _config(env={"LD_LIBRARY_PATH": "/opt/somewhere"}, cwd="/tmp"), "embed"
    )
    assert spec.name == "embed"
    assert spec.command[0] == sys.executable
    assert spec.env == {"LD_LIBRARY_PATH": "/opt/somewhere"}
    assert spec.cwd == "/tmp"
    assert spec.restart == "always"
    assert spec.health_url.endswith("/health")


def test_an_absent_table_is_refused_naming_the_configured_ones():
    with pytest.raises(SidecarConfigError) as exc:
        load_sidecar_spec(_config(), "nope")
    assert "sidecars.nope" in str(exc.value) and "embed" in str(exc.value)


def test_a_command_that_is_a_string_is_refused():
    """A string would have to be split by a shell, and a config that reaches a
    shell can reach a pipeline."""
    with pytest.raises(SidecarConfigError) as exc:
        load_sidecar_spec({SIDECARS_TABLE: {"embed": {"command": "server --port 1"}}}, "embed")
    assert "list of strings" in str(exc.value)


@pytest.mark.parametrize("command", [[], ["ok", ""], ["ok", 3], {"a": "b"}])
def test_a_malformed_command_is_refused(command):
    with pytest.raises(SidecarConfigError):
        load_sidecar_spec({SIDECARS_TABLE: {"embed": {"command": command}}}, "embed")


def test_an_unknown_restart_policy_is_refused():
    with pytest.raises(SidecarConfigError) as exc:
        load_sidecar_spec({SIDECARS_TABLE: {"embed": {"command": ["x"], "restart": "maybe"}}}, "embed")
    assert "restart" in str(exc.value)


def test_the_default_restart_policy_is_never():
    spec = load_sidecar_spec({SIDECARS_TABLE: {"embed": {"command": ["x"]}}}, "embed")
    assert spec.restart == "never", "restarting a process is a side effect nobody asked for"


def test_sidecar_names_lists_only_tables():
    config = {SIDECARS_TABLE: {"b": {"command": ["x"]}, "a": {"command": ["x"]}, "junk": 3}}
    assert sidecar_names(config) == ["a", "b"]
    assert sidecar_names({}) == []


def test_the_spec_report_never_carries_an_env_value():
    """A sidecar's env can carry a token; the state file and every envelope
    report the KEYS."""
    spec = load_sidecar_spec(_config(env={"SECRET_TOKEN": "hunter2"}), "embed")
    rendered = json.dumps(spec.to_dict())
    assert "SECRET_TOKEN" in rendered and "hunter2" not in rendered


# ---------------------------------------------------------------------------
# health probe
# ---------------------------------------------------------------------------


def test_no_health_url_is_reported_as_unconfigured_not_as_healthy():
    assert health_probe(None) == {"configured": False, "ok": None}


def test_a_200_is_healthy_and_a_503_is_not():
    fake = FakeHealth(status=200)
    assert health_probe("http://x/health", _http=fake)["ok"] is True
    assert health_probe("http://x/health", _http=FakeHealth(status=503))["ok"] is False


def test_an_unreachable_endpoint_is_a_reading_not_an_exception():
    probe = health_probe("http://x/health", _http=FakeHealth(raise_with=OSError("refused")))
    assert probe["ok"] is False and "refused" in probe["error"]


# ---------------------------------------------------------------------------
# start / status / stop
# ---------------------------------------------------------------------------


def test_start_spawns_detached_and_records_the_state(program_root):
    config = _config()
    health = FakeHealth()
    result = start_sidecar(program_root, "embed", config=config, _http=health)

    assert result["started"] is True and result["already_running"] is False
    pid = result["pid"]
    assert pid and supervisor._process_alive(pid)

    state = read_state(program_root, "embed", config)
    assert state["pid"] == pid
    assert state["argv"][0] == sys.executable
    assert state["restart"] == "always"
    assert Path(state["log_path"]).parent == sidecar_state_path(program_root, "embed", config).parent
    assert state["started_at"] and state["heartbeat_at"]
    assert health.calls, "the health endpoint was probed"

    if sys.platform != "win32":
        # Detached: its own session, so the CLI's Ctrl-C does not reach it.
        assert os.getsid(pid) == pid


def test_the_state_file_lives_under_the_programs_run_dir(program_root):
    start_sidecar(program_root, "embed", config=_config(), _http=FakeHealth())
    path = sidecar_state_path(program_root, "embed", _config())
    assert path.is_file()
    assert path.parent == program_root / "run" / "sidecars"


def test_a_configured_run_dir_is_honoured(program_root):
    config = {**_config(), "paths": {"run_dir": "var/live"}}
    start_sidecar(program_root, "embed", config=config, _http=FakeHealth())
    assert (program_root / "var" / "live" / "sidecars" / "embed.json").is_file()


def test_the_configured_env_reaches_the_child(program_root, tmp_path):
    marker = tmp_path / "env.txt"
    code = f"import os; open({str(marker)!r}, 'w').write(os.environ.get('LD_LIBRARY_PATH', 'MISSING'))"
    config = _config(code=code, env={"LD_LIBRARY_PATH": "/opt/a-vendored-runtime"}, health_url=None)
    start_sidecar(program_root, "embed", config=config)
    assert _wait_until(marker.is_file)
    assert marker.read_text(encoding="utf-8") == "/opt/a-vendored-runtime"


def test_the_childs_output_lands_in_the_log(program_root):
    config = _config(code="print('hello from the sidecar', flush=True)", health_url=None)
    result = start_sidecar(program_root, "embed", config=config)
    log = Path(result["log_path"])
    assert _wait_until(lambda: log.is_file() and "hello from the sidecar" in log.read_text(encoding="utf-8"))


def test_a_second_start_does_not_spawn_a_second_process(program_root):
    """Two llama-servers on one port is a 4 GB mistake whose only symptom is a
    bind failure in a log file nobody reads."""
    config = _config()
    first = start_sidecar(program_root, "embed", config=config, _http=FakeHealth())
    second = start_sidecar(program_root, "embed", config=config, _http=FakeHealth())
    assert second["started"] is False and second["already_running"] is True
    assert second["pid"] == first["pid"]


def test_status_of_a_never_started_sidecar_says_so(program_root):
    status = sidecar_status(program_root, "embed", config=_config(), _http=FakeHealth())
    assert status["running"] is False and status["state"] == "never_started"
    assert status["restarted"] is False


def test_status_refreshes_the_heartbeat_of_a_healthy_process(program_root):
    config = _config()
    start_sidecar(program_root, "embed", config=config, _http=FakeHealth())
    first = read_state(program_root, "embed", config)["heartbeat_at"]
    time.sleep(0.01)
    status = sidecar_status(program_root, "embed", config=config, _http=FakeHealth())
    assert status["state"] == "healthy"
    assert read_state(program_root, "embed", config)["heartbeat_at"] >= first


def test_a_process_that_is_up_but_unhealthy_does_not_refresh_the_heartbeat(program_root):
    """Otherwise the age of the heartbeat would stop being evidence of
    anything."""
    config = _config()
    start_sidecar(program_root, "embed", config=config, _http=FakeHealth())
    before = read_state(program_root, "embed", config)["heartbeat_at"]
    time.sleep(0.01)
    status = sidecar_status(program_root, "embed", config=config, _http=FakeHealth(status=503))
    assert status["running"] is True and status["state"] == "unhealthy"
    assert read_state(program_root, "embed", config)["heartbeat_at"] == before
    assert read_state(program_root, "embed", config)["health"]["ok"] is False


def test_a_dead_process_is_restarted_when_restart_is_always(program_root):
    config = _config(code=QUITTER)
    first = start_sidecar(program_root, "embed", config=config, _http=FakeHealth())
    assert _wait_until(lambda: not supervisor._process_alive(first["pid"]))

    status = sidecar_status(program_root, "embed", config=config, _http=FakeHealth())
    assert status["restarted"] is True and status["state"] == "restarted"
    assert status["previous_pid"] == first["pid"]
    assert status["pid"] != first["pid"]


def test_a_dead_process_is_left_dead_when_restart_is_never(program_root):
    config = _config(code=QUITTER, restart="never")
    first = start_sidecar(program_root, "embed", config=config, _http=FakeHealth())
    assert _wait_until(lambda: not supervisor._process_alive(first["pid"]))

    status = sidecar_status(program_root, "embed", config=config, _http=FakeHealth())
    assert status["restarted"] is False and status["state"] == "dead"
    assert status["pid"] == first["pid"]


def test_restart_if_dead_false_never_restarts_even_on_always(program_root):
    """What the doctor check uses: a doctor run that silently restarted
    processes would be a doctor run nobody could use to find out what was
    wrong."""
    config = _config(code=QUITTER)
    first = start_sidecar(program_root, "embed", config=config, _http=FakeHealth())
    assert _wait_until(lambda: not supervisor._process_alive(first["pid"]))

    status = sidecar_status(
        program_root, "embed", config=config, restart_if_dead=False, _http=FakeHealth()
    )
    assert status["restarted"] is False and status["state"] == "dead"
    assert read_state(program_root, "embed", config) is not None, "a read-only check wrote nothing away"


def test_stop_terminates_the_process_and_clears_the_state(program_root):
    config = _config()
    result = start_sidecar(program_root, "embed", config=config, _http=FakeHealth())
    pid = result["pid"]

    stopped = stop_sidecar(program_root, "embed", config=config)
    assert stopped["stopped"] is True and stopped["was_running"] is True
    assert "SIGTERM" in stopped["signals"]
    assert not supervisor._process_alive(pid)
    assert read_state(program_root, "embed", config) is None


@pytest.mark.skipif(sys.platform == "win32", reason="SIGTERM semantics are POSIX")
def test_stop_escalates_to_sigkill_for_a_process_that_ignores_sigterm(program_root):
    config = _config(code=STUBBORN)
    result = start_sidecar(program_root, "embed", config=config, _http=FakeHealth())
    pid = result["pid"]
    # give the child time to install its handler before signalling it
    time.sleep(0.3)

    stopped = stop_sidecar(program_root, "embed", config=config, grace_s=0.5)
    assert stopped["stopped"] is True
    assert stopped["signals"] == ["SIGTERM", "SIGKILL"]
    assert not supervisor._process_alive(pid)


@pytest.mark.skipif(sys.platform == "win32", reason="zombies are a POSIX concept")
def test_stop_leaves_no_zombie(program_root):
    """A detached child of a LIVING parent (this interpreter) stays a zombie
    until someone waits on it."""
    config = _config()
    result = start_sidecar(program_root, "embed", config=config, _http=FakeHealth())
    pid = result["pid"]
    stop_sidecar(program_root, "embed", config=config)

    assert str(pid) not in supervisor._OWN_CHILDREN
    state_line = ""
    stat = Path(f"/proc/{pid}/stat")
    if stat.exists():  # pragma: no cover - only if the kernel has not reaped it yet
        state_line = stat.read_text(encoding="utf-8", errors="replace")
    assert " Z " not in state_line, f"pid {pid} is a zombie: {state_line!r}"


def test_stop_of_a_never_started_sidecar_is_not_an_error(program_root):
    result = stop_sidecar(program_root, "embed", config=_config())
    assert result["stopped"] is False and result["was_running"] is False
    assert result["state"] == "never_started"


def test_stop_of_an_already_dead_process_clears_the_stale_state(program_root):
    config = _config(code=QUITTER)
    first = start_sidecar(program_root, "embed", config=config, _http=FakeHealth())
    assert _wait_until(lambda: not supervisor._process_alive(first["pid"]))

    result = stop_sidecar(program_root, "embed", config=config)
    assert result["state"] == "already_gone" and result["was_running"] is False
    assert read_state(program_root, "embed", config) is None


def test_start_after_a_stale_state_file_spawns_a_fresh_process(program_root):
    config = _config()
    path = sidecar_state_path(program_root, "embed", config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"name": "embed", "pid": 999_999_999}), encoding="utf-8")

    result = start_sidecar(program_root, "embed", config=config, _http=FakeHealth())
    assert result["started"] is True
    assert result["pid"] != 999_999_999


def test_an_unreadable_state_file_is_treated_as_absent(program_root):
    config = _config()
    path = sidecar_state_path(program_root, "embed", config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ truncated", encoding="utf-8")
    assert read_state(program_root, "embed", config) is None
    assert start_sidecar(program_root, "embed", config=config, _http=FakeHealth())["started"] is True


def test_a_command_that_cannot_be_executed_reports_rather_than_spawning(program_root):
    config = {SIDECARS_TABLE: {"embed": {"command": ["/nonexistent/binary", "--go"]}}}
    with pytest.raises(FileNotFoundError):
        start_sidecar(program_root, "embed", config=config)
    # nothing was recorded for a process that never existed
    assert read_state(program_root, "embed", config) is None


# ---------------------------------------------------------------------------
# the doctor check
# ---------------------------------------------------------------------------


def _doctor(program_root: Path):
    from trialerror.sidecar.checks import check_sidecar_alive
    from trialerror.util.doctor import DoctorContext

    return check_sidecar_alive(DoctorContext(repo_root=program_root, program_root=program_root))


def _write_config(program_root: Path, toml_text: str) -> None:
    (program_root / "trialerror.toml").write_text(_MINIMAL_CONFIG + toml_text, encoding="utf-8")


def test_the_doctor_check_skips_a_program_with_no_sidecars(program_root):
    result = _doctor(program_root)
    assert result.status == "skip" and result.category == "sidecar"


def test_the_doctor_check_skips_without_a_program_root():
    from trialerror.sidecar.checks import check_sidecar_alive
    from trialerror.util.doctor import DoctorContext

    assert check_sidecar_alive(DoctorContext()).status == "skip"


def test_the_doctor_check_warns_for_a_configured_sidecar_that_was_never_started(program_root):
    _write_config(
        program_root,
        '[sidecars.embed]\ncommand = ["/nonexistent/server"]\nhealth_url = "http://127.0.0.1:1/health"\n',
    )
    result = _doctor(program_root)
    assert result.status == "warn"
    assert "sidecar start" in result.message
    assert result.details["sidecars"]["embed"]["state"] == "never_started"


def test_the_doctor_check_warns_for_a_misconfigured_table(program_root):
    _write_config(program_root, '[sidecars.embed]\ncommand = "server --port 1"\n')
    result = _doctor(program_root)
    assert result.status == "warn"
    assert result.details["sidecars"]["embed"]["state"] == "misconfigured"


def test_the_doctor_check_passes_for_a_live_sidecar_with_no_health_url(program_root, monkeypatch):
    _write_config(
        program_root,
        f'[sidecars.embed]\ncommand = ["{EXECUTABLE_FOR_TOML}", "-c", "import time\\nwhile True: time.sleep(0.05)"]\n',
    )
    config = {SIDECARS_TABLE: {"embed": {"command": [sys.executable, "-c", SLEEPER]}}}
    start_sidecar(program_root, "embed", config=config)
    result = _doctor(program_root)
    assert result.status == "pass", result.message
    assert result.details["sidecars"]["embed"]["health"]["configured"] is False


def test_the_doctor_check_warns_when_the_health_url_does_not_answer(program_root):
    """An unroutable port (TEST-NET-1, RFC 5737) rather than a fake: the check
    itself owns the probe, and this asserts the real one reports a failure
    rather than raising. No service of this container's is contacted."""
    _write_config(
        program_root,
        '[sidecars.embed]\ncommand = ["/nonexistent/server"]\n'
        'health_url = "http://192.0.2.1:9/health"\nhealth_timeout_s = 0.05\n',
    )
    config = {
        SIDECARS_TABLE: {
            "embed": {
                "command": [sys.executable, "-c", SLEEPER],
                "health_url": "http://192.0.2.1:9/health",
                "health_timeout_s": 0.05,
            }
        }
    }
    start_sidecar(program_root, "embed", config=config)
    result = _doctor(program_root)
    assert result.status == "warn"
    assert result.details["sidecars"]["embed"]["running"] is True
    assert result.details["sidecars"]["embed"]["health"]["ok"] is False


def test_the_doctor_check_is_registered_under_its_own_category():
    from trialerror.util.doctor import discover_and_register_checks, registered_checks

    discover_and_register_checks()
    registry = registered_checks()
    assert "sidecar_alive" in registry
    assert registry["sidecar_alive"][0] == "sidecar"


# ---------------------------------------------------------------------------
# the CLI group
# ---------------------------------------------------------------------------


def _cli(program_root: Path, *argv: str) -> dict:
    """One CLI invocation in this process, returning the parsed envelope."""
    from trialerror.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(["sidecar", *argv, "--program-root", str(program_root)])
    return args.handler(args)


def test_the_group_is_auto_discovered():
    from trialerror.cli import discover_groups

    assert "sidecar" in {getattr(m, "GROUP_NAME", None) for m in discover_groups()}


def test_the_cli_starts_status_and_stops(program_root):
    _write_config(
        program_root,
        f'[sidecars.embed]\ncommand = ["{EXECUTABLE_FOR_TOML}", "-c", "import time\\nwhile True: time.sleep(0.05)"]\n'
        'restart = "always"\n',
    )
    started = _cli(program_root, "start", "embed")
    assert started["ok"] is True and started["result"]["started"] is True
    pid = started["result"]["pid"]

    status = _cli(program_root, "status")
    assert status["result"]["running"] == ["embed"]
    assert status["result"]["sidecars"]["embed"]["pid"] == pid

    stopped = _cli(program_root, "stop", "embed")
    assert stopped["result"]["stopped"] is True
    assert not supervisor._process_alive(pid)


def test_the_cli_accepts_the_name_as_a_flag_too(program_root):
    _write_config(
        program_root,
        f'[sidecars.embed]\ncommand = ["{EXECUTABLE_FOR_TOML}", "-c", "import time\\nwhile True: time.sleep(0.05)"]\n',
    )
    started = _cli(program_root, "start", "--name", "embed", "--cmd-from-config")
    assert started["ok"] is True and started["result"]["name"] == "embed"


def test_the_cli_refuses_an_unconfigured_name(program_root):
    _write_config(program_root, '[sidecars.embed]\ncommand = ["/x"]\n')
    result = _cli(program_root, "start", "nope")
    assert result["ok"] is False and result["error"]["code"] == "not_configured"


def test_the_cli_reports_a_spawn_failure_as_an_envelope(program_root):
    _write_config(program_root, '[sidecars.embed]\ncommand = ["/nonexistent/binary"]\n')
    result = _cli(program_root, "start", "embed")
    assert result["ok"] is False and result["error"]["code"] == "spawn_failed"
    assert "FileNotFoundError" in result["error"]["message"]


def test_the_cli_asks_which_sidecar_when_none_is_named(program_root):
    _write_config(program_root, '[sidecars.embed]\ncommand = ["/x"]\n')
    result = _cli(program_root, "start")
    assert result["ok"] is False and result["error"]["code"] == "no_sidecar_named"
    assert "embed" in result["error"]["message"]


def test_the_cli_status_no_restart_flag_reports_without_restarting(program_root):
    _write_config(
        program_root,
        f'[sidecars.embed]\ncommand = ["{EXECUTABLE_FOR_TOML}", "-c", "raise SystemExit(3)"]\n'
        'restart = "always"\n',
    )
    started = _cli(program_root, "start", "embed")
    assert _wait_until(lambda: not supervisor._process_alive(started["result"]["pid"]))

    reported = _cli(program_root, "status", "--no-restart")
    assert reported["result"]["restarted"] == []
    assert reported["result"]["sidecars"]["embed"]["state"] == "dead"

    restarted = _cli(program_root, "status")
    assert restarted["result"]["restarted"] == ["embed"]


def test_the_cli_status_of_a_program_with_no_table_is_still_ok(program_root):
    result = _cli(program_root, "status")
    assert result["ok"] is True and result["result"]["configured"] == []


def test_the_cli_warns_when_a_started_sidecars_health_is_not_up_yet(program_root):
    _write_config(
        program_root,
        f'[sidecars.embed]\ncommand = ["{EXECUTABLE_FOR_TOML}", "-c", "import time\\nwhile True: time.sleep(0.05)"]\n'
        'health_url = "http://192.0.2.1:9/health"\nhealth_timeout_s = 0.05\n',
    )
    result = _cli(program_root, "start", "embed")
    assert result["ok"] is True
    codes = [w.get("code") for w in result.get("warnings", [])]
    assert "health_not_yet_ok" in codes
