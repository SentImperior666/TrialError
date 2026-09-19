"""FX-S1: the RESIDENT embed driver.

The bug this file guards (observed live 2026-09-06): the GPU worker ran at
``--batch-size 8`` and the embed backend re-launched its driver -- and so
re-loaded a multi-gigabyte model -- for every eight chunks. A 217-chunk
document meant 28 model loads for 28 batches of real work.

Everything here runs against a FAKE DRIVER: a small Python file each test
writes into ``tmp_path`` that speaks the same session protocol the real
driver does (readiness handshake, ``{"in","out"}`` control lines, one
``{"ok": ...}`` line back), returns deterministic vectors, and -- the
point -- COUNTS ITS OWN STARTUPS in a file. No GPU, no torch, no
``embed_backend`` install: the assertions are about process lifetime, which
is exactly what a stand-in can prove and a mock cannot.
"""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

import pytest

from trialerror.ingest.backends import (
    DEFAULT_EMBED_SESSION_MODE,
    EmbedDriverCrashed,
    RealQwenEmbedBackend,
    close_embed_driver_sessions,
    load_embed_backend,
)
from trialerror.jobs.worker import EnvironmentalFailure

# ---------------------------------------------------------------------------
# the fake driver
# ---------------------------------------------------------------------------

#: A resident driver that implements the protocol and nothing else.
#: ``argv[1]`` is the module_dir the backend passes (the real driver puts
#: it on ``sys.path``; this one uses it as its own scratch directory, which
#: is where the startup counter and the behaviour switch live). ``argv[2]``
#: is the model_key.
#:
#: Behaviour switches, all driven by the TEXTS in a request so a test can
#: choose them per batch rather than per process:
#:   ``CRASH``     -- write to stderr and exit (a driver that dies mid-session)
#:   ``HANG``      -- sleep far past any test timeout
#:   ``BADBATCH``  -- answer ``{"ok": false}`` but stay alive (a failed
#:                    request that is NOT a dead process)
#: plus one process-level switch, ``mode.txt == "refuse-start"``, for a
#: driver that cannot load its model at all.
FAKE_SESSION_DRIVER = '''\
import json, sys, time
from pathlib import Path

scratch = Path(sys.argv[1])
model_key = sys.argv[2]

counter = scratch / "startups.txt"
previous = int(counter.read_text(encoding="utf-8")) if counter.exists() else 0
counter.write_text(str(previous + 1), encoding="utf-8")

mode_path = scratch / "mode.txt"
mode = mode_path.read_text(encoding="utf-8").strip() if mode_path.exists() else "ok"
if mode == "refuse-start":
    sys.stdout.write(json.dumps({"ready": False, "error": "fake driver cannot load " + model_key}) + "\\n")
    sys.stdout.flush()
    raise SystemExit(1)

sys.stdout.write(json.dumps({"ready": True}) + "\\n")
sys.stdout.flush()

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    req = json.loads(line)
    payload = json.loads(Path(req["in"]).read_text(encoding="utf-8"))
    texts = payload["texts"]
    if "CRASH" in texts:
        sys.stderr.write("fake driver: exploded on a CRASH text\\n")
        sys.stderr.flush()
        raise SystemExit(3)
    if "HANG" in texts:
        time.sleep(60)
    if "BADBATCH" in texts:
        sys.stdout.write(json.dumps({"ok": False, "error": "fake driver: bad batch"}) + "\\n")
        sys.stdout.flush()
        continue
    vectors = [[float(len(t)), float(sum(ord(c) for c in t) % 97), float(len(payload["kind"]))] for t in texts]
    Path(req["out"]).write_text(json.dumps({"vectors": vectors, "dims": 3}), encoding="utf-8")
    sys.stdout.write(json.dumps({"ok": True}) + "\\n")
    sys.stdout.flush()
'''

