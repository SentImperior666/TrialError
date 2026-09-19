"""Worker control and worker observability (ruling C-0097,
``docs/reviews/WORKER_CONTROL_DESIGN.md``) -- acceptance A and the parts of
D2/D3/D7/D9 that live in the worker.

The law under test, in one sentence: anything the interface can start, the
interface can pause and stop, *cooperatively* -- the job finishes its current
unit, records where it stopped, keeps or returns its claim, and can resume.
Nothing here kills a process, which is why every test below is about a FLAG the
worker chose to read rather than a signal something sent it.

**No sleeps anywhere in this module.** A cooperative checkpoint can only act on
a word the heartbeat thread has already seen, so timing is made deterministic
through two seams instead of through waiting: ``ControlTransport.word_for``
decides the reply per beat, and ``StubEmbedBackend.before_batch`` lets a test
hold the model at a known batch boundary until the word is in. A test that
slept would be a test that passes on a fast machine.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from trialerror.offload import control as control_api
from trialerror.offload import protocol
from trialerror.offload.transport import LocalTransport, TransportError
from trialerror.offload.worker import (
    DEFAULT_EMBED_BATCH_SIZE,
    ProgressState,
    ResidentBackends,
    STOPPED_MARKER,
    WorkerControl,
    run_worker,
)
from tests._offload_fixtures import (
    ClosableStubEmbedBackend,
    ControlTransport,
    StubDevBackends,
    StubEmbedBackend,
    StubOcrBackend,
    queue_chunks,
    queue_one,
)

#: Small enough that the heartbeat thread beats many times during a job, so
#: "kept heartbeating while paused" is observable without waiting for anything.
FAST_BEAT = 0.01
#: How long an assertion in this module is willing to wait for an EVENT another
#: thread must set (a reported state, a recorded beat). It is not a worker bound
#: and it is never passed to ``run_worker``: every pause below is released by an
#: OBSERVED ``paused`` beat, which is the only release that proves the worker
#: was still talking to the queue while it held (FIX V-1, FIX V-4). A run that
#: needs a bound says so in its own call.
WAIT_S = 5.0


def _queue(tmp_path: Path) -> Path:
    return protocol.ensure_layout(tmp_path / "offload")


def _run(transport, tmp_path, *, backends=None, **kwargs):
    """``run_worker`` with the PRODUCTION pause semantics: ``max_pause_s`` is
    not passed, so it is ``None`` -- a pause holds until a resume or a stop
    arrives.

    That default used to be overridden here with a five-second bound, and the
    bound was silently absorbing a wedge: a pause taken outside a job stopped
    the worker beating altogether, which no test could see because the bound
    released it (FIX V-1/V-4, verifier findings of the same names)."""
    return run_worker(
        transport=transport,
        backends=backends or StubDevBackends(),
        work_root=tmp_path / "work",
        heartbeat_interval_s=FAST_BEAT,
        **kwargs,
    )


def _paused_states(transport) -> list[dict]:
    return [p for _j, p in transport.beats if (p or {}).get("state") == "paused"]


# ---------------------------------------------------------------------------
# A: pause holds the claim and keeps heartbeating
# ---------------------------------------------------------------------------
def test_pause_holds_the_claim_and_keeps_heartbeating(tmp_path):
    """The defining property of a pause, as opposed to a stop: the job is
    still OURS and the sandbox can still see us. A paused worker that stopped
    beating would be reclaimed after 60 minutes and the pause would silently
    become a loss of work."""
    root = _queue(tmp_path)
    queue_chunks(root, "JOB-embed-1", count=12)

    paused_seen = threading.Event()
    resumed = threading.Event()
    asked: list[int] = []

    def word_for(beat, job_id, payload):
        state = (payload or {}).get("state")
        if state == "paused":
            paused_seen.set()
            # Hold the pause for a few beats so "kept heartbeating while
            # paused" is a measurement, then resume.
            if len(_paused_states(transport)) >= 3:
                resumed.set()
                return "resume"
            return "none"
        # The pause is asked for ONCE, on a beat that belongs to the JOB, which
        # is what makes "the claim is held" the thing under test. (It used to be
        # asked for on beat 1 -- the idle beat, before anything was claimed --
        # so the pause it measured was a pause with no claim at all.)
        if job_id != protocol.idle_job_id("dev") and not asked:
            asked.append(beat)
            return "pause"
        return "none"

    transport = ControlTransport(root, word_for=word_for)
    # The model is held until two beats for this job are recorded, by which
    # point the first beat's word has certainly been applied -- so the pause is
    # in hand before the first checkpoint rather than raced for.
    embed = StubEmbedBackend(
        before_batch=lambda i: transport.wait_for_beats("JOB-embed-1", 2) if i == 0 else None
    )
    summary = _run(transport, tmp_path, backends=StubDevBackends(embed=embed), batch_size=4)

    assert paused_seen.is_set(), "the worker never reported state=paused"
    assert resumed.is_set()
    paused_beats = [p for _j, p in transport.beats if (p or {}).get("state") == "paused"]
    assert len(paused_beats) >= 2, f"only {len(paused_beats)} paused beat(s) -- a pause must keep beating"
    # The claim was held throughout: the job published, so it was never
    # returned to pending mid-pause.
    assert summary["published"] == ["JOB-embed-1"]
    assert summary["stopped"] == []
    assert [t["transition"] for t in summary["control"]] == ["paused", "resumed"]


def test_a_paused_worker_keeps_its_claim_in_the_queue_directory(tmp_path):
    """The same property read off the QUEUE rather than off the summary: while
    the worker is paused the manifest is still in ``claimed/`` and the
    heartbeat stamp is still being refreshed."""
    root = _queue(tmp_path)
    queue_chunks(root, "JOB-embed-1", count=8)
    observed: dict[str, object] = {}
    paused_seen = threading.Event()
    asked: list[int] = []

    def word_for(beat, job_id, payload):
        if (payload or {}).get("state") == "paused":
            if not paused_seen.is_set():
                observed["claims"] = protocol.list_claims(root)
                observed["pending"] = protocol.list_pending(root)
                observed["beat_job"] = job_id
                paused_seen.set()
            return "resume"
        if job_id != protocol.idle_job_id("dev") and not asked:
            asked.append(beat)
            return "pause"
        return "none"

    transport = ControlTransport(root, word_for=word_for)
    embed = StubEmbedBackend(
        before_batch=lambda i: transport.wait_for_beats("JOB-embed-1", 2) if i == 0 else None
    )
    _run(transport, tmp_path, backends=StubDevBackends(embed=embed), batch_size=2)

    assert [c["job_id"] for c in observed["claims"]] == ["JOB-embed-1"]
    assert observed["pending"] == []
    # …and the beat that carried the paused state was the JOB's own, not the
    # idle slot's: the claim is what a pause holds.
    assert observed["beat_job"] == "JOB-embed-1"


# ---------------------------------------------------------------------------
# A: resume continues from the next unit -- nothing repeated, nothing skipped
# ---------------------------------------------------------------------------
def test_resume_continues_from_the_next_unit(tmp_path):
    """The exact wording of the acceptance: "no unit repeated, none skipped".

    Proven over the TEXTS the model was handed, not over a count: a count
    cannot tell a repeated chunk from a skipped one."""
    root = _queue(tmp_path)
    queue_chunks(root, "JOB-embed-1", count=12)
    paused_seen = threading.Event()
    asked: list[int] = []

    def word_for(beat, job_id, payload):
        if (payload or {}).get("state") == "paused":
            paused_seen.set()
            return "resume"
        # In the job, so the resume really does continue from the next UNIT
        # rather than from the start of a job that had not been claimed yet.
        if job_id != protocol.idle_job_id("dev") and not asked:
            asked.append(beat)
            return "pause"
        return "none"

    transport = ControlTransport(root, word_for=word_for)
    embed = StubEmbedBackend(
        before_batch=lambda i: transport.wait_for_beats("JOB-embed-1", 2) if i == 0 else None
    )
    summary = _run(transport, tmp_path, backends=StubDevBackends(embed=embed), batch_size=4)

    assert summary["published"] == ["JOB-embed-1"]
    assert _paused_states(transport), "the pause never landed inside the job"
    assert embed.texts_seen == [f"chunk body {i}" for i in range(12)]
    assert sum(embed.batches) == 12
    # …and the published vectors are the full set, in order.
    published = (protocol.done_dir(root) / "JOB-embed-1" / "vectors.jsonl").read_text(encoding="utf-8")
    assert len([ln for ln in published.splitlines() if ln.strip()]) == 12


# ---------------------------------------------------------------------------
# A: stop writes the partial result and returns the claim
# ---------------------------------------------------------------------------
def test_stop_writes_a_partial_result_and_returns_the_claim(tmp_path):
    """The stop lands DURING the job, which the fixture pins exactly: the model
    is held at the first batch boundary until two beats for this job have been
    recorded, by which point the first ``stop`` has certainly been applied."""
    root = _queue(tmp_path)
    queue_chunks(root, "JOB-embed-1", count=12)

    transport = ControlTransport(
        root, word_for=lambda beat, job, payload: "stop" if job == "JOB-embed-1" else "none"
    )
    embed = StubEmbedBackend(
        before_batch=lambda i: transport.wait_for_beats("JOB-embed-1", 2) if i == 0 else None
    )
    summary = _run(transport, tmp_path, backends=StubDevBackends(embed=embed), batch_size=4)

    assert summary["stopped"] == ["JOB-embed-1"]
    assert summary["published"] == []
    assert "Stopped on request" in summary["message"]
    assert [t["transition"] for t in summary["control"]] == ["stopping"]

    # the claim went back to pending through the EXISTING return verb
    assert protocol.list_pending(root) == ["JOB-embed-1"]
    assert protocol.list_claims(root) == []
    assert protocol.list_done(root) == []

    # the partial result records what was done, in the work dir only
    base = tmp_path / "work" / "JOB-embed-1"
    result = json.loads((base / "out" / protocol.RESULT_FILENAME).read_text(encoding="utf-8"))
    assert result["status"] == "stopped"
    assert result["units_total"] == 12
    assert result["unit"] == "chunk"
    assert 0 < result["units_done"] < 12
    assert result["units_done"] == sum(embed.batches)
    assert (base / STOPPED_MARKER).is_file()


def test_a_stopped_partial_is_never_published_by_a_later_poll(tmp_path):
    """The trap the STOPPED marker exists for. ``_retry_publishes`` runs
    before every claim and pushes anything with a ``result.json`` it has not
    handed over -- which, without the marker, would mean the next run pushing a
    half-finished result at a job the queue has already given to someone else.
    """
    root = _queue(tmp_path)
    queue_chunks(root, "JOB-embed-1", count=12)
    stop_once = ControlTransport(
        root, word_for=lambda beat, job, payload: "stop" if job == "JOB-embed-1" else "none"
    )
    first = StubEmbedBackend(
        before_batch=lambda i: stop_once.wait_for_beats("JOB-embed-1", 2) if i == 0 else None
    )
    _run(stop_once, tmp_path, backends=StubDevBackends(embed=first), batch_size=4)
    assert protocol.list_pending(root) == ["JOB-embed-1"]
    assert (tmp_path / "work" / "JOB-embed-1" / STOPPED_MARKER).is_file()

    # A second, uncontrolled run: it must run the job afresh and publish a
    # COMPLETE result, never adopt the partial one left behind.
    plain = ControlTransport(root)
    embed = StubEmbedBackend()
    summary = _run(plain, tmp_path, backends=StubDevBackends(embed=embed), batch_size=4)
    assert summary["published"] == ["JOB-embed-1"]
    # FIX V-10: the second run's first sweep dropped the stopped directory, so
    # "never published" is now true by construction and not only by a check.
    assert embed.texts_seen == [f"chunk body {i}" for i in range(12)]
    result = json.loads(
        (protocol.done_dir(root) / "JOB-embed-1" / protocol.RESULT_FILENAME).read_text(encoding="utf-8")
    )
    assert "status" not in result


# ---------------------------------------------------------------------------
# FIX V-10: the marker lands before the result, and a stopped dir is swept
# ---------------------------------------------------------------------------
def test_the_stopped_marker_is_written_before_the_partial_result(tmp_path, monkeypatch):
    """Write order, pinned by making the SECOND write fail. The marker is the
    only thing that keeps ``_retry_publishes`` off a partial result, so a crash
    between the two used to leave a publishable half-finished result for a job
    whose claim had already gone back to the queue."""
    from trialerror.offload import worker as worker_mod

    root = _queue(tmp_path)
    queue_chunks(root, "JOB-embed-1", count=12)

    def boom(*_args, **_kwargs):
        raise RuntimeError("crashed between the marker and the result")

    monkeypatch.setattr(worker_mod, "_write_result", boom)
    transport = ControlTransport(
        root, word_for=lambda beat, job, payload: "stop" if job == "JOB-embed-1" else "none"
    )
    embed = StubEmbedBackend(
        before_batch=lambda i: transport.wait_for_beats("JOB-embed-1", 2) if i == 0 else None
    )
    summary = _run(transport, tmp_path, backends=StubDevBackends(embed=embed), batch_size=4)

    base = tmp_path / "work" / "JOB-embed-1"
    assert (base / STOPPED_MARKER).is_file(), "the marker must land before the result"
    assert summary["failed"] == ["JOB-embed-1"], "the crash itself still travels as a failure"


def test_a_stopped_work_directory_is_swept_by_the_next_run(tmp_path):
    """The partial outlives the run that wrote it -- an operator can read what
    the GPU time bought -- and is then swept by the first sweep of a later run.
    It used to survive for ever unless the same worker happened to re-claim the
    same job id."""
    root = _queue(tmp_path)
    queue_chunks(root, "JOB-embed-1", count=12)
    stop_once = ControlTransport(
        root, word_for=lambda beat, job, payload: "stop" if job == "JOB-embed-1" else "none"
    )
    embed = StubEmbedBackend(
        before_batch=lambda i: stop_once.wait_for_beats("JOB-embed-1", 2) if i == 0 else None
    )
    _run(stop_once, tmp_path, backends=StubDevBackends(embed=embed), batch_size=4)
    base = tmp_path / "work" / "JOB-embed-1"
    assert (base / STOPPED_MARKER).is_file(), "the record survives the run that wrote it"
    assert (base / "out" / protocol.RESULT_FILENAME).is_file()

    # a later run on an EMPTY queue: nothing to claim, and the sweep still runs
    protocol.server_claim(root, "JOB-embed-1", worker_id="dev")  # park it out of `pending`
    logged: list[str] = []
    run_worker(
        transport=ControlTransport(root),
        backends=StubDevBackends(),
        work_root=tmp_path / "work",
        heartbeat_interval_s=FAST_BEAT,
        log=logged.append,
    )
    assert not base.exists(), "a stopped work directory must not leak"
    assert any("stopped partial swept" in line for line in logged)


def test_stop_on_an_ocr_job_publishes_the_finished_unit_and_then_exits(tmp_path):
    """D6 read honestly. For OCR the unit IS the job, so "finish the current
    unit" finishes the whole thing -- and handing that claim back would throw
    away a marker run that already succeeded, in a subsystem whose first
    principle is that GPU minutes are never recomputed. The run ends after the
    job, which is the other half of the instruction."""
    root = _queue(tmp_path)
    queue_one(root, "JOB-ocr-1", stage="ocr", payload=b"page one")
    queue_one(root, "JOB-ocr-2", stage="ocr", payload=b"page two")

    transport = ControlTransport(
        root, word_for=lambda beat, job, payload: "stop" if job == "JOB-ocr-1" else "none"
    )
    ocr = StubOcrBackend(before_run=lambda n: transport.wait_for_beats("JOB-ocr-1", 2))
    summary = _run(transport, tmp_path, backends=StubDevBackends(ocr=ocr), batch_size=4)

    assert summary["published"] == ["JOB-ocr-1"]
    assert summary["stopped"] == []
    assert "Stopped on request" in summary["message"]
    assert ocr.calls == 1, "the second job must not have been claimed"
    assert protocol.list_pending(root) == ["JOB-ocr-2"]


def test_stop_with_no_claim_held_ends_the_run_before_claiming(tmp_path):
    """The between-jobs checkpoint, reached on the idle beat at the top of the
    poll: a stop that arrives while nothing is claimed costs nothing at all."""
    root = _queue(tmp_path)
    queue_chunks(root, "JOB-embed-1", count=4)
    transport = ControlTransport(root, word_for=lambda beat, job, payload: "stop")
    embed = StubEmbedBackend()
    summary = _run(transport, tmp_path, backends=StubDevBackends(embed=embed), batch_size=4)

    assert summary["claimed"] == [] and summary["published"] == [] and summary["stopped"] == []
    assert embed.batches == []
    assert protocol.list_pending(root) == ["JOB-embed-1"]
    assert "Stopped on request" in summary["message"]


# ---------------------------------------------------------------------------
# A: stop while paused returns immediately
# ---------------------------------------------------------------------------
def test_stop_while_paused_returns_immediately(tmp_path):
    """D2's last clause. A paused worker is sitting inside
    ``wait_while_paused``; a stop there must win without waiting for another
    unit to be handed to the GPU."""
    root = _queue(tmp_path)
    queue_chunks(root, "JOB-embed-1", count=12)
    paused_seen = threading.Event()
    asked: list[int] = []

    def word_for(beat, job_id, payload):
        if (payload or {}).get("state") == "paused":
            paused_seen.set()
            return "stop"
        if job_id != protocol.idle_job_id("dev") and not asked:
            asked.append(beat)
            return "pause"
        return "none"

    transport = ControlTransport(root, word_for=word_for)
    embed = StubEmbedBackend(
        before_batch=lambda i: transport.wait_for_beats("JOB-embed-1", 2) if i == 0 else None
    )
    summary = _run(transport, tmp_path, backends=StubDevBackends(embed=embed), batch_size=4)

    assert summary["stopped"] == ["JOB-embed-1"]
    assert [t["transition"] for t in summary["control"]] == ["paused", "stopping"]
    # No batch ran after the pause landed: the stop was honoured from inside
    # the wait, not after another unit.
    assert sum(embed.batches) == 4
    assert protocol.list_pending(root) == ["JOB-embed-1"]


# ---------------------------------------------------------------------------
# FIX V-1: a pause taken OUTSIDE a job keeps beating, so it can be undone
#
# The one place the control law could not survive itself. Two of the three
# checkpoints D2 names sit where no heartbeat THREAD is running -- the top of
# the poll loop and between jobs -- and the control word only ever arrives on a
# beat. A wait that stopped beating there could not learn the resume or the stop
# that would end it, and with the production default (no bound) it never did:
# the worker sat in a wait nothing could reach, the queue's last word about it
# aged past the lost window into LOST, and the only way out was the manual
# process kill this ruling exists to abolish.
# ---------------------------------------------------------------------------
def test_a_pause_outside_a_job_keeps_beating_and_can_be_resumed(tmp_path):
    """The production default (``max_pause_s=None``) and the likeliest moment to
    pause: between jobs, which is when an operator watching the queue counts
    reaches for the button.

    Driven on a thread with a join deadline rather than inline, because the
    failure this pins is a HANG: against the pre-fix worker the run never
    returns, the beat count stays at 1, and the job is never claimed."""
    root = _queue(tmp_path)
    queue_chunks(root, "JOB-embed-1", count=8)
    idle_id = protocol.idle_job_id("dev")
    seen_rows: list[dict] = []

    def word_for(beat, job_id, payload):
        if (payload or {}).get("state") == "paused":
            paused = _paused_states(transport)
            if len(paused) == 2:
                # What the JOBS card would read at this moment (D4): the chip
                # comes off the worker's own reported state, so a pause that
                # stopped beating would leave the card saying `idle` and then
                # `LOST`.
                seen_rows.extend(control_api.worker_rows(root))
            if len(paused) >= 3:
                return "resume"
            return "none"
        # Beat 1 IS the idle beat at the top of the poll loop: nothing is
        # claimed, and no heartbeat thread exists to carry the next word.
        return "pause" if beat == 1 else "none"

    transport = ControlTransport(root, word_for=word_for)
    summary: dict = {}
    runner = threading.Thread(
        target=lambda: summary.update(
            _run(transport, tmp_path, backends=StubDevBackends(embed=StubEmbedBackend()), batch_size=4)
        ),
        daemon=True,
    )
    runner.start()
    runner.join(timeout=30.0)
    assert not runner.is_alive(), "the worker never came back from a pause taken outside a job"

    paused = _paused_states(transport)
    assert len(paused) >= 3, f"only {len(paused)} paused beat(s) -- the wait must keep beating"
    assert {j for j, p in transport.beats if (p or {}).get("state") == "paused"} == {idle_id}
    # the pause was honoured BEFORE the claim, and the resume let the run finish
    assert summary["published"] == ["JOB-embed-1"]
    assert [t["transition"] for t in summary["control"]] == ["paused", "resumed"]
    # …and the queue could say so while it was held
    assert seen_rows and seen_rows[0]["state"] == "paused"
    assert seen_rows[0]["lost"] is False


def test_a_stop_reaches_a_worker_paused_outside_a_job(tmp_path):
    """The other half of the same wedge: a pause between jobs used to make the
    worker unstoppable as well as unresumable. A stop must win from inside that
    wait, with nothing claimed and nothing to hand back."""
    root = _queue(tmp_path)
    queue_chunks(root, "JOB-embed-1", count=4)

    def word_for(beat, job_id, payload):
        if (payload or {}).get("state") == "paused":
            return "stop"
        return "pause" if beat == 1 else "none"

    transport = ControlTransport(root, word_for=word_for)
    summary: dict = {}
    runner = threading.Thread(
        target=lambda: summary.update(_run(transport, tmp_path, batch_size=4)), daemon=True
    )
    runner.start()
    runner.join(timeout=30.0)
    assert not runner.is_alive(), "a stop could not reach a worker paused outside a job"
    assert "Stopped on request" in summary["message"]
    assert [t["transition"] for t in summary["control"]] == ["paused", "stopping"]
    assert protocol.list_pending(root) == ["JOB-embed-1"], "nothing was claimed, so nothing moved"


# ---------------------------------------------------------------------------
# FIX V-4: a bounded pause expires HONESTLY, and the bound is per pause
# ---------------------------------------------------------------------------
def test_a_bounded_pause_expiry_clears_the_latch_and_says_so(tmp_path):
    """``max_pause_s`` is for a launcher that must not sit paused forever. On
    expiry the latch used to stay set: the worker went back to work while every
    later checkpoint still read ``paused``, reported ``running`` and returned
    instantly -- a worker whose card disagreed with what the GPU was doing. An
    expiry is a transition like any other, so it is logged and carried."""
    root = _queue(tmp_path)
    queue_chunks(root, "JOB-embed-1", count=8)
    # One pause, asked for on the job's first beats and never resumed: the bound
    # is the only way out.
    transport = ControlTransport(
        root,
        word_for=lambda beat, job, payload: (
            "pause" if job != protocol.idle_job_id("dev") and beat <= 2 else "none"
        ),
    )
    summary = _run(transport, tmp_path, batch_size=4, max_pause_s=0.05)

    assert [t["transition"] for t in summary["control"]] == ["paused", "pause_expired"]
    assert summary["published"] == ["JOB-embed-1"], "the run must carry on after the bound"
    # No beat claims `paused` after the expiry: the state and the behaviour agree.
    states = transport.states()
    assert "paused" in states
    assert states[-1] != "paused"


def test_the_pause_bound_is_per_pause_not_a_budget_for_the_whole_job(tmp_path):
    """It used to be computed once per job, so a second pause in the same job
    inherited an already-spent budget and returned instantly -- a pause the
    operator asked for that never happened."""
    root = _queue(tmp_path)
    queue_chunks(root, "JOB-embed-1", count=12)
    asked: list[int] = []
    asked_twice = threading.Event()

    def word_for(beat, job_id, payload):
        state = (payload or {}).get("state")
        if job_id == protocol.idle_job_id("dev"):
            return "none"
        if not asked:
            asked.append(beat)
            return "pause"
        if len(asked) == 1 and _paused_states(transport) and state == "running":
            # The first pause was honoured (a paused beat went out) and has
            # expired (this beat says running again), so this is a SECOND pause
            # inside the same job -- the one that used to be swallowed by a
            # budget the first pause had already spent.
            asked.append(beat)
            asked_twice.set()
            return "pause"
        return "none"

    transport = ControlTransport(root, word_for=word_for)
    # Hold the model at each boundary until the word for that pause is in hand:
    # the first pause before the first checkpoint, the second (asked for on the
    # first `running` beat after the expiry) before the next one.
    def hold(i):
        if i == 0:
            transport.wait_for_beats("JOB-embed-1", 2)
        elif i == 1:
            asked_twice.wait(WAIT_S)

    summary = _run(
        transport,
        tmp_path,
        backends=StubDevBackends(embed=StubEmbedBackend(before_batch=hold)),
        batch_size=4,
        max_pause_s=0.05,
    )

    assert [t["transition"] for t in summary["control"]] == [
        "paused",
        "pause_expired",
        "paused",
        "pause_expired",
    ], "the second pause must get its own bound"
    assert summary["published"] == ["JOB-embed-1"]
    assert len(_paused_states(transport)) >= 2


# ---------------------------------------------------------------------------
# FIX V-6: a beat the queue refuses FOR ITS PAYLOAD is retried bare
# ---------------------------------------------------------------------------
class _PayloadRefusingTransport:
    """A queue host whose wrapper does not know this worker's payload -- the
    rollout state, since the wrapper and the worker live on different machines
    and are deployed separately. Everything else is the real local transport."""

    def __init__(self, root, *, worker_id: str = "dev"):
        self.inner = LocalTransport(root, worker_id=worker_id)
        self.refused = 0
        self.bare = 0

    def __getattr__(self, name):  # the other six verbs, unchanged
        return getattr(self.inner, name)

    def heartbeat(self, job_id: str, *, progress: bytes | None = None) -> str:
        if progress is not None:
            self.refused += 1
            raise TransportError(
                "offload-shell would refuse the heartbeat payload: heartbeat payload carries an "
                "unknown key 'units_done'"
            )
        self.bare += 1
        return self.inner.heartbeat(job_id)


def test_a_beat_refused_for_its_payload_still_stamps_the_claim(tmp_path):
    """D3 calls the payload optional; the worker was treating it as mandatory,
    so against such a wrapper EVERY beat was lost -- no stamp, so the sandbox's
    60-minute reclaim re-queues a job that is still running, and no control word
    ever arrives again."""
    from trialerror.offload.worker import _Heartbeat

    root = _queue(tmp_path)
    queue_one(root, "JOB-a")
    protocol.server_claim(root, "JOB-a", worker_id="dev")
    stamp = protocol.claimed_dir(root) / "dev" / f"JOB-a{protocol.HEARTBEAT_SUFFIX}"
    stamp.unlink()

    transport = _PayloadRefusingTransport(root)
    progress = ProgressState(worker_id="dev")
    progress.start_job("JOB-a", kind="embed", unit="chunk", units_total=10)
    beat = _Heartbeat(transport, "JOB-a", progress=progress, control=WorkerControl())

    assert beat.beat() == "none", "the bare retry must still bring the control word back"
    assert (transport.refused, transport.bare) == (1, 1)
    assert (beat.beats, beat.payload_refusals, beat.bare_beats) == (1, 1, 1)
    assert stamp.is_file(), "the claim's stamp is what keeps reclaim off a running job"
    assert not protocol.progress_path(root, "dev", "JOB-a").exists()


def test_a_whole_run_survives_a_queue_that_refuses_every_payload(tmp_path):
    """The same fault for a whole run: the job still publishes, and the worker is
    still reachable -- the word comes back on the bare beat."""
    root = _queue(tmp_path)
    queue_chunks(root, "JOB-embed-1", count=8)
    transport = _PayloadRefusingTransport(root)
    summary = _run(transport, tmp_path, batch_size=4)

    assert summary["published"] == ["JOB-embed-1"]
    assert transport.refused >= 2 and transport.bare >= 2
    # No progress file ever reached the queue (every payload was refused), which
    # is the honest outcome: a claim the card can see, with no detail.
    assert all(r.get("state") in (None, "unknown") for r in control_api.worker_rows(root))


def test_the_control_latches_are_latches(tmp_path):
    """The semantics, unit-level, because the whole control surface rests on
    them: ``none`` is "no new instruction", never "carry on"; only ``resume``
    clears a pause; nothing clears a stop."""
    control = WorkerControl()
    assert not control.paused and not control.stopping and control.control_seen is None

    control.observe("none")
    assert not control.paused
    control.observe("pause")
    assert control.paused and control.control_seen == "pause"
    control.observe("none")
    assert control.paused, "`none` must not resume a paused worker"
    control.observe("resume")
    assert not control.paused
    control.observe("stop")
    assert control.stopping
    control.observe("resume")
    assert control.stopping, "there is no un-stop"
    assert [t["transition"] for t in control.transitions] == ["paused", "resumed", "stopping"]

    # an unrecognised reply is not an instruction
    other = WorkerControl()
    other.observe("kill")
    other.observe("")
    other.observe(None)
    assert not other.paused and not other.stopping and other.transitions == []


# ---------------------------------------------------------------------------
# A: a stale CONTROL is ignored and reported
# ---------------------------------------------------------------------------
def test_a_stale_control_request_is_ignored_and_reported(tmp_path):
    """D1. A laptop switched off for a week must not come back and stop itself
    over something asked for last Tuesday -- and the request must still be
    VISIBLE, because a silently dropped instruction is worse than an ignored
    one."""
    root = _queue(tmp_path)
    queue_chunks(root, "JOB-embed-1", count=4)
    protocol.write_json(
        protocol.control_path(root, "dev"),
        {
            "schema": protocol.CONTROL_SCHEMA,
            "request": "stop",
            "by_launch": "LNCH-old",
            "ts": "2020-01-01T00:00:00.000Z",
            "job_id": None,
        },
    )
    assert control_api.control_word(root, "dev") == "none"
    record = control_api.read_control(root, "dev")
    assert record["request"] == "stop" and record["stale"] is True and record["age_s"] > 3600

    # …and the worker drains the queue as if nothing had been asked.
    summary = _run(LocalTransport(root), tmp_path, batch_size=4)
    assert summary["published"] == ["JOB-embed-1"]
    assert summary["stopped"] == []


def test_a_fresh_control_request_is_obeyed_through_the_real_local_transport(tmp_path):
    """The same path without the scripted fake: the word comes off a real
    ``CONTROL.json`` through ``LocalTransport``, which is the in-process mirror
    of the wrapper the restricted key talks to."""
    root = _queue(tmp_path)
    queue_chunks(root, "JOB-embed-1", count=8)
    control_api.request_control(
        root, worker_id="dev", request="stop", by_launch="LNCH-1", require_worker=False
    )
    assert control_api.control_word(root, "dev") == "stop"

    summary = _run(LocalTransport(root), tmp_path, batch_size=4)
    assert summary["stopped"] == [] and summary["published"] == []
    assert "Stopped on request" in summary["message"]
    assert protocol.list_pending(root) == ["JOB-embed-1"]


def test_a_short_ttl_makes_the_transport_report_none(tmp_path):
    """``control_ttl_s`` is a knob, not a constant: the acceptance for "stale
    is ignored" would otherwise have to wait an hour."""
    root = _queue(tmp_path)
    queue_one(root, "JOB-a")
    protocol.server_claim(root, "JOB-a", worker_id="dev")
    control_api.request_control(
        root, worker_id="dev", request="pause", by_launch="LNCH-1", require_worker=False
    )
    assert LocalTransport(root).heartbeat("JOB-a") == "pause"
    assert LocalTransport(root, control_ttl_s=-1.0).heartbeat("JOB-a") == "none"


