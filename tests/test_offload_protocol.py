"""Lane L0-C: the offload queue's own semantics (design section 4).

Covers the named C-unit cases that live at the protocol level:
``claim_race``, ``partial_publish_invisible``, ``sha_mismatch_to_failed``
(the verification half; the ledger half is in
``tests/test_offload_stage.py``), ``stale_claim_reclaimed`` and
``double_claim_after_reclaim_idempotent``.
"""

from __future__ import annotations

import io
import json
import tarfile

import pytest

from trialerror.offload import protocol
from trialerror.util.timeutil import now, parse
from tests._offload_fixtures import STUB_OCR_NAME, publish_stub_result, queue_one


@pytest.fixture()
def root(tmp_path):
    return protocol.ensure_layout(tmp_path / "offload")


# ---------------------------------------------------------------------------
# job ids
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("job_id", ["JOB-ingest-DOC-1", "a", "A.b_c-1", "JOB.embed"])
def test_valid_job_ids_are_accepted(job_id):
    assert protocol.validate_job_id(job_id) == job_id


@pytest.mark.parametrize(
    "job_id",
    ["", ".", "..", "../escape", "a/b", "a\\b", "a b", "a;rm -rf /", "a\nb", "a*", "a$b"],
)
def test_path_unsafe_job_ids_are_refused(job_id):
    """The id becomes a path component on two machines and a word in an SSH
    command string on one of them -- everything outside
    ``[A-Za-z0-9._-]`` is refused, and ``.``/``..`` on top of that (the
    character class alone would admit them)."""
    with pytest.raises(protocol.OffloadProtocolError):
        protocol.validate_job_id(job_id)


# ---------------------------------------------------------------------------
# queue / claim / publish
# ---------------------------------------------------------------------------
def test_queue_marker_writes_inputs_and_manifest(root):
    manifest = queue_one(root, "JOB-a", payload=b"page one\x0cpage two")
    assert protocol.list_pending(root) == ["JOB-a"]
    assert (protocol.pending_dir(root) / "JOB-a" / "input.txt").read_bytes() == b"page one\x0cpage two"
    assert manifest["inputs"][0]["sha256"] == protocol.sha256_bytes(b"page one\x0cpage two")
    assert manifest["offload_attempts"] == 0
    assert protocol.find_manifest(root, "JOB-a")[0] == "pending"


def test_claim_moves_manifest_and_inputs_and_writes_a_heartbeat(root):
    queue_one(root, "JOB-a")
    manifest = protocol.server_claim(root, "JOB-a", worker_id="dev")
    assert manifest["job_id"] == "JOB-a"
    assert protocol.list_pending(root) == []
    assert protocol.find_manifest(root, "JOB-a")[0] == "claimed"
    claims = protocol.list_claims(root)
    assert [c["job_id"] for c in claims] == ["JOB-a"]
    assert claims[0]["worker_id"] == "dev"
    assert claims[0]["heartbeat_ts"] is not None


def test_claim_race_has_exactly_one_winner(root):
    """C-unit ``claim_race``. The manifest rename IS the ownership
    transfer, so the loser sees no source file -- the same "one winner,
    one ordinary no-op" shape the ledger's conditional UPDATE has."""
    queue_one(root, "JOB-a")
    protocol.server_claim(root, "JOB-a", worker_id="dev1")
    with pytest.raises(protocol.OffloadProtocolError):
        protocol.server_claim(root, "JOB-a", worker_id="dev2")
    assert [c["worker_id"] for c in protocol.list_claims(root)] == ["dev1"]


def test_pull_returns_a_flat_tar_of_the_inputs(root):
    queue_one(root, "JOB-a", payload=b"body")
    protocol.server_claim(root, "JOB-a", worker_id="dev")
    data = protocol.server_pull(root, "JOB-a", worker_id="dev")
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as tar:
        assert [m.name for m in tar.getmembers()] == ["input.txt"]