#: The ONE-SHOT protocol's stand-in: same counter file, but it embeds one
#: batch from ``argv[1]`` (in-file) and exits -- which is what makes the
#: startup count rise per batch and proves the fallback is still the old
#: protocol rather than session mode wearing a different flag.
FAKE_ONE_SHOT_DRIVER = '''\
import json, sys
from pathlib import Path

in_path, scratch, out_path = sys.argv[1], Path(sys.argv[2]), sys.argv[3]
counter = scratch / "startups.txt"
previous = int(counter.read_text(encoding="utf-8")) if counter.exists() else 0
counter.write_text(str(previous + 1), encoding="utf-8")

payload = json.loads(Path(in_path).read_text(encoding="utf-8"))
texts = payload["texts"]
vectors = [[float(len(t)), float(sum(ord(c) for c in t) % 97), float(len(payload["kind"]))] for t in texts]
Path(out_path).write_text(json.dumps({"vectors": vectors, "dims": 3}), encoding="utf-8")
'''


def _startups(scratch: Path) -> int:
    path = scratch / "startups.txt"
    return int(path.read_text(encoding="utf-8")) if path.exists() else 0


def _session_backend(tmp_path: Path, *, timeout_s: float = 30.0, log=None) -> RealQwenEmbedBackend:
    """A backend wired to the fake session driver. ``module_dir`` doubles
    as the driver's scratch directory (the fake ignores ``sys.path``)."""
    driver = tmp_path / "fake_session_driver.py"
    driver.write_text(FAKE_SESSION_DRIVER, encoding="utf-8")
    backend = RealQwenEmbedBackend(
        python_exe=sys.executable,
        module_dir=str(tmp_path),
        model_key="fake-resident",
        dims=3,
        timeout_s=timeout_s,
        log=log,
    )
    # Instance-level seam, the same one the FX-1/FX-2 tests already use for
    # ``_DRIVER_SOURCE``: the driver really is spawned, really does speak
    # the protocol over real pipes -- only its body is the test's.
    backend._SESSION_DRIVER_SOURCE = driver.read_text(encoding="utf-8")
    return backend


@pytest.fixture(autouse=True)
def _no_leaked_drivers():
    """Nothing in this file may leave a driver process behind -- a test
    that forgets is a test that would have hidden the leak it is about."""
    yield
    close_embed_driver_sessions()


# ---------------------------------------------------------------------------
# the fix itself: one startup, many batches, many jobs
# ---------------------------------------------------------------------------


def test_session_mode_is_the_default():
    backend = RealQwenEmbedBackend(python_exe=sys.executable, module_dir="x")
    assert backend.session is DEFAULT_EMBED_SESSION_MODE is True


def test_driver_starts_once_across_many_batches(tmp_path):
    """The whole fix in one assertion: 8 batches, 1 model load."""
    backend = _session_backend(tmp_path)
    try:
        for i in range(8):
            vectors = backend.embed_batch([f"chunk {i}a", f"chunk {i}b"])
            assert len(vectors) == 2
    finally:
        backend.close()
    assert _startups(tmp_path) == 1


def test_driver_starts_once_across_many_jobs(tmp_path):
    """"Job" here is the caller-level unit the offload worker has: the same
    backend instance handed a fresh document's chunks. Under the old
    protocol each of these 3 x 4 batches was its own process."""
    backend = _session_backend(tmp_path)
    try:
        for job in range(3):
            for batch in range(4):
                backend.embed_batch([f"job {job} batch {batch}"])
    finally:
        backend.close()
    assert _startups(tmp_path) == 1


def test_session_vectors_are_the_drivers_own_and_batch_ordered(tmp_path):
    backend = _session_backend(tmp_path)
    try:
        vectors = backend.embed_batch(["aa", "bbbb"], kind="query")
    finally:
        backend.close()
    assert vectors == [
        [2.0, float(sum(ord(c) for c in "aa") % 97), 5.0],
        [4.0, float(sum(ord(c) for c in "bbbb") % 97), 5.0],
    ]


def test_session_forwards_the_kind_through_the_control_channel(tmp_path):
    """``kind`` decides query-vs-document prompting in the real backend, so
    it has to survive the extra hop the session protocol adds."""
    backend = _session_backend(tmp_path)
    try:
        as_document = backend.embed_batch(["x"], kind="document")
        as_query = backend.embed_batch(["x"], kind="query")
    finally:
        backend.close()
    assert as_document[0][2] == float(len("document"))
    assert as_query[0][2] == float(len("query"))