# ---------------------------------------------------------------------------
# FIX V-2 / V-3: a control request has a lifecycle, and the sandbox closes it
#
# The first version of this module could write a request and never end one. Two
# halves of the law broke on that: `resume` was refused for the whole TTL as
# "still pending" (an assertion the code could not make and which was false once
# the worker had read the pause), and a `stop` outlived the worker it stopped, so
# every restart inside the hour stopped itself -- the exact manoeuvre the ruling
# was written for.
# ---------------------------------------------------------------------------
def _beat_with(root: Path, *, worker_id: str = "dev", job_id: str | None = None, **payload):
    """One real heartbeat carrying a D3 payload, through the in-process mirror of
    the wrapper -- i.e. the worker's side of the conversation, without a worker."""
    target = job_id or protocol.idle_job_id(worker_id)
    LocalTransport(root, worker_id=worker_id).heartbeat(
        target,
        progress=control_api.encode_progress({"worker_id": worker_id, **payload}),
    )


def test_a_resume_is_accepted_once_the_worker_has_read_the_pause(tmp_path):
    """Acceptance F's middle step, which used to be unperformable: pause, watch
    it land, resume. The refusal's own sentence claimed the worker had not read
    the request -- while the worker was reporting that it had."""
    root = _queue(tmp_path)
    _beat_with(root, state="idle")
    control_api.request_control(
        root, worker_id="dev", request="pause", by_launch="LNCH-1", require_worker=False
    )
    # the worker picks it up and says so, which is the only evidence there is
    _beat_with(root, state="paused", control_seen="pause")
    row = control_api.worker_row(root, "dev")
    assert row["control_seen"] == "pause"
    assert control_api.control_was_read(row, control_api.read_control(root, "dev")) is True

    record = control_api.request_control(
        root, worker_id="dev", request="resume", by_launch="LNCH-2", require_worker=False
    )
    assert record["request"] == "resume"
    assert control_api.control_word(root, "dev") == "resume"