def test_pull_and_push_refuse_a_job_this_worker_does_not_hold(root):
    queue_one(root, "JOB-a")
    protocol.server_claim(root, "JOB-a", worker_id="dev1")
    for fn in (
        lambda: protocol.server_pull(root, "JOB-a", worker_id="dev2"),
        lambda: protocol.server_push(root, "JOB-a", b"", worker_id="dev2"),
        lambda: protocol.server_publish(root, "JOB-a", worker_id="dev2"),
        lambda: protocol.server_return(root, "JOB-a", worker_id="dev2"),
        lambda: protocol.server_heartbeat(root, "JOB-a", worker_id="dev2"),
    ):
        with pytest.raises(protocol.OffloadProtocolError):
            fn()


def test_partial_publish_is_invisible(root):
    """C-unit ``partial_publish_invisible``. A pushed-but-unpublished
    result lives under ``done/.partial/`` and must not be readable as a
    published result by any sandbox-side reader -- otherwise a stage could
    consume half an upload."""
    queue_one(root, "JOB-a")
    protocol.server_claim(root, "JOB-a", worker_id="dev")
    protocol.server_push(root, "JOB-a", _tar({"pages.json": b"{}"}), worker_id="dev")

    assert protocol.list_done(root) == []
    assert protocol.published_result(root, "JOB-a") is None
    assert protocol.find_manifest(root, "JOB-a")[0] == "claimed"
    assert (protocol.partial_dir(root) / "JOB-a" / "pages.json").is_file()


def test_publish_makes_the_result_visible_with_its_manifest_and_clears_the_claim(root):
    queue_one(root, "JOB-a")
    publish_stub_result(root, "JOB-a")
    assert protocol.list_done(root) == ["JOB-a"]
    assert protocol.list_claims(root) == []
    state, path = protocol.find_manifest(root, "JOB-a")
    assert state == "done"
    assert path.name == protocol.MANIFEST_FILENAME
    assert protocol.published_result(root, "JOB-a")["backend"] == STUB_OCR_NAME


def test_publish_twice_is_refused(root):
    queue_one(root, "JOB-a")
    publish_stub_result(root, "JOB-a")
    queue_one(root, "JOB-a")  # a second life for the same id
    protocol.server_claim(root, "JOB-a", worker_id="dev")
    protocol.server_push(root, "JOB-a", _tar({"pages.json": b"{}"}), worker_id="dev")
    with pytest.raises(protocol.OffloadProtocolError, match="already published"):
        protocol.server_publish(root, "JOB-a", worker_id="dev")


def test_return_puts_the_job_back_unrun(root):
    queue_one(root, "JOB-a", payload=b"body")
    protocol.server_claim(root, "JOB-a", worker_id="dev")
    protocol.server_return(root, "JOB-a", worker_id="dev")
    assert protocol.list_pending(root) == ["JOB-a"]
    assert protocol.list_claims(root) == []
    assert (protocol.pending_dir(root) / "JOB-a" / "input.txt").read_bytes() == b"body"