def test_large_batch_survives_the_pipe(tmp_path):
    """The payload travels through files precisely so a batch bigger than
    any pipe buffer is a non-event. 256 texts of 4 KB each is ~1 MB in and
    a comparable amount back -- well past a Windows pipe's default."""
    backend = _session_backend(tmp_path)
    texts = [f"{i}" + "x" * 4096 for i in range(256)]
    try:
        vectors = backend.embed_batch(texts)
    finally:
        backend.close()
    assert len(vectors) == 256
    assert _startups(tmp_path) == 1


# ---------------------------------------------------------------------------
# timeout: EnvironmentalFailure semantics preserved
# ---------------------------------------------------------------------------


def test_per_request_timeout_raises_environmental_failure(tmp_path):
    """FX-1's contract is unchanged by session mode: a wedged driver is an
    ENVIRONMENTAL failure, so ``trialerror.jobs.worker.run_one`` re-queues
    without consuming a retry attempt."""
    backend = _session_backend(tmp_path, timeout_s=0.5)
    try:
        with pytest.raises(EnvironmentalFailure) as excinfo:
            backend.embed_batch(["HANG"])
    finally:
        backend.close()
    assert "timed out" in excinfo.value.reason


def test_timeout_kills_the_wedged_driver_rather_than_leaving_it_running(tmp_path):
    backend = _session_backend(tmp_path, timeout_s=0.5)
    with pytest.raises(EnvironmentalFailure):
        backend.embed_batch(["HANG"])
    assert backend._session is None, "the wedged session must be dropped, not reused"
    # ...and the next call gets a fresh one that works.
    try:
        assert backend.embed_batch(["fine"])
    finally:
        backend.close()
    assert _startups(tmp_path) == 2


def test_startup_timeout_is_environmental_too(tmp_path, monkeypatch):
    """The readiness handshake IS the model load, so it is bounded by the
    same ``timeout_s`` -- and a machine whose GPU is thrashing at load time
    must not burn the job's retry budget."""
    backend = _session_backend(tmp_path, timeout_s=0.5)
    backend._SESSION_DRIVER_SOURCE = "import time\ntime.sleep(60)\n"
    with pytest.raises(EnvironmentalFailure) as excinfo:
        backend.embed_batch(["anything"])
    assert "timed out" in excinfo.value.reason
    assert backend._session is None


# ---------------------------------------------------------------------------
# crash-then-restart-once
# ---------------------------------------------------------------------------


def test_driver_crash_surfaces_the_stderr_head(tmp_path):
    backend = _session_backend(tmp_path)
    try:
        backend.embed_batch(["warm the driver up"])
        with pytest.raises(EmbedDriverCrashed) as excinfo:
            backend.embed_batch(["CRASH"])
    finally:
        backend.close()
    assert "exploded on a CRASH text" in str(excinfo.value)


def test_the_next_call_after_a_crash_restarts_the_driver_once(tmp_path):
    backend = _session_backend(tmp_path)
    try:
        backend.embed_batch(["first"])
        assert _startups(tmp_path) == 1

        with pytest.raises(EmbedDriverCrashed):
            backend.embed_batch(["CRASH"])
        assert backend._session is None, "a dead process must never be handed to the next batch"
        assert _startups(tmp_path) == 1, "the failing call itself must NOT retry"

        assert backend.embed_batch(["after the crash"])
        assert _startups(tmp_path) == 2, "exactly one restart, on the NEXT call"
    finally:
        backend.close()


def test_a_failed_batch_that_did_not_kill_the_driver_keeps_the_process(tmp_path):
    """A driver that reports ``{"ok": false}`` is still alive and still
    holding the model. Tearing it down for a bad batch would reintroduce
    the very reload this fix removes."""
    backend = _session_backend(tmp_path)
    try:
        backend.embed_batch(["first"])
        with pytest.raises(EmbedDriverCrashed) as excinfo:
            backend.embed_batch(["BADBATCH"])
        assert "bad batch" in str(excinfo.value)
        assert backend._session is not None and backend._session.alive
        backend.embed_batch(["still works"])
    finally:
        backend.close()
    assert _startups(tmp_path) == 1