def test_a_resume_overtakes_a_pause_the_worker_has_not_read_yet(tmp_path):
    """And an operator who changes their mind BEFORE the worker has looked is not
    made to wait either: a resume over an unread pause is the whole point of
    having a resume. Only an identical unread word is refused."""
    root = _queue(tmp_path)
    _beat_with(root, state="idle")
    control_api.request_control(
        root, worker_id="dev", request="pause", by_launch="LNCH-1", require_worker=False
    )
    record = control_api.request_control(
        root, worker_id="dev", request="resume", by_launch="LNCH-1", require_worker=False
    )
    assert record["request"] == "resume"


def test_an_unread_repeat_of_the_same_word_is_still_refused_and_says_why(tmp_path):
    """D5's "request already pending" refusal, narrowed to the one case it can
    honestly describe -- and now the sentence is true: it is the worker's own
    ``control_seen``, on a beat later than the request, that decides."""
    root = _queue(tmp_path)
    _beat_with(root, state="idle")
    control_api.request_control(
        root, worker_id="dev", request="pause", by_launch="LNCH-1", require_worker=False
    )
    with pytest.raises(control_api.ControlError) as excinfo:
        control_api.request_control(
            root, worker_id="dev", request="pause", by_launch="LNCH-1", require_worker=False
        )
    message = str(excinfo.value)
    assert "unread" in message and "no beat has reported reading it yet" in message
    assert "--clear" in message

    # …and once a beat HAS reported reading it, asking again is allowed (the
    # worker may have un-paused on a bound, and the operator may want it held).
    _beat_with(root, state="paused", control_seen="pause")
    again = control_api.request_control(
        root, worker_id="dev", request="pause", by_launch="LNCH-1", require_worker=False
    )
    assert again["request"] == "pause"