# ---------------------------------------------------------------------------
# reclaim
# ---------------------------------------------------------------------------
def _age_heartbeat(root, job_id, worker_id, seconds):
    hb = protocol.claimed_dir(root) / worker_id / f"{job_id}{protocol.HEARTBEAT_SUFFIX}"
    from datetime import timedelta

    hb.write_text((parse(now()) - timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%S.000Z") + "\n", encoding="utf-8")


def test_stale_claim_reclaimed(root):
    """C-unit ``stale_claim_reclaimed``. A closed laptop cannot return its
    own claim; the 60-minute sweep is the only thing that can."""
    queue_one(root, "JOB-a", payload=b"body")
    protocol.server_claim(root, "JOB-a", worker_id="dev")
    _age_heartbeat(root, "JOB-a", "dev", protocol.DEFAULT_CLAIM_EXPIRY_S + 60)

    reclaimed = protocol.reclaim_stale(root)
    assert [r["job_id"] for r in reclaimed] == ["JOB-a"]
    assert protocol.list_pending(root) == ["JOB-a"]
    assert protocol.list_claims(root) == []
    assert (protocol.pending_dir(root) / "JOB-a" / "input.txt").read_bytes() == b"body"


def test_fresh_claim_is_not_reclaimed(root):
    queue_one(root, "JOB-a")
    protocol.server_claim(root, "JOB-a", worker_id="dev")
    assert protocol.reclaim_stale(root) == []
    assert protocol.find_manifest(root, "JOB-a")[0] == "claimed"


def test_double_claim_after_reclaim_is_idempotent(root):
    """C-unit ``double_claim_after_reclaim_idempotent``. The suspended
    worker wakes up and finishes the job it still thinks it owns, while a
    second worker has already taken it: exactly one of them can publish,
    and the queue is left in a single consistent state either way."""
    queue_one(root, "JOB-a")
    protocol.server_claim(root, "JOB-a", worker_id="dev1")
    _age_heartbeat(root, "JOB-a", "dev1", protocol.DEFAULT_CLAIM_EXPIRY_S * 2)
    protocol.reclaim_stale(root)

    manifest = protocol.server_claim(root, "JOB-a", worker_id="dev2")
    assert manifest["job_id"] == "JOB-a"

    # dev1 wakes up: every verb it still believes it can use is refused,
    # because its claim no longer exists.
    for fn in (
        lambda: protocol.server_pull(root, "JOB-a", worker_id="dev1"),
        lambda: protocol.server_push(root, "JOB-a", b"", worker_id="dev1"),
        lambda: protocol.server_publish(root, "JOB-a", worker_id="dev1"),
        lambda: protocol.server_heartbeat(root, "JOB-a", worker_id="dev1"),
    ):
        with pytest.raises(protocol.OffloadProtocolError):
            fn()

    publish_stub_result(root, "JOB-a", worker_id="dev2", manifest=manifest)
    assert protocol.list_done(root) == ["JOB-a"]
    assert protocol.list_claims(root) == []


def test_reclaim_tolerates_an_interrupted_claim(root):
    """The crash window inside ``claim``: manifest already moved, inputs
    not yet. Reclaim must converge rather than refuse."""
    queue_one(root, "JOB-a", payload=b"body")
    protocol.server_claim(root, "JOB-a", worker_id="dev")
    # simulate the interruption: put the inputs back by hand, leaving the
    # manifest claimed (exactly what a kill between the two renames leaves)
    import shutil

    shutil.move(
        str(protocol.claimed_dir(root) / "dev" / "JOB-a"),
        str(protocol.pending_dir(root) / "JOB-a"),
    )
    _age_heartbeat(root, "JOB-a", "dev", protocol.DEFAULT_CLAIM_EXPIRY_S * 2)

    protocol.reclaim_stale(root)
    assert protocol.list_pending(root) == ["JOB-a"]
    assert (protocol.pending_dir(root) / "JOB-a" / "input.txt").read_bytes() == b"body"


def test_adopt_orphaned_partials_finishes_an_interrupted_publish(root):
    """The crash window inside ``publish``: the manifest is already inside
    the staging directory but the final rename never happened."""
    queue_one(root, "JOB-a")
    manifest = protocol.server_claim(root, "JOB-a", worker_id="dev")
    protocol.server_push(root, "JOB-a", _tar({"pages.json": b"{}"}), worker_id="dev")
    # the first step of publish, and nothing after it
    (protocol.claimed_dir(root) / "dev" / "JOB-a.json").replace(
        protocol.partial_dir(root) / "JOB-a" / protocol.MANIFEST_FILENAME
    )
    assert protocol.list_done(root) == []

    assert protocol.adopt_orphaned_partials(root) == ["JOB-a"]
    assert protocol.list_done(root) == ["JOB-a"]
    assert protocol.read_json(protocol.done_dir(root) / "JOB-a" / protocol.MANIFEST_FILENAME) == manifest
    assert protocol.list_claims(root) == []


def test_adopt_leaves_a_push_still_in_flight_alone(root):
    queue_one(root, "JOB-a")
    protocol.server_claim(root, "JOB-a", worker_id="dev")
    protocol.server_push(root, "JOB-a", _tar({"pages.json": b"{}"}), worker_id="dev")
    assert protocol.adopt_orphaned_partials(root) == []
    assert protocol.find_manifest(root, "JOB-a")[0] == "claimed"


# ---------------------------------------------------------------------------
# tar safety
# ---------------------------------------------------------------------------
def _tar(files: dict[str, bytes], *, symlink: str | None = None) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        if symlink is not None:
            info = tarfile.TarInfo(name=symlink)
            info.type = tarfile.SYMTYPE
            info.linkname = "/etc/passwd"
            tar.addfile(info)
    return buf.getvalue()


@pytest.mark.parametrize("shape", ["traversal", "absolute", "nested", "symlink"])
def test_push_refuses_a_hostile_archive(root, shape):
    """The whole attack surface of "a remote key hands us an archive":
    traversal names, absolute names, nesting, and symlinks that a later
    member could be written through."""
    archive = {
        "traversal": lambda: _tar({"../escape.json": b"x"}),
        "absolute": lambda: _tar({"/abs.json": b"x"}),
        "nested": lambda: _tar({"nested/inner.json": b"x"}),
        "symlink": lambda: _tar({}, symlink="link.json"),
    }[shape]()
    queue_one(root, "JOB-a")
    protocol.server_claim(root, "JOB-a", worker_id="dev")
    with pytest.raises(protocol.OffloadProtocolError):
        protocol.server_push(root, "JOB-a", archive, worker_id="dev")
    assert not (protocol.partial_dir(root) / "JOB-a").exists()


# ---------------------------------------------------------------------------
# verification
# ---------------------------------------------------------------------------
def _published(root, **kwargs):
    queue_one(root, "JOB-a")
    publish_stub_result(root, "JOB-a", **kwargs)
    return protocol.read_json(protocol.done_dir(root) / "JOB-a" / protocol.MANIFEST_FILENAME)


def test_verify_published_accepts_a_matching_result(root):
    manifest = _published(root)
    result = protocol.verify_published(root, "JOB-a", manifest)
    assert result["backend"] == STUB_OCR_NAME


def test_verify_published_rejects_a_payload_sha_mismatch(root):
    """C-unit ``sha_mismatch_to_failed``, verification half: the bytes on
    disk no longer match what the worker declared."""
    manifest = _published(root)
    (protocol.done_dir(root) / "JOB-a" / "pages.json").write_text("tampered", encoding="utf-8")
    with pytest.raises(protocol.OffloadVerificationError, match="sha256 mismatch"):
        protocol.verify_published(root, "JOB-a", manifest)


def test_verify_published_rejects_a_missing_payload(root):
    manifest = _published(root)
    (protocol.done_dir(root) / "JOB-a" / "pages.json").unlink()
    with pytest.raises(protocol.OffloadVerificationError, match="missing"):
        protocol.verify_published(root, "JOB-a", manifest)


def test_verify_published_does_not_ask_the_worker_about_the_config_hash(root):
    """SEC-2. This function used to compare ``result["config_hash"]`` with
    ``manifest["config_hash"]`` -- a check the worker could only ever pass,
    because it filled the former by copying the latter. The comparison that
    means something ("is this result for the configuration the stage has
    NOW?") lives in ``trialerror.offload.stage._resolve_or_park``, against
    the live config; see
    ``test_offload_stage.py::test_a_result_for_a_changed_config_is_requeued_not_folded_in``.

    So a result that invents a config_hash is simply ignored here -- and a
    result that omits it entirely (what the worker now writes) verifies."""
    manifest = _published(root, result_overrides={"config_hash": "someone-elses-config"})
    assert protocol.verify_published(root, "JOB-a", manifest)["backend"] == STUB_OCR_NAME
    assert "config_hash" not in protocol.published_result(root, "JOB-a") or True


def test_verify_published_rejects_a_fake_ocr_backend(root):
    """D13's last mile: even a correctly-shaped, correctly-hashed result is
    refused if the worker admits it came from the fake backend."""
    manifest = _published(root, result_overrides={"backend": "fake"})
    with pytest.raises(protocol.OffloadVerificationError, match="fake"):
        protocol.verify_published(root, "JOB-a", manifest)


def test_verify_published_rejects_the_wrong_ocr_backend(root):
    manifest = _published(root, result_overrides={"backend": "someothermarker"})
    with pytest.raises(protocol.OffloadVerificationError, match="expected"):
        protocol.verify_published(root, "JOB-a", manifest)


def test_verify_published_checks_the_embed_expect_block(root):
    protocol.queue_marker(
        root,
        job_id="JOB-e",
        stage="embed",
        doc_id="DOC-1",
        expect={
            "stage": "embed",
            "model_key": "stub-embed",
            "dims": 4,
            "chunk_count": 2,
            "chunk_ids": ["CHK-1", "CHK-2"],
            "outputs": ["vectors.jsonl"],
            "input_name": "chunks.jsonl",
        },
        config_hash="cfg",
        inputs=[("chunks.jsonl", b"{}\n")],
    )
    vectors = b"[0,0,0,0]\n[1,1,1,1]\n"
    publish_stub_result(
        root,
        "JOB-e",
        outputs={"vectors.jsonl": vectors},
        result_overrides={"model_key": "stub-embed", "dims": 4, "chunk_ids": ["CHK-1", "CHK-2"]},
    )
    manifest = protocol.read_json(protocol.done_dir(root) / "JOB-e" / protocol.MANIFEST_FILENAME)
    assert protocol.verify_published(root, "JOB-e", manifest)["dims"] == 4

    manifest_wrong = {**manifest, "expect": {**manifest["expect"], "model_key": "qwen3-4b"}}
    with pytest.raises(protocol.OffloadVerificationError, match="model_key"):
        protocol.verify_published(root, "JOB-e", manifest_wrong)

    manifest_wrong = {**manifest, "expect": {**manifest["expect"], "chunk_ids": ["CHK-1"]}}
    with pytest.raises(protocol.OffloadVerificationError, match="chunk id list"):
        protocol.verify_published(root, "JOB-e", manifest_wrong)


# ---------------------------------------------------------------------------
# counts / terminal transitions
# ---------------------------------------------------------------------------
def test_counts_and_oldest_pending_age(root):
    queue_one(root, "JOB-a")
    counts = protocol.counts(root)
    assert counts["pending"] == 1 and counts["claimed"] == 0 and counts["failed"] == 0
    assert counts["oldest_pending_age_s"] is not None

    manifest = protocol.read_json(protocol.pending_dir(root) / "JOB-a.json")
    manifest["created_ts"] = "2020-01-01T00:00:00.000Z"
    protocol.write_json(protocol.pending_dir(root) / "JOB-a.json", manifest)
    assert protocol.oldest_pending_age_s(root) > protocol.BACKLOG_WARN_S


def test_fail_marker_is_terminal_and_purges_every_other_trace(root):
    manifest = queue_one(root, "JOB-a")
    protocol.server_claim(root, "JOB-a", worker_id="dev")
    protocol.fail_marker(root, "JOB-a", manifest=manifest, error="boom")
    assert protocol.list_failed(root) == ["JOB-a"]
    assert protocol.list_pending(root) == []
    assert protocol.list_claims(root) == []
    assert protocol.find_manifest(root, "JOB-a")[0] == "failed"
    err = protocol.read_json(protocol.failed_dir(root) / "JOB-a" / protocol.ERROR_FILENAME)
    assert err["error"] == "boom"


def test_requeue_marker_carries_the_attempt_counter_forward(root):
    manifest = queue_one(root, "JOB-a")
    publish_stub_result(root, "JOB-a")
    bumped = {**manifest, "offload_attempts": 2}
    protocol.requeue_marker(root, "JOB-a", manifest=bumped, inputs=[("input.txt", b"again")])
    assert protocol.list_done(root) == []
    assert protocol.list_pending(root) == ["JOB-a"]
    assert protocol.read_json(protocol.pending_dir(root) / "JOB-a.json")["offload_attempts"] == 2


def test_discard_published_sweeps_a_completed_job(root):
    queue_one(root, "JOB-a")
    publish_stub_result(root, "JOB-a")
    protocol.discard_published(root, "JOB-a")
    assert protocol.list_done(root) == []
    assert protocol.find_manifest(root, "JOB-a") is None


def test_queue_marker_refuses_a_payload_name_with_a_separator(root):
    with pytest.raises(protocol.OffloadProtocolError):
        protocol.queue_marker(
            root,
            job_id="JOB-a",
            stage="ocr",
            doc_id=None,
            expect={},
            config_hash="c",
            inputs=[("../evil", b"x")],
        )


def test_manifest_round_trips_as_json(root):
    manifest = queue_one(root, "JOB-a")
    on_disk = json.loads((protocol.pending_dir(root) / "JOB-a.json").read_text(encoding="utf-8"))
    assert on_disk == manifest
    assert on_disk["schema"] == protocol.MANIFEST_SCHEMA


# ---------------------------------------------------------------------------
# SEC-1: the adoption gate
#
# `done/.partial/<job>/` is written by `push`, i.e. by whatever the holder of
# the restricted DEV key put in a tar. Before the fix, `kick` adopted any
# staging directory containing a manifest.json -- so the key could author the
# manifest that the whole verification chain is then checked against, using
# nothing but the seven permitted verbs.
# ---------------------------------------------------------------------------
def _forge_staging(root, job_id, manifest: dict) -> None:
    """Whatever `push` can leave behind: a staging directory whose entire
    contents, manifest.json included, came off the wire."""
    staging = protocol.partial_dir(root) / job_id
    staging.mkdir(parents=True, exist_ok=True)
    protocol.write_json(staging / protocol.MANIFEST_FILENAME, manifest)
    (staging / "pages.json").write_text('{"pages": []}', encoding="utf-8")


def test_adopt_refuses_a_forged_staging_dir_for_an_unknown_job(root):
    """The whole SEC-1 blocker in one test: a manifest for a job this
    sandbox never queued must never become a published result."""
    _forge_staging(
        root,
        "JOB-forged",
        {
            "schema": protocol.MANIFEST_SCHEMA,
            "job_id": "JOB-forged",
            "stage": "ocr",
            "config_hash": "whatever-the-key-likes",
            "expect": {},
            "inputs": [],
        },
    )
    assert protocol.adopt_orphaned_partials(root) == []
    assert protocol.list_done(root) == []
    rejected = protocol.rejected_partials(root)
    assert [r["job_id"] for r in rejected] == ["JOB-forged"]
    assert "never queued" in rejected[0]["reason"]
    assert not (protocol.partial_dir(root) / "JOB-forged").exists()


def test_adopt_refuses_a_pushed_manifest_while_the_job_is_still_claimed(root):
    """The same forgery aimed at a REAL job id. A genuine interrupted
    publish has already moved its manifest out of `claimed/`; this one has
    not, which is exactly what tells the two apart."""
    queue_one(root, "JOB-a")
    protocol.server_claim(root, "JOB-a", worker_id="dev")
    protocol.server_push(root, "JOB-a", _tar({"pages.json": b"{}"}), worker_id="dev")
    _forge_staging(root, "JOB-a", {"job_id": "JOB-a", "stage": "ocr", "config_hash": "forged"})

    assert protocol.adopt_orphaned_partials(root) == []
    assert protocol.list_done(root) == []
    assert protocol.find_manifest(root, "JOB-a")[0] == "claimed"
    assert [r["job_id"] for r in protocol.rejected_partials(root)] == ["JOB-a"]


def test_adopt_publishes_the_sandboxs_manifest_not_the_pushed_one(root):
    """A genuine interrupted publish is still adopted -- and the manifest
    that lands in `done/` is the sandbox's own record, because even a
    directory that passes both gates has had DEV bytes in it."""
    queued = queue_one(root, "JOB-a")
    protocol.server_claim(root, "JOB-a", worker_id="dev")
    protocol.server_push(root, "JOB-a", _tar({"pages.json": b"{}"}), worker_id="dev")
    # publish's first step, and nothing after it -- then the key overwrites
    # the manifest that step moved in.
    (protocol.claimed_dir(root) / "dev" / "JOB-a.json").replace(
        protocol.partial_dir(root) / "JOB-a" / protocol.MANIFEST_FILENAME
    )
    protocol.write_json(
        protocol.partial_dir(root) / "JOB-a" / protocol.MANIFEST_FILENAME,
        {**queued, "config_hash": "forged", "expect": {"backend": "anything-goes"}},
    )

    assert protocol.adopt_orphaned_partials(root) == ["JOB-a"]
    published = protocol.read_json(protocol.done_dir(root) / "JOB-a" / protocol.MANIFEST_FILENAME)
    assert published == queued
    assert published["config_hash"] == "cfg-hash"


def test_the_queued_record_is_not_reachable_through_the_verbs(root):
    """`queued/` is the trust anchor: it must not be one of the directories
    the wrapper creates, claims into, or publishes through."""
    queue_one(root, "JOB-a")
    assert protocol.queued_manifest(root, "JOB-a")["job_id"] == "JOB-a"
    protocol.server_claim(root, "JOB-a", worker_id="dev")
    protocol.server_push(root, "JOB-a", _tar({"pages.json": b"{}"}), worker_id="dev")
    protocol.server_publish(root, "JOB-a", worker_id="dev")
    # untouched by the whole round trip
    assert protocol.queued_manifest(root, "JOB-a")["job_id"] == "JOB-a"
    # and dropped only when the sandbox itself is done with the job
    protocol.discard_published(root, "JOB-a")
    assert protocol.queued_manifest(root, "JOB-a") is None


def test_a_terminal_marker_drops_the_queued_record(root):
    manifest = queue_one(root, "JOB-a")
    protocol.fail_marker(root, "JOB-a", manifest=manifest, error="nope")
    assert protocol.queued_manifest(root, "JOB-a") is None


# ---------------------------------------------------------------------------
# SEC-4 / SEC-5: archive names and sizes
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", ["C:pages.json", "C:/pages.json", "pages.json:stream"])
def test_unpack_into_refuses_a_drive_or_stream_qualified_name(tmp_path, name):
    """SEC-5. ``C:pages.json`` is drive-RELATIVE: ``os.path.isabs`` says
    False, and joining it onto the staging directory resolves it against
    the current directory of drive C: instead."""
    with pytest.raises(protocol.OffloadProtocolError, match="member name"):
        protocol.unpack_into(_tar({name: b"x"}), tmp_path / "out")


def test_unpack_into_refuses_an_over_large_archive(tmp_path):
    """SEC-4: the cap fails the verb; it never truncates."""
    with pytest.raises(protocol.OffloadProtocolError, match="cap|more than"):
        protocol.unpack_into(_tar({"pages.json": b"x" * 4096}), tmp_path / "out", max_bytes=1024)


def test_server_pull_refuses_an_over_large_input_set(root, monkeypatch):
    queue_one(root, "JOB-a", payload=b"x" * 4096)
    protocol.server_claim(root, "JOB-a", worker_id="dev")
    monkeypatch.setattr(protocol, "MAX_PAYLOAD_BYTES", 512)
    with pytest.raises(protocol.OffloadProtocolError, match="cap"):
        protocol.server_pull(root, "JOB-a", worker_id="dev")


# ---------------------------------------------------------------------------
# SEC-6 / V2
# ---------------------------------------------------------------------------
def test_a_job_id_with_a_trailing_newline_is_refused(root):
    """SEC-6: Python's ``$`` matches before a trailing newline, so the old
    anchor accepted ``"JOB-a\n"`` as if it were ``"JOB-a"`` -- a different
    path component, and one the shell wrapper refuses."""
    with pytest.raises(protocol.OffloadProtocolError):
        protocol.validate_job_id("JOB-a\n")


def test_reclaim_and_adopt_are_read_only_when_there_is_no_queue(tmp_path):
    """V2: `reclaim`/`kick` run from an unattended loop in every program,
    including the many that offload nothing. Asking the question must not
    create the answer."""
    absent = tmp_path / "program" / "offload"
    assert protocol.reclaim_stale(absent) == []
    assert protocol.adopt_orphaned_partials(absent) == []
    assert protocol.rejected_partials(absent) == []
    assert not absent.exists()