def test_a_driver_that_refuses_to_start_says_so_instead_of_failing_the_first_batch(tmp_path):
    (tmp_path / "mode.txt").write_text("refuse-start", encoding="utf-8")
    backend = _session_backend(tmp_path)
    with pytest.raises(EmbedDriverCrashed) as excinfo:
        backend.embed_batch(["anything"])
    assert "cannot load fake-resident" in str(excinfo.value)
    assert backend._session is None


def test_a_driver_whose_first_line_is_not_json_fails_at_startup(tmp_path):
    backend = _session_backend(tmp_path)
    backend._SESSION_DRIVER_SOURCE = "print('not json at all')\n"
    with pytest.raises(EmbedDriverCrashed) as excinfo:
        backend.embed_batch(["anything"])
    assert "not JSON" in str(excinfo.value)


def test_a_driver_that_dies_before_saying_anything_surfaces_its_stderr(tmp_path):
    backend = _session_backend(tmp_path)
    backend._SESSION_DRIVER_SOURCE = (
        "import sys\nsys.stderr.write('no module named embed_backend\\n')\nraise SystemExit(2)\n"
    )
    with pytest.raises(EmbedDriverCrashed) as excinfo:
        backend.embed_batch(["anything"])
    assert "no module named embed_backend" in str(excinfo.value)


# ---------------------------------------------------------------------------
# shutdown
# ---------------------------------------------------------------------------


def test_close_reaps_the_driver_process(tmp_path):
    backend = _session_backend(tmp_path)
    backend.embed_batch(["warm up"])
    proc = backend._session._proc
    assert proc is not None and proc.poll() is None
    backend.close()
    assert proc.poll() is not None, "the driver must be gone, not merely forgotten"
    assert backend._session is None


def test_close_is_idempotent_and_safe_before_any_embedding(tmp_path):
    backend = _session_backend(tmp_path)
    backend.close()
    backend.close()
    assert _startups(tmp_path) == 0  # nothing was ever started


def test_context_manager_closes_the_driver(tmp_path):
    driver = tmp_path / "fake_session_driver.py"
    driver.write_text(FAKE_SESSION_DRIVER, encoding="utf-8")
    with _session_backend(tmp_path) as backend:
        backend.embed_batch(["inside the with"])
        proc = backend._session._proc
    assert proc.poll() is not None


def test_close_embed_driver_sessions_reaps_a_backend_nobody_closed(tmp_path):
    """The ``atexit`` backstop, called directly. A caller that forgets
    ``close()`` still must not leave a loaded model resident forever."""
    backend = _session_backend(tmp_path)
    backend.embed_batch(["leak me"])
    proc = backend._session._proc
    assert close_embed_driver_sessions() >= 1
    assert proc.poll() is not None


# ---------------------------------------------------------------------------
# the one-shot fallback, still there and still one process per batch
# ---------------------------------------------------------------------------


def _one_shot_backend(tmp_path: Path) -> RealQwenEmbedBackend:
    backend = RealQwenEmbedBackend(
        python_exe=sys.executable,
        module_dir=str(tmp_path),
        model_key="fake-one-shot",
        dims=3,
        timeout_s=30.0,
        session=False,
    )
    backend._DRIVER_SOURCE = FAKE_ONE_SHOT_DRIVER
    return backend


def test_one_shot_fallback_still_embeds(tmp_path):
    backend = _one_shot_backend(tmp_path)
    vectors = backend.embed_batch(["aa", "bbbb"], kind="document")
    assert vectors == [
        [2.0, float(sum(ord(c) for c in "aa") % 97), 8.0],
        [4.0, float(sum(ord(c) for c in "bbbb") % 97), 8.0],
    ]


def test_one_shot_fallback_is_still_one_process_per_batch(tmp_path):
    """The contrast that makes the fix legible: same three batches, three
    startups instead of one."""
    backend = _one_shot_backend(tmp_path)
    for i in range(3):
        backend.embed_batch([f"batch {i}"])
    assert _startups(tmp_path) == 3


def test_one_shot_fallback_never_opens_a_session(tmp_path):
    backend = _one_shot_backend(tmp_path)
    backend.embed_batch(["x"])
    assert backend._session is None
    backend.close()  # a no-op, and must not raise