def test_a_stop_does_not_outlive_the_worker_it_stopped(tmp_path):
    """The restart case, measured as three consecutive runs against ONE stop.

    Before this, runs 2 and 3 stopped themselves with nothing claimed and no
    chunk embedded, for the full hour: the queue, the CLI and the dashboard had
    no way to clear a request, and only a `stop` was accepted as a follow-up --
    which rewrites the same word."""
    root = _queue(tmp_path)
    queue_chunks(root, "JOB-embed-1", count=8)
    control_api.request_control(
        root, worker_id="dev", request="stop", by_launch="LNCH-1", require_worker=False
    )

    first = _run(LocalTransport(root), tmp_path, batch_size=4)
    assert "Stopped on request" in first["message"]
    assert protocol.list_pending(root) == ["JOB-embed-1"]
    # the worker acted on it and left, so the request is spent
    row = control_api.worker_row(root, "dev")
    assert row["control_seen"] == "stop"
    assert row["state"] in control_api.WORKER_GONE_STATES
    reaped = control_api.reap_spent_controls(root)
    assert [r["request"] for r in reaped] == ["stop"]
    assert not protocol.control_path(root, "dev").is_file()

    second = _run(LocalTransport(root), tmp_path / "b", batch_size=4)
    assert second["published"] == ["JOB-embed-1"], "a restart inside the TTL must start clean"


