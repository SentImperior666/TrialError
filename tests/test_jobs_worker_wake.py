"""Mining adoption rowboat-F8: poll-window jitter + wake signal in
``trialerror.jobs.worker.run_loop`` (``docs/reviews/
MINING_2026-09_OPERATOR_LINKS.md`` section 3, verdict "adopt-now:jobs --
jitter + wake-signal in run_loop ... port with the two bug fixes named").

Both named source bugs get their own test here -- a reversed window
(``test_jitter_normalizes_a_reversed_window``) and a nap that would land
in the past (``test_jitter_never_returns_a_negative_nap``) -- because
"ported with the bug fixed" is a claim a reader should be able to check
without reading the original TypeScript.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import tests._job_handlers  # noqa: F401 - registers the test_* job handlers
from trialerror.jobs import ledger
from trialerror.jobs.worker import (
    DEFAULT_JITTER_FRACTION,
    jittered_poll_interval,
    kick,
    make_worker_id,
    read_wake_token,
    run_loop,
    wake_signal_path,
)
from trialerror.stores.store import open_store

# ---------------------------------------------------------------------------
# jitter
# ---------------------------------------------------------------------------


def test_jitter_stays_inside_the_poll_window():
    for i in range(200):
        nap = jittered_poll_interval(2.0, worker_id=f"worker-{i}", poll_index=i % 7)
        assert 2.0 * (1 - DEFAULT_JITTER_FRACTION) <= nap <= 2.0 * (1 + DEFAULT_JITTER_FRACTION)


def test_jitter_is_deterministic_per_worker_and_poll():
    a1 = jittered_poll_interval(2.0, worker_id="1234:ts", poll_index=3)
    a2 = jittered_poll_interval(2.0, worker_id="1234:ts", poll_index=3)
    assert a1 == a2  # reproducible: the same worker naps the same way twice


def test_jitter_staggers_distinct_workers():
    """The whole point of the adoption: N workers launched in the same
    second must not all claim on the same tick."""
    naps = {jittered_poll_interval(2.0, worker_id=f"{pid}:2026-09-05T00:00:00.000Z") for pid in range(50)}
    assert len(naps) > 40  # near-total spread, not a handful of buckets


def test_jitter_frac_zero_is_the_old_fixed_nap():
    assert jittered_poll_interval(2.0, worker_id="w", jitter_frac=0.0) == 2.0


def test_jitter_normalizes_a_reversed_window():
    """PORTED BUG FIX 1: the source has no wrap handling when the window's
    end precedes its start. A negative fraction reverses the bounds; the
    port normalizes instead of sampling a negative span."""
    nap = jittered_poll_interval(2.0, worker_id="w", jitter_frac=-0.25)
    assert 1.5 <= nap <= 2.5


def test_jitter_never_returns_a_negative_nap():
    """PORTED BUG FIX 2: the source can pick a run time already in the
    past. A fraction above 1.0 drives the low bound below zero; the port
    clamps at zero, so the worst case is "poll immediately"."""
    for i in range(100):
        nap = jittered_poll_interval(1.0, worker_id=f"w{i}", poll_index=i, jitter_frac=3.0)
        assert nap >= 0.0


# ---------------------------------------------------------------------------
# wake signal
# ---------------------------------------------------------------------------


def test_wake_signal_lives_next_to_the_jobs_db(program_root):
    assert wake_signal_path(program_root).parent == (program_root / "stores")


def test_kick_writes_a_fresh_token_each_time(program_root):
    first = kick(program_root)
    second = kick(program_root)
    assert first["token"] != second["token"]
    assert read_wake_token(wake_signal_path(program_root)) == second["token"]


def test_kick_leaves_no_temp_file_behind(program_root):
    kick(program_root)
    leftovers = list((program_root / "stores").glob("*.tmp"))
    assert leftovers == []


def test_read_wake_token_is_total_on_a_missing_file(tmp_path):
    assert read_wake_token(tmp_path / "nope" / "jobs.wake") is None
    assert read_wake_token(None) is None


def test_read_wake_token_treats_an_empty_file_as_no_signal(tmp_path):
    path = tmp_path / "jobs.wake"
    path.write_text("   \n", encoding="utf-8")
    assert read_wake_token(path) is None


# ---------------------------------------------------------------------------
# run_loop integration
# ---------------------------------------------------------------------------


def test_run_loop_exits_on_idle_streak_when_nothing_kicks(store):
    results = run_loop(store, poll_interval_s=0.01, max_idle_polls=2)
    assert [r["status"] for r in results] == ["idle", "idle"]


def test_run_loop_is_woken_by_a_kick_and_runs_the_new_job(store, program_root, platform_root):
    """The behaviour the adoption buys: a job enqueued during a nap runs at
    the kick, not at the end of the poll interval. The enqueue happens on
    its OWN store handle (sqlite3 connections are thread-bound) -- which is
    also the realistic shape: the thing that enqueues is another process."""

    def _enqueue_then_kick():
        time.sleep(0.05)
        other = open_store(program_root, platform_root=platform_root)
        try:
            ledger.enqueue(other, kind="custom", payload={"handler": "noop"})
        finally:
            other.close()
        kick(program_root)

    # A deliberately long poll interval: without the wake signal this loop
    # would sit idle for 20s. max_iterations=1 makes it exit the moment the
    # woken pass claims the job, so the test measures the wake, not the nap.
    t = threading.Thread(target=_enqueue_then_kick)
    t.start()
    try:
        results = run_loop(store, poll_interval_s=20.0, max_idle_polls=2, wake_tick_s=0.02, max_iterations=1)
    finally:
        t.join()

    statuses = [r["status"] for r in results]
    assert "woken" in statuses
    assert statuses[-1] == "complete"


def test_a_wake_resets_the_idle_streak(store, program_root):
    """Documented semantics: a kick is an outside caller asserting work
    exists, so it counts like a successful claim and the loop stays alive
    past what would otherwise have been its last idle poll."""

    def _kick_once():
        time.sleep(0.05)
        kick(program_root)

    t = threading.Thread(target=_kick_once)
    t.start()
    try:
        results = run_loop(store, poll_interval_s=0.6, max_idle_polls=2, wake_tick_s=0.02)
    finally:
        t.join()

    statuses = [r["status"] for r in results]
    assert statuses.count("woken") == 1
    # idle, woken, then a fresh streak of 2 idles before the loop gives up
    assert statuses == ["idle", "woken", "idle", "idle"]


def test_run_loop_ignores_the_wake_signal_when_disabled(store, program_root):
    kick(program_root)  # a token already sitting there before the loop starts
    results = run_loop(store, poll_interval_s=0.01, max_idle_polls=2, wake_signal=False)
    assert [r["status"] for r in results] == ["idle", "idle"]


def test_a_pre_existing_token_is_a_baseline_not_a_wake(store, program_root):
    """A kick from last week must not wake a loop that starts today --
    only a token CHANGE is a signal."""
    kick(program_root)
    results = run_loop(store, poll_interval_s=0.01, max_idle_polls=2)
    assert "woken" not in [r["status"] for r in results]


def test_run_loop_accepts_an_explicit_wake_path(store, tmp_path):
    explicit = tmp_path / "elsewhere" / "jobs.wake"
    explicit.parent.mkdir(parents=True)

    def _kick_once():
        time.sleep(0.05)
        explicit.write_text("TOKEN-1", encoding="utf-8")

    t = threading.Thread(target=_kick_once)
    t.start()
    try:
        results = run_loop(
            store, poll_interval_s=0.6, max_idle_polls=2, wake_signal=explicit, wake_tick_s=0.02
        )
    finally:
        t.join()
    woken = [r for r in results if r["status"] == "woken"]
    assert len(woken) == 1
    assert woken[0]["token"] == "TOKEN-1"


def test_run_loop_survives_an_unreadable_wake_directory(store, tmp_path):
    """A wake path whose parent does not exist must degrade to the
    pre-adoption behaviour (sleep out the nap), never crash the worker."""
    results = run_loop(
        store,
        poll_interval_s=0.01,
        max_idle_polls=2,
        wake_signal=Path(tmp_path / "no" / "such" / "dir" / "jobs.wake"),
    )
    assert [r["status"] for r in results] == ["idle", "idle"]


def test_spawn_worker_argv_carries_the_new_knobs_only_when_overridden(store, program_root, monkeypatch):
    from trialerror.jobs import worker as worker_mod

    captured: dict[str, list[str]] = {}

    class _FakePopen:
        pid = 4242

        def __init__(self, argv, **kwargs):
            captured["argv"] = argv

    monkeypatch.setattr(worker_mod.subprocess, "Popen", _FakePopen)

    worker_mod.spawn_worker(program_root=program_root, mode="loop")
    assert "--jitter-frac" not in captured["argv"]
    assert "--no-wake-signal" not in captured["argv"]

    worker_mod.spawn_worker(program_root=program_root, mode="loop", jitter_frac=0.0, wake_signal=False)
    assert captured["argv"][captured["argv"].index("--jitter-frac") + 1] == "0.0"
    assert "--no-wake-signal" in captured["argv"]


def test_make_worker_id_feeds_the_jitter_with_a_distinct_value():
    """Two worker ids minted in the same process differ, which is what
    :func:`jittered_poll_interval` relies on to stagger them."""
    ids = {make_worker_id(pid) for pid in range(10)}
    assert len(ids) == 10


# ---------------------------------------------------------------------------
# the wake token follows [paths].stores_dir (fix pass, F-04)
# ---------------------------------------------------------------------------


def _relocated_program(tmp_path, dirname: str = "db_elsewhere"):
    """A program whose ``trialerror.toml`` moves its stores out of the
    default ``stores/`` -- the same relocation-fixture shape
    ``tests/test_config_paths_knobs.py`` uses."""
    program_root = tmp_path / "relocated-program"
    program_root.mkdir()
    (program_root / "trialerror.toml").write_text(
        "\n".join(["[program]", 'id = "wake-knob-check"', "", "[paths]", f'stores_dir = "{dirname}"', ""]),
        encoding="utf-8",
    )
    return program_root


def test_the_wake_token_follows_a_relocated_stores_dir(tmp_path):
    """``wake_signal_path``'s docstring promises the wake signal moves with
    ``[paths].stores_dir``. Before the fix pass its ``config`` argument was
    dead -- NO caller passed one (``run_loop`` has only a ``Store``,
    ``jobs kick`` deliberately opens nothing at all), so the token always
    landed at the hardcoded ``stores/`` literal while ``jobs.db`` itself
    sat wherever the knob put it."""
    program_root = _relocated_program(tmp_path)
    assert wake_signal_path(program_root).parent == program_root / "db_elsewhere"


def test_a_kick_on_a_relocated_program_writes_next_to_the_jobs_db(tmp_path):
    """And leaves no stray default-named directory behind -- the old
    behavior created a ``stores/`` the program otherwise does not have."""
    program_root = _relocated_program(tmp_path)
    result = kick(program_root)

    token_path = program_root / "db_elsewhere" / "jobs.wake"
    assert Path(result["wake_signal_path"]) == token_path
    assert read_wake_token(token_path) == result["token"]
    assert not (program_root / "stores").exists()


def test_the_worker_and_the_cli_agree_on_a_relocated_wake_path(tmp_path, platform_root):
    """The two sides must resolve the SAME file. They agreed before the fix
    only by being wrong in the same way; this pins the agreement to the
    location the store actually uses."""
    from trialerror.jobs.worker import _resolve_wake_path

    program_root = _relocated_program(tmp_path)
    store = open_store(program_root, platform_root=platform_root)
    try:
        # where open_store actually put jobs.db, straight from the connection
        jobs_db = Path(store.jobs.execute("PRAGMA database_list").fetchone()[2])
        worker_side = _resolve_wake_path(store, True)
    finally:
        store.close()

    cli_side = Path(kick(program_root)["wake_signal_path"])
    assert jobs_db.parent.name == "db_elsewhere"  # the knob really did relocate the store
    assert worker_side == cli_side == jobs_db.parent / "jobs.wake"


def test_an_explicit_config_still_wins_over_discovery(tmp_path):
    program_root = _relocated_program(tmp_path)
    explicit = wake_signal_path(program_root, {"paths": {"stores_dir": "somewhere_else"}})
    assert explicit.parent == program_root / "somewhere_else"


def test_a_program_without_a_config_still_uses_the_default_literal(program_root):
    """No ``trialerror.toml`` -> the pre-existing hardcoded default, byte
    for byte (the "missing/invalid config reproduces the old behavior"
    half of the loader convention)."""
    assert not (program_root / "trialerror.toml").exists()
    assert wake_signal_path(program_root).parent == program_root / "stores"