# ---------------------------------------------------------------------------
# config seam
# ---------------------------------------------------------------------------


def test_load_embed_backend_enables_session_mode_by_default():
    backend = load_embed_backend(
        {"backend": "qwen3-4b", "python_exe": "C:/fake/python.exe", "module_dir": "C:/fake/embeddings_local"}
    )
    assert backend.session is True


def test_load_embed_backend_honours_session_false():
    backend = load_embed_backend(
        {
            "backend": "qwen3-4b",
            "python_exe": "C:/fake/python.exe",
            "module_dir": "C:/fake/embeddings_local",
            "session": False,
        }
    )
    assert backend.session is False


def test_the_shipped_session_driver_source_speaks_the_protocol_shape():
    """Not an execution test (that needs torch): a cheap guard that the
    real driver still announces readiness, loops over stdin, and answers
    with ``ok`` -- the three things :class:`_EmbedDriverSession` requires."""
    source = RealQwenEmbedBackend._SESSION_DRIVER_SOURCE
    assert "'ready': True" in source
    assert "for line in sys.stdin" in source
    assert "'ok': True" in source
    assert "load_backend(sys.argv[2])" in source


# ---------------------------------------------------------------------------
# the operator-visible signal
# ---------------------------------------------------------------------------


def test_driver_started_is_logged_exactly_once(tmp_path):
    lines: list[str] = []
    backend = _session_backend(tmp_path, log=lines.append)
    try:
        for i in range(5):
            backend.embed_batch([f"batch {i}"])
    finally:
        backend.close()
    assert [line for line in lines if "driver started" in line] == [
        "driver started (model_key=fake-resident)"
    ]


def test_a_restart_after_a_crash_logs_a_second_driver_started(tmp_path):
    """One "driver started" per model load, which is what makes the log a
    usable check on whether session mode is actually working in the field."""
    lines: list[str] = []
    backend = _session_backend(tmp_path, log=lines.append)
    try:
        backend.embed_batch(["first"])
        with pytest.raises(EmbedDriverCrashed):
            backend.embed_batch(["CRASH"])
        backend.embed_batch(["after"])
    finally:
        backend.close()
    assert len([line for line in lines if "driver started" in line]) == 2


def test_the_control_channel_only_carries_paths_not_text(tmp_path):
    """Design C-0007 ("page text never transits the orchestrator's
    context") applies to the new hop as well: chunk text goes to disk, and
    the pipe sees two file paths."""
    backend = _session_backend(tmp_path)
    session_writes: list[str] = []
    try:
        backend.embed_batch(["warm up"])
        session = backend._session
        real_stdin = session._proc.stdin

        class _Spy:
            def write(self, data):
                session_writes.append(data)
                return real_stdin.write(data)

            def flush(self):
                return real_stdin.flush()

        session._proc.stdin = _Spy()
        backend.embed_batch(["a very distinctive chunk of page text"])
        session._proc.stdin = real_stdin
    finally:
        backend.close()

    assert session_writes, "the control line should have gone through the spy"
    control = json.loads(session_writes[0])
    assert set(control) == {"in", "out"}
    assert "distinctive chunk of page text" not in session_writes[0]


# ---------------------------------------------------------------------------
# VERIFY V-1: the SHIPPED driver source against a chatty stand-in backend
#
# Everything above overrides ``_SESSION_DRIVER_SOURCE`` with a fake, so it
# proves the SESSION's behaviour and nothing about the driver that actually
# ships. The block below runs the real ``_SESSION_DRIVER_SOURCE``, with a
# stub ``embed_backend.py`` on its ``sys.path`` standing in for the module
# that lives outside this repo -- a stub that is DELIBERATELY chatty on
# stdout, because a model load that prints is the normal case (progress
# bars, "Loading checkpoint shards", a native library writing to fd 1) and
# whether it prints is not this repo's call.
#
# Before the fd-1 hand-off, one such line derailed every batch AND cost a
# ledger retry attempt each time -- worse than the reload this whole file
# exists to remove, on exactly the path no test here can reach with a real
# model. Still not GPU coverage: it is protocol coverage against a
# stand-in, which is the honest claim.
# ---------------------------------------------------------------------------