def test_a_stop_a_running_worker_is_ignoring_is_never_reaped(tmp_path):
    """The evidence the doctor's FAIL needs, protected from the reaper. A worker
    that has ACKNOWLEDGED an hour-old stop and is still reporting work is the one
    defect ``worker_heartbeat_stale`` fails on, so that request stays on disk --
    spent is narrower than read on purpose."""
    root = _queue(tmp_path)
    queue_one(root, "JOB-a")
    protocol.server_claim(root, "JOB-a", worker_id="dev")
    protocol.write_json(
        protocol.control_path(root, "dev"),
        {
            "schema": protocol.CONTROL_SCHEMA,
            "request": "stop",
            "by_launch": "LNCH-1",
            "ts": "2020-01-01T00:00:00.000Z",
            "job_id": None,
        },
    )
    _beat_with(root, job_id="JOB-a", state="running", control_seen="stop", units_done=3)

    record = control_api.read_control(root, "dev")
    row = control_api.worker_row(root, "dev")
    assert control_api.control_was_read(row, record) is True
    assert control_api.control_is_spent(row, record) is False
    assert control_api.reap_spent_controls(root) == []
    assert protocol.control_path(root, "dev").is_file()


def test_a_resume_is_spent_the_moment_it_is_read(tmp_path):
    """Nothing about "carry on" stands after the worker has carried on, so a
    resume is reaped as soon as it is acknowledged -- otherwise the next pause
    would be compared against a word that has already done its work."""
    root = _queue(tmp_path)
    _beat_with(root, state="paused", control_seen="pause")
    control_api.request_control(
        root, worker_id="dev", request="resume", by_launch="LNCH-1", require_worker=False
    )
    _beat_with(root, state="running", control_seen="resume")
    reaped = control_api.reap_spent_controls(root)
    assert [r["request"] for r in reaped] == ["resume"]
    assert control_api.control_word(root, "dev") == "none"


def test_a_request_no_worker_ever_read_is_left_alone_by_the_reaper(tmp_path):
    """``--even-if-absent`` leaves a request waiting for a worker that has not
    started. The reaper must not tidy that away -- it is the one case where a
    pending request is exactly what the operator asked for."""
    root = _queue(tmp_path)
    control_api.request_control(
        root, worker_id="dev", request="pause", by_launch="LNCH-1", require_worker=False
    )
    assert control_api.reap_spent_controls(root) == []
    assert control_api.control_word(root, "dev") == "pause"


# ---------------------------------------------------------------------------
# A / B (python port): the payload cap and a malformed payload
# ---------------------------------------------------------------------------
def test_the_payload_cap_is_refused_without_touching_the_stamp(tmp_path):
    """The refusal order the wrapper uses, mirrored in-process: an oversized
    payload fails the verb and the stamp is left exactly as it was. A status
    file the sandbox cannot trust must not also cost the job its claim."""
    root = _queue(tmp_path)
    queue_one(root, "JOB-a")
    protocol.server_claim(root, "JOB-a", worker_id="dev")
    transport = LocalTransport(root)
    stamp_path = protocol.claimed_dir(root) / "dev" / f"JOB-a{protocol.HEARTBEAT_SUFFIX}"
    before = stamp_path.read_text(encoding="utf-8")

    oversized = b'{"last_error":"' + b"x" * 5000 + b'"}'
    with pytest.raises(TransportError, match="cap"):
        transport.heartbeat("JOB-a", progress=oversized)
    assert stamp_path.read_text(encoding="utf-8") == before
    assert not protocol.progress_path(root, "dev", "JOB-a").exists()


@pytest.mark.parametrize(
    "payload,why",
    [
        (b"[1,2]", "a JSON array is not an object"),
        (b'"just a string"', "a bare JSON string is not an object"),
        (b'{"nope":1}', "an unknown key"),
        (b'{"worker_id":"dev","units":1}', "a near-miss of a known key is still unknown"),
    ],
    ids=["array", "string", "unknown-key", "near-miss-key"],
)
def test_a_malformed_payload_is_refused_without_touching_the_stamp(tmp_path, payload, why):
    root = _queue(tmp_path)
    queue_one(root, "JOB-a")
    protocol.server_claim(root, "JOB-a", worker_id="dev")
    stamp_path = protocol.claimed_dir(root) / "dev" / f"JOB-a{protocol.HEARTBEAT_SUFFIX}"
    before = stamp_path.read_text(encoding="utf-8")
    with pytest.raises(TransportError):
        LocalTransport(root).heartbeat("JOB-a", progress=payload)
    assert stamp_path.read_text(encoding="utf-8") == before, why
    assert not protocol.progress_path(root, "dev", "JOB-a").exists()


def test_the_key_check_constrains_identifier_shaped_keys_only(tmp_path):
    """Where the wrapper's key rule STOPS, stated so nobody reads it as more
    than it is. ``grep -o '"[A-Za-z_][A-Za-z0-9_]*"[[:blank:]]*:'`` -- and the
    Python port, deliberately the same regex -- only sees identifier-shaped
    keys, so a key with a space in it is not a key token to either half and
    both store the payload. That is a bounded outcome, not a hole: the file is
    only ever read as data (``json.loads`` into a dict, unknown keys dropped by
    the row builder), nothing in it is ever evaluated, and the size cap still
    applies. The check exists to catch a worker and a sandbox drifting apart on
    the keys they BOTH use, and a key neither side has a name for cannot drift.
    """
    from trialerror.offload.shell import progress_payload_refusal

    assert progress_payload_refusal(b'{"worker_id":"dev","rm -rf /":1}') is None
    assert progress_payload_refusal(b'{"worker_id":"dev","units":1}') is not None

    root = _queue(tmp_path)
    queue_one(root, "JOB-a")
    protocol.server_claim(root, "JOB-a", worker_id="dev")
    LocalTransport(root).heartbeat("JOB-a", progress=b'{"worker_id":"dev","rm -rf /":1}')
    stored = control_api.read_progress(root, "dev", "JOB-a")
    assert stored["worker_id"] == "dev"
    row = next(r for r in control_api.worker_rows(root) if r["worker_id"] == "dev")
    assert "rm -rf /" not in row


def test_an_absent_payload_is_not_a_refusal(tmp_path):
    """Every caller before C-0097 sent no payload. That must still stamp, still
    print a word, and write no progress file."""
    root = _queue(tmp_path)
    queue_one(root, "JOB-a")
    protocol.server_claim(root, "JOB-a", worker_id="dev")
    assert LocalTransport(root).heartbeat("JOB-a") == "none"
    assert not protocol.progress_path(root, "dev", "JOB-a").exists()


def test_the_python_side_refuses_what_the_wrapper_cannot_check(tmp_path):
    """The documented split: the wrapper checks size, object-shape and the key
    allowlist; the side that BUILDS the payload also checks the per-value rules
    D3 names, because POSIX sh without a JSON parser cannot."""
    with pytest.raises(control_api.ControlError, match="finite"):
        control_api.encode_progress({"worker_id": "dev", "eta_s": float("inf")})
    with pytest.raises(control_api.ControlError, match="256-character"):
        control_api.encode_progress({"worker_id": "x" * 300})
    with pytest.raises(control_api.ControlError, match="state must be one of"):
        control_api.encode_progress({"worker_id": "dev", "state": "dancing"})
    with pytest.raises(control_api.ControlError, match="unknown key"):
        control_api.encode_progress({"settings": {"gpu": "yes"}})

    # last_error is TRUNCATED rather than refused: the most useful field on the
    # card is also the least predictable in length, and losing the whole
    # heartbeat to a long traceback would lose the status when it matters most.
    raw = control_api.encode_progress({"worker_id": "dev", "last_error": "e" * 900})
    assert len(json.loads(raw.decode())["last_error"]) == control_api.MAX_PROGRESS_STRING_CHARS