#: A stand-in for ``research/tools/embeddings_local/embed_backend.py``: the
#: ``load_backend(name).embed_batch(texts, kind=...)`` shape the driver
#: calls, returning an object with ``tolist()``/``shape`` the way a numpy
#: array does -- and printing to stdout in BOTH phases.
CHATTY_STUB_EMBED_BACKEND = '''\
import os, sys


class _Vectors:
    def __init__(self, rows):
        self._rows = rows
        self.shape = (len(rows), len(rows[0]) if rows else 0)

    def tolist(self):
        return self._rows


class _Backend:
    def embed_batch(self, texts, kind="document"):
        # Noise DURING a request: the shape of a per-batch progress line.
        print("stub backend: embedding %d text(s) as %s" % (len(texts), kind))
        return _Vectors([[float(len(t)), float(len(kind)), 7.0] for t in texts])


def load_backend(name):
    # Noise during the MODEL LOAD, both flavours: a Python-level print and
    # a write straight to fd 1 (what a native library does -- sys.stdout
    # rebinding alone would not catch it).
    print("Loading checkpoint shards:  50%|#####     | 2/4 [00:03<00:03]")
    os.write(1, b"stub backend: fd-1 noise from a native library" + bytes((10,)))
    sys.stdout.flush()
    return _Backend()
'''


def _shipped_driver_backend(tmp_path, *, timeout_s: float = 30.0, log=None) -> RealQwenEmbedBackend:
    """A backend running the SHIPPED session driver against the chatty
    stub. ``module_dir`` is what the driver puts on ``sys.path``, so the
    stub really is imported by name the way the real module is."""
    (tmp_path / "embed_backend.py").write_text(CHATTY_STUB_EMBED_BACKEND, encoding="utf-8")
    return RealQwenEmbedBackend(
        python_exe=sys.executable,
        module_dir=str(tmp_path),
        model_key="stub-chatty",
        dims=3,
        timeout_s=timeout_s,
        log=log,
    )


def test_the_shipped_driver_survives_a_backend_that_prints_to_stdout(tmp_path):
    """The regression V-1 names: chatty load AND chatty embed, three
    batches, no ``non-JSON`` failure and no reload."""
    lines: list[str] = []
    backend = _shipped_driver_backend(tmp_path, log=lines.append)
    try:
        for i in range(3):
            vectors = backend.embed_batch([f"text {i}", "second"])
            assert vectors == [[float(len(f"text {i}")), 8.0, 7.0], [6.0, 8.0, 7.0]]
        # One model load for three batches -- the fix itself, now measured
        # through the driver that ships rather than through a fake.
        assert len([line for line in lines if "driver started" in line]) == 1
    finally:
        backend.close()


def test_the_shipped_driver_keeps_the_backends_stdout_noise_as_diagnostics(tmp_path):
    """Redirected, not discarded: the load-time chatter has to remain
    readable, because on a GPU box it is often the only clue about what
    the model did. It goes where diagnostics already go."""
    backend = _shipped_driver_backend(tmp_path)
    try:
        backend.embed_batch(["hello"])
        noise = backend._session._stderr_head()
    finally:
        backend.close()
    assert "Loading checkpoint shards" in noise
    assert "fd-1 noise from a native library" in noise


def test_the_shipped_driver_forwards_the_kind_and_kills_its_process_on_close(tmp_path):
    """The remaining two contract points of the shipped driver: ``kind``
    reaches ``embed_batch`` (the stub folds it into the vector), and
    ``close()`` really reaps the process rather than leaving a loaded model
    behind."""
    backend = _shipped_driver_backend(tmp_path)
    try:
        vectors = backend.embed_batch(["q"], kind="query")
        assert vectors == [[1.0, 5.0, 7.0]]
        proc = backend._session._proc
    finally:
        backend.close()
    assert proc.poll() is not None