# ---------------------------------------------------------------------------
# FIX V-5: the READER validates, and the dashboard cannot emit non-JSON
#
# The wrapper on the queue host has no JSON parser, so it can only check that a
# payload is an object, under the cap, with known keys -- and it stores the bytes
# it was handed. Nothing validated on read, so a payload that passed those three
# rules reached the card and the bundle verbatim: a nested object drew as
# "[OBJECT OBJECT]", and `Infinity`/`NaN` made the whole /dashboard/api/all body
# unparseable by any browser, taking out every card rather than one.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "stored,why",
    [
        (b'{"state":{"state":"running"},"units_done":1}', "a nested object where a word belongs"),
        (b'{"state":"running","eta_s":Infinity}', "a non-finite number"),
        (b'{"state":"running","pace_s_per_unit":NaN}', "a non-number"),
        (b'{"state":"dancing"}', "a state outside the vocabulary"),
    ],
    ids=["nested-object", "infinity", "nan", "unknown-state"],
)
def test_a_payload_the_wrapper_cannot_check_is_refused_on_read(tmp_path, stored, why):
    """Each of these passes all three rules the wrapper can apply -- object,
    under the cap, known keys -- so the only place left to catch them is the
    read, which is the half that is supposed to distrust this input."""
    root = _queue(tmp_path)
    queue_one(root, "JOB-a")
    protocol.server_claim(root, "JOB-a", worker_id="dev")
    from trialerror.offload.shell import progress_payload_refusal

    assert progress_payload_refusal(stored) is None, f"the wrapper accepts this one: {why}"
    protocol.progress_path(root, "dev", "JOB-a").write_bytes(stored)

    payload = control_api.read_progress(root, "dev", "JOB-a")
    assert "unreadable" in payload, why
    row = control_api.worker_row(root, "dev")
    assert row["state"] == "unknown"
    assert row["eta_s"] is None and row["pace_s_per_unit"] is None


def test_an_over_long_last_error_is_truncated_on_read_too(tmp_path):
    """D3 caps it at 256 characters. The builder truncates; the reader used to
    forward whatever was on disk, so a 2,000-character value written by the key
    reached the card -- where it is drawn in full."""
    root = _queue(tmp_path)
    queue_one(root, "JOB-a")
    protocol.server_claim(root, "JOB-a", worker_id="dev")
    protocol.progress_path(root, "dev", "JOB-a").write_text(
        json.dumps({"state": "running", "last_error": "b" * 2000}), encoding="utf-8"
    )
    row = control_api.worker_row(root, "dev")
    assert len(row["last_error"]) == control_api.MAX_PROGRESS_STRING_CHARS
    assert row["state"] == "running", "a truncation is not a refusal"


def test_an_unknown_key_on_read_is_dropped_not_refused(tmp_path):
    """The boundary the key check leaves open (a key that is not
    identifier-shaped is not a key token to either half) stays a dropped key
    rather than a lost row: the payload is data, and the row builder has always
    ignored what it has no name for."""
    root = _queue(tmp_path)
    queue_one(root, "JOB-a")
    protocol.server_claim(root, "JOB-a", worker_id="dev")
    protocol.progress_path(root, "dev", "JOB-a").write_text(
        '{"worker_id":"dev","state":"running","rm -rf /":1}', encoding="utf-8"
    )
    payload = control_api.read_progress(root, "dev", "JOB-a")
    assert payload == {"worker_id": "dev", "state": "running"}


def test_the_dashboard_serialiser_cannot_emit_infinity_or_nan():
    """Belt and braces for the same finding, one layer down: every JSON body the
    dashboard sends goes through one serialiser, and that serialiser refuses to
    write what ``JSON.parse`` cannot read. One bad number used to cost the whole
    console bundle, not the card that carried it."""
    from trialerror.dashboard.serve import json_text

    body = json_text({"workers": [{"eta_s": float("inf"), "pace_s_per_unit": float("nan")}]})
    assert "Infinity" not in body and "NaN" not in body
    assert json.loads(body) == {"workers": [{"eta_s": None, "pace_s_per_unit": None}]}
    # …and an ordinary body is untouched
    assert json.loads(json_text({"a": 1, "b": [1.5, None], "c": "x"})) == {
        "a": 1, "b": [1.5, None], "c": "x",
    }


# ---------------------------------------------------------------------------
# D3: the progress payload and the idle slot
# ---------------------------------------------------------------------------
def test_the_progress_payload_carries_what_d3_names(tmp_path):
    root = _queue(tmp_path)
    queue_chunks(root, "JOB-embed-1", count=12)
    transport = ControlTransport(root)
    # Hold the model until the job has beaten twice, so a `running` beat is
    # guaranteed rather than raced for: the first beat of a claim can still say
    # `claiming`, which is correct and is not the payload under test here.
    embed = StubEmbedBackend(
        before_batch=lambda i: transport.wait_for_beats("JOB-embed-1", 2) if i == 1 else None
    )
    _run(transport, tmp_path, backends=StubDevBackends(embed=embed), batch_size=4)

    running = [p for _j, p in transport.beats if (p or {}).get("state") == "running"]
    assert running, "no running beat was sent"
    sample = running[-1]
    assert sample["worker_id"] == "dev"
    assert sample["kind"] == "embed"
    assert sample["job_id"] == "JOB-embed-1"
    assert sample["unit"] == "chunk"
    assert sample["units_total"] == 12
    assert 0 <= sample["units_done"] <= 12
    assert sample["settings"]["batch_size"] == 4
    assert sample["settings"]["model_key"] == "stub-embed"
    assert sample["settings"]["resident_backends"] == ["embed"]
    assert set(sample) <= set(control_api.PROGRESS_KEYS)


def test_an_idle_worker_says_so_under_the_synthetic_id(tmp_path):
    """D3's "idle worker" vs "no worker". Without this beat the two are the
    same empty claim directory, and an operator cannot tell a drained queue
    from a laptop somebody shut."""
    root = _queue(tmp_path)
    transport = ControlTransport(root)
    summary = _run(transport, tmp_path)
    assert summary["message"].startswith("Queue empty")

    idle_id = protocol.idle_job_id("dev")
    assert [j for j, _p in transport.beats] == [idle_id, idle_id]
    # The first beat is the live reading -- alive, nothing claimed -- and the
    # last one is terminal (FIX V-9): the run's process has ended, and the file
    # it leaves behind has to say which of the two it is.
    assert transport.states() == ["idle", "exited"]
    payload = control_api.read_progress(root, "dev", idle_id)
    assert payload["state"] == "exited"
    assert payload["worker_id"] == "dev"
    rows = control_api.worker_rows(root)
    assert [r["state"] for r in rows] == ["exited"]
    assert rows[0]["job_id"] is None, "the synthetic id must not be printed as a job"
    assert rows[0]["lost"] is False


# ---------------------------------------------------------------------------
# FIX V-9: an exited worker says so, and a held claim outranks the idle slot
# ---------------------------------------------------------------------------
def test_the_last_beat_of_a_run_is_a_terminal_state(tmp_path):
    """Every run ends with a beat, and the file it leaves is read for the whole
    lost window -- eleven minutes at the default interval. Reporting ``idle``
    there said "alive, nothing to do" about a process that had exited, so the
    card could not tell a drained queue from a laptop somebody shut. The worker
    cannot delete its own progress file (no verb of the seven-verb contract
    can), so the fix is a word the card can label."""
    root = _queue(tmp_path)
    queue_chunks(root, "JOB-embed-1", count=4)
    transport = ControlTransport(root)
    summary = _run(transport, tmp_path, batch_size=4)

    assert summary["published"] == ["JOB-embed-1"]
    assert transport.states()[0] == "idle", "the first beat is still the live reading"
    assert transport.states()[-1] == "exited"
    row = control_api.worker_row(root, "dev")
    assert row["state"] == "exited"
    assert row["job_id"] is None
    assert row["state"] in control_api.WORKER_GONE_STATES


class _ReturnRefusingTransport(ControlTransport):
    """A queue that takes the stop but cannot take the claim back -- the dropped
    connection the sandbox's 60-minute reclaim exists for."""

    def return_job(self, job_id: str) -> None:
        raise TransportError("offload: `return` failed on the queue host (connection dropped)")


def test_a_claim_the_worker_could_not_hand_back_outranks_the_idle_slot(tmp_path):
    """The row the card draws after a stop whose ``return`` failed. The run's
    final beat is always the NEWEST progress file, so it used to replace the
    held job's row -- hiding the claim the sandbox still has to reclaim, and
    delaying ``worker_heartbeat_stale``'s warn by the whole lost window."""
    root = _queue(tmp_path)
    queue_chunks(root, "JOB-embed-1", count=12)
    transport = _ReturnRefusingTransport(
        root, word_for=lambda beat, job, payload: "stop" if job == "JOB-embed-1" else "none"
    )
    embed = StubEmbedBackend(
        before_batch=lambda i: transport.wait_for_beats("JOB-embed-1", 2) if i == 0 else None
    )
    summary = _run(transport, tmp_path, backends=StubDevBackends(embed=embed), batch_size=4)

    assert summary["stopped"] == ["JOB-embed-1"]
    # the claim is still ours, because the return could not be delivered
    assert [c["job_id"] for c in protocol.list_claims(root)] == ["JOB-embed-1"]
    assert protocol.list_pending(root) == []
    # …and the card says so, rather than showing a tidy `exited` worker
    row = control_api.worker_row(root, "dev")
    assert row["progress_job_id"] == "JOB-embed-1"
    assert row["job_id"] == "JOB-embed-1"
    assert row["state"] == "stopping"


def test_a_published_job_leaves_no_stale_worker_row_for_it(tmp_path):
    """Publish deletes the job's progress file (and so does return). Left
    behind, a finished job would sit on the card as a live worker row until it
    aged into ``lost`` -- a reading about the worker, not about the job."""
    root = _queue(tmp_path)
    queue_chunks(root, "JOB-embed-1", count=4)
    _run(ControlTransport(root), tmp_path, batch_size=4)
    worker_dir = protocol.claimed_dir(root) / "dev"
    assert not (worker_dir / f"JOB-embed-1{protocol.PROGRESS_SUFFIX}").exists()
    rows = control_api.worker_rows(root)
    assert [r["job_id"] for r in rows] == [None]


def test_a_progress_file_is_not_mistaken_for_a_claim(tmp_path):
    """``*.progress.json`` and ``CONTROL.json`` share the claim directory and
    the ``.json`` extension with the manifests. A reader that counted them
    would report a worker's own status file as a held job -- and ``reclaim``
    would then try to return it."""
    root = _queue(tmp_path)
    queue_one(root, "JOB-a")
    protocol.server_claim(root, "JOB-a", worker_id="dev")
    LocalTransport(root).heartbeat(
        "JOB-a", progress=control_api.encode_progress({"worker_id": "dev", "state": "running"})
    )
    control_api.request_control(
        root, worker_id="dev", request="pause", by_launch="LNCH-1", require_worker=False
    )
    assert [c["job_id"] for c in protocol.list_claims(root)] == ["JOB-a"]
    assert protocol.counts(root)["claimed"] == 1
    assert protocol.reclaim_stale(root, expiry_s=10_000) == []


def test_a_pace_and_eta_appear_once_there_is_something_to_measure():
    """``pace_s_per_unit`` is the rolling mean of the last 20 units and the ETA
    is that pace times what is left -- deliberately the simplest arithmetic
    that can be right, because an ETA that models more than "the recent past,
    repeated" is confidently wrong the moment the job changes shape."""
    progress = ProgressState(worker_id="dev")
    progress.start_job("JOB-1", kind="embed", unit="chunk", units_total=100)
    first = progress.snapshot()
    assert "pace_s_per_unit" not in first and "eta_s" not in first
    progress.units_done(10)
    later = progress.snapshot(control_seen="pause")
    assert later["units_done"] == 10
    assert later["pace_s_per_unit"] >= 0.0
    assert later["eta_s"] >= 0.0
    assert later["control_seen"] == "pause"


# ---------------------------------------------------------------------------
# D7: the measured batch default
# ---------------------------------------------------------------------------
def test_the_embed_batch_default_is_the_measured_four():
    """D7. 64 was a guess about VRAM; 4 is a measurement. The constant is the
    one number that decides the memory high-water mark of a run, so it is
    pinned here as well as in the CLI's help text."""
    assert DEFAULT_EMBED_BATCH_SIZE == 4


def test_the_batch_size_reaches_the_model_as_the_per_call_count(tmp_path):
    root = _queue(tmp_path)
    queue_chunks(root, "JOB-embed-1", count=10)
    embed = StubEmbedBackend()
    _run(ControlTransport(root), tmp_path, backends=StubDevBackends(embed=embed))
    assert embed.batches == [4, 4, 2], "the default must be handed to the GPU four at a time"


# ---------------------------------------------------------------------------
# D9: unload the idle backend on a kind switch
# ---------------------------------------------------------------------------
def test_a_kind_switch_unloads_the_idle_backend(tmp_path):
    """D9, measured live: both stages resident at once left 0.9 GB of RAM free
    on the machine this subsystem runs on. The policy is observable through
    ``close()`` -- which on the real embedding backend exits the driver
    subprocess and gives the VRAM back."""
    embed = ClosableStubEmbedBackend()
    backends = StubDevBackends(embed=embed)
    resident = ResidentBackends(backends)

    assert resident.for_stage("embed") is embed
    assert resident.resident_kinds() == ["embed"]
    ocr = resident.for_stage("ocr")
    assert embed.closes == 1, "the embed driver must be unloaded before marker starts"
    assert resident.resident_kinds() == ["ocr"]
    assert resident.for_stage("ocr") is ocr, "same kind twice must not reload"
    assert embed.closes == 1

    with pytest.raises(RuntimeError, match="unknown offload stage"):
        resident.for_stage("transcribe")


def test_keep_resident_holds_both_stages(tmp_path):
    """The escape hatch for a machine with the memory, and the proof that the
    default is a POLICY rather than a property of the backend."""
    embed = ClosableStubEmbedBackend()
    resident = ResidentBackends(StubDevBackends(embed=embed), keep_resident=True)
    resident.for_stage("embed")
    resident.for_stage("ocr")
    assert embed.closes == 0
    assert resident.resident_kinds() == ["ocr", "embed"]
    resident.close()
    assert embed.closes == 1


def test_the_kind_switch_happens_in_a_real_two_stage_run(tmp_path):
    """The policy where it actually bites: a queue holding one job of each
    kind. ``--keep-resident`` is the difference between one unload and none."""
    root = _queue(tmp_path)
    queue_chunks(root, "JOB-embed-1", count=4)
    queue_one(root, "JOB-ocr-1", stage="ocr", payload=b"page one")
    embed = ClosableStubEmbedBackend()
    summary = _run(
        ControlTransport(root), tmp_path, backends=StubDevBackends(embed=embed), batch_size=4
    )
    assert sorted(summary["published"]) == ["JOB-embed-1", "JOB-ocr-1"]
    assert embed.closes >= 1

    root2 = protocol.ensure_layout(tmp_path / "offload2")
    queue_chunks(root2, "JOB-embed-1", count=4)
    queue_one(root2, "JOB-ocr-1", stage="ocr", payload=b"page one")
    embed2 = ClosableStubEmbedBackend()
    _run(
        ControlTransport(root2),
        tmp_path / "b",
        backends=StubDevBackends(embed=embed2),
        batch_size=4,
        keep_resident=True,
    )
    assert embed2.closes == 1, "only the run's own teardown should have closed it"