# ---------------------------------------------------------------------------
# VERIFY V-6: FX-2's stderr decoding, on the DEFAULT (session) path
#
# The three RealQwenEmbedBackend FX-1/FX-2 cases in tests/test_ingest_backends.py
# were pinned to session=False so they keep owning the one-shot protocol's
# coverage -- correct, but it left the decoding contract with no guard on
# the path that now runs by default. The behaviour is right; only the
# regression guard was missing.
# ---------------------------------------------------------------------------


def test_session_stderr_head_decodes_non_ascii_as_utf8_not_cp1252(tmp_path):
    """A torch/transformers traceback is UTF-8 and can carry non-ASCII.
    The child writes real UTF-8 BYTES to ``stderr.buffer`` so the assertion
    is about the PARENT's decode (``Popen(encoding='utf-8')``), not the
    child's encode."""
    backend = _session_backend(tmp_path, timeout_s=30)
    backend._SESSION_DRIVER_SOURCE = (
        "import sys\n"
        "sys.stderr.buffer.write('embed failed on caf\u00e9 \u2014 mojibake canary'.encode('utf-8'))\n"
        "sys.stderr.buffer.flush()\n"
        "raise SystemExit(1)\n"
    )
    with pytest.raises(EmbedDriverCrashed) as excinfo:
        backend.embed_batch(["hello world"])
    assert "café" in str(excinfo.value)


def test_session_invalid_utf8_stderr_does_not_raise_unicode_decode_error(tmp_path):
    """FX-2's ``errors='replace'`` half on the session path: a stray
    non-UTF-8 byte must degrade to U+FFFD, never crash the decode with a
    UnicodeDecodeError that would masquerade as an unrelated logic
    failure."""
    backend = _session_backend(tmp_path, timeout_s=30)
    backend._SESSION_DRIVER_SOURCE = (
        "import sys\n"
        "sys.stderr.buffer.write(b'bad byte follows: \xff\xfe garbage')\n"
        "sys.stderr.buffer.flush()\n"
        "raise SystemExit(1)\n"
    )
    with pytest.raises(EmbedDriverCrashed) as excinfo:  # NOT UnicodeDecodeError
        backend.embed_batch(["hello world"])
    assert "bad byte follows" in str(excinfo.value)


# ---------------------------------------------------------------------------
# VERIFY V-8: one exchange at a time on the shared pipe
# ---------------------------------------------------------------------------


def test_two_threads_cannot_interleave_one_sessions_exchange(tmp_path):
    """Session mode turns a stateless backend into a long-lived shared
    object, which is the shape that invites a caller to share one across
    threads. The write and the read are two halves of one conversation on
    one pipe: interleave them and each thread gets the other's vectors,
    silently. The gate below parks in the middle of thread A's exchange and
    proves thread B never reaches its own write until A is done."""
    backend = _session_backend(tmp_path, timeout_s=30)
    session = None
    real_stdin = None
    try:
        backend.embed_batch(["warm up"])
        session = backend._session
        real_stdin = session._proc.stdin
        entered = threading.Event()
        release = threading.Event()

        class _Gate:
            def write(self, data):
                entered.set()
                release.wait(10.0)
                return real_stdin.write(data)

            def flush(self):
                return real_stdin.flush()

        session._proc.stdin = _Gate()
        results: dict[str, list] = {}

        def _embed(name: str) -> None:
            results[name] = backend.embed_batch([name])

        first = threading.Thread(target=_embed, args=("first",))
        first.start()
        assert entered.wait(10.0), "thread A never reached the control write"
        entered.clear()

        second = threading.Thread(target=_embed, args=("second",))
        second.start()
        # THE assertion: B is parked on the lock, not writing into the
        # middle of A's exchange.
        assert not entered.wait(0.5), "thread B wrote into an exchange already in flight"

        release.set()
        first.join(20)
        second.join(20)
        assert not first.is_alive() and not second.is_alive()
    finally:
        if session is not None and real_stdin is not None:
            session._proc.stdin = real_stdin
        backend.close()

    # Each thread got ITS OWN vectors (the fake driver derives them from
    # the text), which is what a crossed response would have broken.
    assert results["first"] == [[5.0, float(sum(ord(c) for c in "first") % 97), 8.0]]
    assert results["second"] == [[6.0, float(sum(ord(c) for c in "second") % 97), 8.0]]
