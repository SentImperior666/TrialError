"""The file queue — the one structure that crosses the trust boundary.

Three properties carry the whole transport, and each has tests here that fail
loudly if it ever stops holding:

* **claiming is atomic** — two workers racing on one manifest produce exactly
  one winner, and the loser never reads the file;
* **publication is atomic** — a half-written result is invisible, whatever
  happens to the writer in the middle;
* **the schemas are closed** — an unknown key is ``manifest_invalid``, not a
  field politely ignored, because the manifest is what a process this design
  assumes may be compromised hands to the process with egress.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from trialerror.webfetch import MANIFEST_SCHEMA, RESULT_SCHEMA, WebFetchRefused
from trialerror.webfetch.protocol import (
    BODY_FILENAME,
    HEADERS_SUBSET_KEYS,
    ID_RE,
    MANIFEST_KEYS,
    REPO_FILENAME,
    RESULT_FILENAME,
    RESULT_KEYS,
    STATE_ABSENT,
    STATE_CLAIMED,
    STATE_DONE,
    STATE_FAILED,
    STATE_PENDING,
    AlreadyPublished,
    Manifest,
    ProtocolError,
    Queue,
    append_jsonl,
    check_id,
    new_result,
    validate_manifest,
    validate_result,
)

FIXED = datetime(2026, 9, 5, 12, 0, 0, tzinfo=timezone.utc)


class FakeNow:
    """A movable clock for heartbeat, reclaim and expiry."""

    def __init__(self, start: datetime = FIXED) -> None:
        self.value = start

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value = self.value + timedelta(seconds=seconds)


def make_manifest(**overrides) -> Manifest:
    fields = {
        "job_id": "JOB-webfetch-WF-01",
        "fetch_id": "WF-01",
        "launch_id": "LNCH-01M1R3J6TZ2HFQM95AEFEW1GY1",
        "url": "https://diataxis.fr/tutorials/",
        "kind": "page",
        "origin": "operator_list",
        "created_ts": "2026-09-05T12:00:00.000Z",
    }
    fields.update(overrides)
    return Manifest.build(**fields)


def make_queue(tmp_path: Path, now: FakeNow | None = None) -> Queue:
    return Queue(tmp_path / "webfetch", _now_fn=now or FakeNow()).ensure_layout()


def age_file_to(path: Path, moment: datetime) -> None:
    """Set a file's mtime to a moment on the *fake* clock.

    A claim token carries no timestamp inside it, so ``Queue.reclaim``'s
    orphan-token sweep dates it by ``st_mtime`` — which in production is the
    same clock ``_now_fn`` reads, and in a test is emphatically not. Without
    this, a test that advances :class:`FakeNow` past a cutoff still compares
    that cutoff against the *wall-clock* mtime of a file created seconds ago,
    so it passes or fails depending on the real time of day (found the hard
    way: green all morning, red after 13:00 UTC on the fixed date below).
    """
    stamp = moment.timestamp()
    os.utime(path, (stamp, stamp))


def ok_result(manifest: Manifest, **overrides) -> dict:
    fields = {
        "manifest": manifest,
        "outcome": "fetched",
        "url_norm": "https://diataxis.fr/tutorials/",
        "final_url": "https://diataxis.fr/tutorials/",
        "http_status": 200,
        "content_type": "text/html; charset=utf-8",
        "content_class": "html",
        "payload_bytes": 5,
        "content_sha256": "e" * 64,
    }
    fields.update(overrides)
    return new_result(**fields)


# --------------------------------------------------------------------------
# ids
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,why",
    [
        ("..", "parent directory"),
        (".", "current directory"),
        (".partial", "would collide with the publish staging directory"),
        (".hidden", "any leading dot"),
        ("a/b", "a path separator"),
        ("a\\b", "a Windows path separator"),
        ("", "empty"),
        ("x" * 200, "absurdly long"),
        ("has space", "space"),
        ("nul\x00", "NUL byte"),
        (None, "not a string"),
        (7, "not a string"),
    ],
)
def test_unsafe_ids_are_refused(value: object, why: str) -> None:
    with pytest.raises(ProtocolError):
        check_id(value, what="job_id")


@pytest.mark.parametrize(
    "value", ["JOB-webfetch-WF-01ARZ3", "WF-01ARZ3NDEKTSV4RRFFQ69G5FAV", "a", "a.b_c-d"]
)
def test_ordinary_ids_are_accepted(value: str) -> None:
    assert check_id(value) == value


def test_the_id_pattern_forbids_a_leading_dot() -> None:
    """The design's floor is ``^[A-Za-z0-9._-]+$``; this adds "no leading
    dot", which keeps ``.``, ``..`` and ``.partial`` out of the namespace in
    one rule."""
    assert ID_RE.match("ok-1")
    assert not ID_RE.match(".partial")


# --------------------------------------------------------------------------
# manifest schema
# --------------------------------------------------------------------------


def test_a_round_trip_manifest_validates() -> None:
    manifest = make_manifest()
    assert validate_manifest(manifest.to_dict()) == manifest
    assert set(manifest.to_dict()) == set(MANIFEST_KEYS)


def test_an_unknown_key_is_refused() -> None:
    """The headline case of design §4 T2: the manifest has no header, body or
    method field, and a manifest that invents one is refused rather than
    having the extra silently dropped."""
    data = make_manifest().to_dict()
    data["headers"] = {"Authorization": "Bearer x"}
    with pytest.raises(WebFetchRefused) as caught:
        validate_manifest(data)
    assert caught.value.reason == "manifest_invalid"
    assert "headers" in caught.value.detail


@pytest.mark.parametrize("key", sorted(set(MANIFEST_KEYS)))
def test_every_key_is_required(key: str) -> None:
    data = make_manifest().to_dict()
    del data[key]
    with pytest.raises(WebFetchRefused) as caught:
        validate_manifest(data)
    assert caught.value.reason == "manifest_invalid"


@pytest.mark.parametrize(
    "mutation,why",
    [
        ({"schema": 2}, "a schema version this build does not speak"),
        ({"schema": "1"}, "a string schema version"),
        ({"job_id": "../escape"}, "an id used as a path component"),
        ({"fetch_id": ".partial"}, "an id that would name the staging directory"),
        ({"launch_id": 5}, "an id that is not a string"),
        ({"program_id": "../x"}, "a program id used in a path"),
        ({"url": ""}, "an empty URL"),
        ({"url": "h" * 9000}, "an absurd URL"),
        ({"url": 42}, "a URL that is not a string"),
        ({"origin": "root"}, "an origin outside the closed set"),
        ({"kind": "socket"}, "a kind outside the closed set"),
        ({"created_ts": "yesterday"}, "an unparseable timestamp"),
        ({"conditional": {"etag": "x"}}, "a conditional block missing a key"),
        ({"conditional": {"etag": "x", "last_modified": None, "extra": 1}}, "an extra key"),
        ({"conditional": {"etag": "x" * 300, "last_modified": None}}, "an oversized etag"),
        ({"conditional": "none"}, "a conditional block that is not an object"),
        ({"list_ref": 3}, "a list ref that is not a string"),
    ],
)
def test_malformed_manifests_are_refused(mutation: dict, why: str) -> None:
    data = make_manifest().to_dict()
    data.update(mutation)
    with pytest.raises(WebFetchRefused) as caught:
        validate_manifest(data)
    assert caught.value.reason == "manifest_invalid", why


def test_a_manifest_that_is_not_an_object_is_refused() -> None:
    for value in ([], "x", 3, None):
        with pytest.raises(WebFetchRefused):
            validate_manifest(value)


def test_nullable_fields_really_are_nullable() -> None:
    manifest = make_manifest(list_ref=None, program_id=None, robots_override_ruling=None)
    assert manifest.list_ref is None
    assert manifest.etag is None and manifest.last_modified is None


def test_conditional_headers_are_reachable_by_name() -> None:
    manifest = make_manifest(etag='W/"abc"', last_modified="Wed, 21 Oct 2026 07:28:00 GMT")
    assert manifest.etag == 'W/"abc"'
    assert manifest.last_modified.endswith("GMT")


def test_build_validates_on_the_writing_side() -> None:
    """A malformed manifest should fail where the operator can see the CLI
    error, not 45 seconds later in a container with no console."""
    with pytest.raises(WebFetchRefused):
        Manifest.build(job_id="ok", fetch_id="ok", launch_id="ok", url="x", kind="socket")


# --------------------------------------------------------------------------
# result schema
# --------------------------------------------------------------------------


def test_new_result_fills_every_key() -> None:
    result = ok_result(make_manifest())
    assert set(result) == set(RESULT_KEYS)
    assert result["schema"] == RESULT_SCHEMA
    assert result["sidecar_version"] == "webfetch-sidecar/1"
    assert set(result["headers_subset"]) == set(HEADERS_SUBSET_KEYS)


def test_a_refusal_before_any_socket_still_produces_a_complete_record() -> None:
    result = new_result(
        manifest=make_manifest(), outcome="refused", url_norm="", reason="ip_private"
    )
    assert set(result) == set(RESULT_KEYS)
    assert result["bytes"] == 0 and result["resolved_ips"] == [] and result["bytes_out"] == 0
    assert result["robots"] == {"fetched": False, "verdict": "n/a", "crawl_delay_s": 0}
    assert result["policy"]["verdict"] == "refused"


def test_a_reason_outside_the_closed_vocabulary_is_refused() -> None:
    with pytest.raises(WebFetchRefused) as caught:
        new_result(
            manifest=make_manifest(), outcome="refused", url_norm="", reason="looked_wrong"
        )
    assert caught.value.reason == "manifest_invalid"


def test_refused_requires_a_reason_and_others_forbid_one() -> None:
    with pytest.raises(WebFetchRefused):
        new_result(manifest=make_manifest(), outcome="refused", url_norm="")
    with pytest.raises(WebFetchRefused):
        new_result(
            manifest=make_manifest(), outcome="fetched", url_norm="", reason="timeout"
        )


def test_an_unknown_result_key_is_refused() -> None:
    data = ok_result(make_manifest())
    data["cookies"] = {"session": "x"}
    with pytest.raises(WebFetchRefused):
        validate_result(data)


def test_headers_subset_has_fixed_keys_only() -> None:
    """A header the remote invents cannot create a key here (design §4 T3)."""
    result = ok_result(
        make_manifest(),
        headers_subset={"etag": '"a"', "x-evil": "drop tables", "Server": "nginx"},
    )
    assert result["headers_subset"]["etag"] == '"a"'
    assert result["headers_subset"]["server"] == "nginx"
    assert "x-evil" not in result["headers_subset"]


def test_an_oversized_header_value_is_refused() -> None:
    data = ok_result(make_manifest())
    data["headers_subset"]["server"] = "x" * 2000
    with pytest.raises(WebFetchRefused):
        validate_result(data)


@pytest.mark.parametrize(
    "mutation",
    [
        {"outcome": "downloaded"},
        {"content_class": "exe"},
        {"redirect_chain": "https://a"},
        {"resolved_ips": [1, 2]},
        {"bytes": -1},
        {"elapsed_ms": "fast"},
        {"robots": {"fetched": True, "verdict": "maybe", "crawl_delay_s": 0}},
        {"robots": {"fetched": "yes", "verdict": "allow", "crawl_delay_s": 0}},
        {"policy": {"verdict": "sure", "host_rule": None, "query_stripped": False}},
        {"policy": {"verdict": "allow", "host_rule": None, "query_stripped": "no"}},
        {"git": {"head": "abc", "ref": "main"}},
        {"git": {"head": "abc", "ref": "main", "path": None, "extra": 1}},
        {"schema": 99},
    ],
)
def test_malformed_results_are_refused(mutation: dict) -> None:
    data = ok_result(make_manifest())
    data.update(mutation)
    with pytest.raises(WebFetchRefused):
        validate_result(data)


def test_a_git_result_carries_its_commit() -> None:
    result = ok_result(
        make_manifest(kind="git", url="https://github.com/o/r"),
        content_class="git",
        git={"head": "a" * 40, "ref": "main", "path": None},
    )
    assert result["git"]["head"] == "a" * 40


# --------------------------------------------------------------------------
# submit and state
# --------------------------------------------------------------------------


def test_submit_writes_a_pending_manifest(tmp_path: Path) -> None:
    queue = make_queue(tmp_path)
    manifest = make_manifest()
    outcome = queue.submit(manifest)
    assert outcome.wrote is True and outcome.state == STATE_PENDING
    assert queue.state_of(manifest.job_id) == STATE_PENDING
    written = json.loads(outcome.path.read_text(encoding="utf-8"))
    assert validate_manifest(written) == manifest


def test_submit_is_idempotent_in_every_state(tmp_path: Path) -> None:
    """The handler is re-entered every jobs-worker cycle while it waits; each
    re-entry must be a no-op rather than a second fetch of the same URL."""
    queue = make_queue(tmp_path)
    manifest = make_manifest()
    queue.submit(manifest)
    again = queue.submit(manifest)
    assert again.wrote is False and again.state == STATE_PENDING

    claim = queue.claim(manifest.job_id, "worker-1")
    assert claim is not None
    assert queue.submit(manifest).state == STATE_CLAIMED

    queue.publish(claim, ok_result(manifest), {BODY_FILENAME: b"hello"})
    assert queue.submit(manifest).state == STATE_DONE


def test_state_is_absent_for_an_unknown_job(tmp_path: Path) -> None:
    assert make_queue(tmp_path).state_of("JOB-nothing") == STATE_ABSENT


def test_a_half_written_result_is_invisible(tmp_path: Path) -> None:
    """Publication staging lives inside ``done/``, so the final rename never
    crosses a filesystem — and until it happens the job is not done."""
    queue = make_queue(tmp_path)
    manifest = make_manifest()
    queue.submit(manifest)
    claim = queue.claim(manifest.job_id, "worker-1")
    assert claim is not None

    staging = queue.partial_dir / manifest.job_id
    staging.mkdir(parents=True, exist_ok=True)
    (staging / RESULT_FILENAME).write_text("{}", encoding="utf-8")
    (staging / BODY_FILENAME).write_bytes(b"half")

    assert queue.state_of(manifest.job_id) == STATE_CLAIMED
    assert not (queue.done_dir / manifest.job_id).exists()


def test_a_directory_without_a_result_is_not_done(tmp_path: Path) -> None:
    queue = make_queue(tmp_path)
    (queue.done_dir / "JOB-x").mkdir(parents=True)
    assert queue.state_of("JOB-x") == STATE_ABSENT


# --------------------------------------------------------------------------
# claiming
# --------------------------------------------------------------------------


def test_claim_moves_the_manifest_out_of_pending(tmp_path: Path) -> None:
    queue = make_queue(tmp_path)
    manifest = make_manifest()
    queue.submit(manifest)
    claim = queue.claim(manifest.job_id, "worker-1")
    assert claim is not None and claim.ok
    assert claim.manifest == manifest
    assert not queue.pending_path(manifest.job_id).exists()
    assert claim.manifest_path.is_file()
    assert claim.heartbeat_path.is_file()


def test_the_loser_of_a_claim_race_gets_nothing(tmp_path: Path) -> None:
    queue = make_queue(tmp_path)
    manifest = make_manifest()
    queue.submit(manifest)
    assert queue.claim(manifest.job_id, "worker-1") is not None
    assert queue.claim(manifest.job_id, "worker-2") is None


def test_concurrent_workers_claim_each_job_exactly_once(tmp_path: Path) -> None:
    """The property that matters, under a real race.

    This test is the reason ``claim`` takes an ``O_EXCL`` token before it
    renames anything. With the rename alone it fails on Windows — four
    threads that each opened the manifest before any of them moved it all
    report success, passing one file down a chain of destinations. The
    filesystem still ends up consistent, which is exactly what makes the bug
    worth a test rather than a code read: only the count is wrong.
    """
    queue = make_queue(tmp_path)
    job_count = 12
    for index in range(job_count):
        queue.submit(
            make_manifest(job_id=f"JOB-webfetch-WF-{index:03d}", fetch_id=f"WF-{index:03d}")
        )

    start = threading.Barrier(4)
    claimed: list[str] = []
    lock = threading.Lock()

    def worker(name: str) -> None:
        start.wait()
        while True:
            claim = queue.claim_next(name)
            if claim is None:
                return
            with lock:
                claimed.append(claim.job_id)

    threads = [threading.Thread(target=worker, args=(f"worker-{i}",)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert len(claimed) == job_count, "a job was claimed by more than one worker"
    assert len(set(claimed)) == job_count
    assert queue.pending_job_ids() == []
    on_disk = sorted(path.stem for path in queue.claimed_dir.glob("*/*.json"))
    assert on_disk == sorted(claimed)


def test_a_claimed_job_holds_an_exclusion_token(tmp_path: Path) -> None:
    queue = make_queue(tmp_path)
    manifest = make_manifest()
    queue.submit(manifest)
    claim = queue.claim(manifest.job_id, "worker-1")
    assert claim is not None
    assert claim.lock_path.is_file()
    assert claim.lock_path.read_text(encoding="utf-8").strip() == "worker-1"


def test_publishing_releases_the_token(tmp_path: Path) -> None:
    queue = make_queue(tmp_path)
    manifest = make_manifest()
    queue.submit(manifest)
    claim = queue.claim(manifest.job_id, "worker-1")
    assert claim is not None
    queue.publish(claim, ok_result(manifest), {BODY_FILENAME: b"x"})
    assert not claim.lock_path.exists()


def test_a_token_over_a_missing_manifest_is_released_immediately(tmp_path: Path) -> None:
    """Winning the token but finding nothing to move means the job was
    settled between the listing and now — the token must not park it."""
    queue = make_queue(tmp_path)
    assert queue.claim("JOB-gone", "worker-1") is None
    assert not queue.claim_lock_path("JOB-gone").exists()


def test_an_orphan_token_is_swept_by_reclaim(tmp_path: Path) -> None:
    """A worker killed between taking the token and moving the manifest
    leaves a token with nothing behind it; without the sweep the job would be
    claimable by nobody, forever."""
    now = FakeNow()
    queue = make_queue(tmp_path, now)
    manifest = make_manifest()
    queue.submit(manifest)
    lock = queue.claim_lock_path(manifest.job_id)
    lock.write_text("worker-dead\n", encoding="utf-8")
    age_file_to(lock, now.value)

    assert queue.claim(manifest.job_id, "worker-2") is None
    now.advance(7200)
    queue.reclaim(3600)
    assert not queue.claim_lock_path(manifest.job_id).exists()
    assert queue.claim(manifest.job_id, "worker-2") is not None


def test_a_fresh_token_is_left_alone_by_reclaim(tmp_path: Path) -> None:
    now = FakeNow()
    queue = make_queue(tmp_path, now)
    manifest = make_manifest()
    queue.submit(manifest)
    claim = queue.claim(manifest.job_id, "worker-1")
    assert claim is not None
    age_file_to(claim.lock_path, now.value)
    now.advance(30)
    assert queue.reclaim(3600) == []
    assert queue.claim_lock_path(manifest.job_id).is_file()


def test_reclaiming_a_stale_claim_frees_its_token_too(tmp_path: Path) -> None:
    now = FakeNow()
    queue = make_queue(tmp_path, now)
    manifest = make_manifest()
    queue.submit(manifest)
    queue.claim(manifest.job_id, "worker-1")
    now.advance(7200)
    assert queue.reclaim(3600) == [manifest.job_id]
    assert not queue.claim_lock_path(manifest.job_id).exists()
    assert queue.claim(manifest.job_id, "worker-2") is not None


def test_claim_next_returns_none_on_an_empty_queue(tmp_path: Path) -> None:
    assert make_queue(tmp_path).claim_next("worker-1") is None


def test_a_manifest_that_does_not_parse_is_still_claimed(tmp_path: Path) -> None:
    """Claimed, so no other worker hits the same wall — and carrying the
    refusal, so the loop can publish a record instead of leaving the file to
    rot."""
    queue = make_queue(tmp_path)
    queue.pending_dir.mkdir(parents=True, exist_ok=True)
    (queue.pending_dir / "JOB-bad.json").write_text('{"schema": 1, "nope": true}', encoding="utf-8")

    claim = queue.claim("JOB-bad", "worker-1")
    assert claim is not None
    assert claim.ok is False
    assert claim.error is not None and claim.error.reason == "manifest_invalid"


def test_a_manifest_naming_a_different_job_is_refused(tmp_path: Path) -> None:
    """The filename and the ``job_id`` inside must agree, or one of them is a
    lie about which job this is."""
    queue = make_queue(tmp_path)
    manifest = make_manifest(job_id="JOB-webfetch-WF-99", fetch_id="WF-99")
    (queue.pending_dir / "JOB-other.json").write_text(
        json.dumps(manifest.to_dict()), encoding="utf-8"
    )
    claim = queue.claim("JOB-other", "worker-1")
    assert claim is not None and claim.ok is False
    assert claim.error is not None and claim.error.reason == "manifest_invalid"


def test_an_absurdly_large_manifest_is_refused_unread(tmp_path: Path) -> None:
    queue = make_queue(tmp_path)
    (queue.pending_dir / "JOB-big.json").write_text("x" * (70 * 1024), encoding="utf-8")
    claim = queue.claim("JOB-big", "worker-1")
    assert claim is not None and claim.error is not None
    assert "cap" in claim.error.detail


def test_pending_files_with_unusable_names_are_ignored(tmp_path: Path) -> None:
    queue = make_queue(tmp_path)
    (queue.pending_dir / ".sneaky.json").write_text("{}", encoding="utf-8")
    assert queue.pending_job_ids() == []
    assert queue.claim_next("worker-1") is None


# --------------------------------------------------------------------------
# heartbeat, reclaim, expiry
# --------------------------------------------------------------------------


def test_a_stale_claim_returns_to_pending(tmp_path: Path) -> None:
    now = FakeNow()
    queue = make_queue(tmp_path, now)
    manifest = make_manifest()
    queue.submit(manifest)
    queue.claim(manifest.job_id, "worker-1")

    now.advance(30)
    assert queue.reclaim(3600) == []

    now.advance(7200)
    assert queue.reclaim(3600) == [manifest.job_id]
    assert queue.state_of(manifest.job_id) == STATE_PENDING


def test_a_heartbeat_keeps_a_long_job_claimed(tmp_path: Path) -> None:
    now = FakeNow()
    queue = make_queue(tmp_path, now)
    manifest = make_manifest()
    queue.submit(manifest)
    claim = queue.claim(manifest.job_id, "worker-1")
    assert claim is not None

    now.advance(7200)
    queue.heartbeat(claim)
    assert queue.reclaim(3600) == []
    assert queue.state_of(manifest.job_id) == STATE_CLAIMED


def test_a_stale_claim_on_an_already_settled_job_is_just_dropped(tmp_path: Path) -> None:
    now = FakeNow()
    queue = make_queue(tmp_path, now)
    manifest = make_manifest()
    queue.submit(manifest)
    claim = queue.claim(manifest.job_id, "worker-1")
    assert claim is not None
    queue.publish(claim, ok_result(manifest), {BODY_FILENAME: b"hi"})

    # A stale claim file left behind by a killed worker.
    stray = queue.claimed_dir / "worker-9"
    stray.mkdir(parents=True)
    stray_manifest = stray / f"{manifest.job_id}.json"
    stray_manifest.write_text(json.dumps(manifest.to_dict()), encoding="utf-8")
    # No heartbeat beside it, so reclaim dates it by mtime — pin that to the
    # fake clock or the branch under test depends on the real time of day
    # (see ``age_file_to``).
    age_file_to(stray_manifest, now.value)
    now.advance(7200)
    assert queue.reclaim(3600) == []
    assert queue.state_of(manifest.job_id) == STATE_DONE


def test_unclaim_puts_a_job_back_without_settling_it(tmp_path: Path) -> None:
    queue = make_queue(tmp_path)
    manifest = make_manifest()
    queue.submit(manifest)
    claim = queue.claim(manifest.job_id, "worker-1")
    assert claim is not None
    assert queue.unclaim(claim) is True
    assert queue.state_of(manifest.job_id) == STATE_PENDING
    assert queue.unclaim(claim) is False


def test_expired_pending_uses_the_manifests_own_timestamp(tmp_path: Path) -> None:
    now = FakeNow()
    queue = make_queue(tmp_path, now)
    fresh = make_manifest(
        job_id="JOB-fresh", fetch_id="WF-fresh", created_ts="2026-09-05T11:59:00.000Z"
    )
    old = make_manifest(
        job_id="JOB-old", fetch_id="WF-old", created_ts="2026-09-03T12:00:00.000Z"
    )
    queue.submit(fresh)
    queue.submit(old)
    assert queue.expired_pending(86400) == ["JOB-old"]


def test_an_unreadable_manifest_still_ages(tmp_path: Path) -> None:
    """A file nobody can parse must not pin the queue open forever."""
    now = FakeNow(datetime(2099, 1, 1, tzinfo=timezone.utc))
    queue = make_queue(tmp_path, now)
    (queue.pending_dir / "JOB-junk.json").write_text("not json", encoding="utf-8")
    assert queue.expired_pending(86400) == ["JOB-junk"]


def test_the_sidecar_heartbeat_reports_its_age(tmp_path: Path) -> None:
    now = FakeNow()
    queue = make_queue(tmp_path, now)
    assert queue.sidecar_heartbeat_age_s() is None

    queue.touch_sidecar_heartbeat({"worker_id": "worker-1"})
    assert queue.sidecar_heartbeat_age_s() == pytest.approx(0.0)
    record = json.loads(queue.heartbeat_path.read_text(encoding="utf-8"))
    assert record["worker_id"] == "worker-1" and record["pending"] == 0

    now.advance(900)
    assert queue.sidecar_heartbeat_age_s() == pytest.approx(900.0)


# --------------------------------------------------------------------------
# publication
# --------------------------------------------------------------------------


def test_publish_lands_a_complete_result_and_clears_the_claim(tmp_path: Path) -> None:
    queue = make_queue(tmp_path)
    manifest = make_manifest()
    queue.submit(manifest)
    claim = queue.claim(manifest.job_id, "worker-1")
    assert claim is not None

    target = queue.publish(claim, ok_result(manifest), {BODY_FILENAME: b"hello"})
    assert target == queue.done_path(manifest.job_id)
    assert (target / RESULT_FILENAME).is_file()
    assert (target / BODY_FILENAME).read_bytes() == b"hello"
    assert queue.state_of(manifest.job_id) == STATE_DONE
    assert not claim.manifest_path.exists()
    assert not claim.heartbeat_path.exists()
    assert not queue.partial_dir.joinpath(manifest.job_id).exists()


def test_publish_accepts_a_payload_from_a_file(tmp_path: Path) -> None:
    queue = make_queue(tmp_path)
    manifest = make_manifest(kind="git", url="https://github.com/o/r")
    queue.submit(manifest)
    claim = queue.claim(manifest.job_id, "worker-1")
    assert claim is not None
    tar = tmp_path / "repo.tar"
    tar.write_bytes(b"tarbytes")

    queue.publish(
        claim,
        ok_result(manifest, content_class="git", git={"head": "a" * 40, "ref": "main", "path": None}),
        {REPO_FILENAME: tar},
    )
    assert queue.payload_path(manifest.job_id, REPO_FILENAME).read_bytes() == b"tarbytes"


def test_only_the_two_fixed_payload_names_may_be_published(tmp_path: Path) -> None:
    """The research side reads fixed names only, so a compromised sidecar
    cannot get an arbitrarily-named file read by naming it in the result."""
    queue = make_queue(tmp_path)
    manifest = make_manifest()
    queue.submit(manifest)
    claim = queue.claim(manifest.job_id, "worker-1")
    assert claim is not None
    with pytest.raises(ProtocolError, match="payload"):
        queue.publish(claim, ok_result(manifest), {"../../evil.sh": b"#!/bin/sh"})
    with pytest.raises(ProtocolError):
        queue.payload_path(manifest.job_id, "notes.txt")


def test_publishing_twice_says_so_rather_than_overwriting(tmp_path: Path) -> None:
    queue = make_queue(tmp_path)
    manifest = make_manifest()
    queue.submit(manifest)
    claim = queue.claim(manifest.job_id, "worker-1")
    assert claim is not None
    queue.publish(claim, ok_result(manifest), {BODY_FILENAME: b"first"})

    with pytest.raises(AlreadyPublished):
        queue.publish(claim, ok_result(manifest), {BODY_FILENAME: b"second"})
    assert queue.payload_path(manifest.job_id, BODY_FILENAME).read_bytes() == b"first"
    assert not queue.partial_dir.joinpath(manifest.job_id).exists()


def test_a_result_for_a_different_job_is_refused(tmp_path: Path) -> None:
    queue = make_queue(tmp_path)
    manifest = make_manifest()
    queue.submit(manifest)
    claim = queue.claim(manifest.job_id, "worker-1")
    assert claim is not None
    other = make_manifest(job_id="JOB-other", fetch_id="WF-other")
    with pytest.raises(ProtocolError, match="does not match the claim"):
        queue.publish(claim, ok_result(other))


def test_a_refusal_is_published_to_failed_with_no_payload(tmp_path: Path) -> None:
    queue = make_queue(tmp_path)
    manifest = make_manifest()
    queue.submit(manifest)
    claim = queue.claim(manifest.job_id, "worker-1")
    assert claim is not None

    refusal = new_result(
        manifest=manifest, outcome="refused", url_norm="https://x/", reason="robots_disallow"
    )
    target = queue.fail(claim, refusal)
    assert target == queue.failed_path(manifest.job_id)
    assert queue.state_of(manifest.job_id) == STATE_FAILED
    state, read_back = queue.read_result(manifest.job_id)
    assert state == STATE_FAILED and read_back["reason"] == "robots_disallow"
    assert list(target.iterdir()) == [target / RESULT_FILENAME]


def test_read_result_validates_what_it_reads(tmp_path: Path) -> None:
    """A compromised sidecar must not hand the database a shape nobody
    checked."""
    queue = make_queue(tmp_path)
    directory = queue.done_path("JOB-forged")
    directory.mkdir(parents=True)
    (directory / RESULT_FILENAME).write_text('{"schema": 1, "surprise": true}', encoding="utf-8")
    with pytest.raises(WebFetchRefused):
        queue.read_result("JOB-forged")


def test_read_result_on_an_unsettled_job_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ProtocolError):
        make_queue(tmp_path).read_result("JOB-nothing")


# --------------------------------------------------------------------------
# payload verification and sweeping
# --------------------------------------------------------------------------


def publish_body(queue: Queue, manifest: Manifest, body: bytes) -> dict:
    import hashlib

    queue.submit(manifest)
    claim = queue.claim(manifest.job_id, "worker-1")
    assert claim is not None
    result = ok_result(
        manifest, payload_bytes=len(body), content_sha256=hashlib.sha256(body).hexdigest()
    )
    queue.publish(claim, result, {BODY_FILENAME: body})
    return result


def test_verify_payload_accepts_a_matching_body(tmp_path: Path) -> None:
    queue = make_queue(tmp_path)
    manifest = make_manifest()
    result = publish_body(queue, manifest, b"the page")
    assert queue.verify_payload(manifest.job_id, result) == queue.payload_path(
        manifest.job_id, BODY_FILENAME
    )


def test_verify_payload_catches_a_size_disagreement(tmp_path: Path) -> None:
    queue = make_queue(tmp_path)
    manifest = make_manifest()
    result = publish_body(queue, manifest, b"the page")
    result = dict(result, bytes=999)
    with pytest.raises(ProtocolError, match="bytes"):
        queue.verify_payload(manifest.job_id, result)


def test_verify_payload_catches_a_hash_disagreement(tmp_path: Path) -> None:
    queue = make_queue(tmp_path)
    manifest = make_manifest()
    result = publish_body(queue, manifest, b"the page")
    result = dict(result, content_sha256="0" * 64)
    with pytest.raises(ProtocolError, match="sha256"):
        queue.verify_payload(manifest.job_id, result)


def test_verify_payload_has_nothing_to_check_for_a_refusal(tmp_path: Path) -> None:
    queue = make_queue(tmp_path)
    refusal = new_result(
        manifest=make_manifest(), outcome="refused", url_norm="", reason="timeout"
    )
    assert queue.verify_payload("JOB-webfetch-WF-01", refusal) is None


def test_sweep_removes_an_ingested_result(tmp_path: Path) -> None:
    queue = make_queue(tmp_path)
    manifest = make_manifest()
    publish_body(queue, manifest, b"x")
    assert queue.sweep(manifest.job_id) is True
    assert queue.state_of(manifest.job_id) == STATE_ABSENT
    assert queue.sweep(manifest.job_id) is False


def test_iter_results_walks_both_terminal_directories(tmp_path: Path) -> None:
    queue = make_queue(tmp_path)
    good = make_manifest(job_id="JOB-good", fetch_id="WF-good")
    bad = make_manifest(job_id="JOB-bad", fetch_id="WF-bad")
    publish_body(queue, good, b"x")
    queue.submit(bad)
    claim = queue.claim(bad.job_id, "worker-1")
    assert claim is not None
    queue.fail(
        claim,
        new_result(manifest=bad, outcome="refused", url_norm="", reason="paywalled"),
    )
    listing = {(state, job_id) for state, job_id, _ in queue.iter_results()}
    assert listing == {(STATE_DONE, "JOB-good"), (STATE_FAILED, "JOB-bad")}


def test_disk_usage_counts_the_whole_queue(tmp_path: Path) -> None:
    queue = make_queue(tmp_path)
    assert queue.disk_usage_bytes() == 0
    publish_body(queue, make_manifest(), b"x" * 1000)
    assert queue.disk_usage_bytes() > 1000


# --------------------------------------------------------------------------
# audit and proposals
# --------------------------------------------------------------------------


def test_append_jsonl_writes_one_line_per_record(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "audit.jsonl"
    append_jsonl(path, {"b": 1, "a": 2})
    append_jsonl(path, {"a": 3})
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines == ['{"a": 2, "b": 1}', '{"a": 3}']


def test_proposals_and_audit_lines_are_timestamped(tmp_path: Path) -> None:
    queue = make_queue(tmp_path)
    queue.append_proposal({"host": "evil.example", "reason": "host_not_allowed"})
    queue.append_audit({"job_id": "JOB-1", "outcome": "refused"})
    proposal = json.loads(queue.proposals_path.read_text(encoding="utf-8").strip())
    audit = json.loads(queue.audit_copy_path.read_text(encoding="utf-8").strip())
    assert proposal["host"] == "evil.example" and proposal["ts"].endswith("Z")
    assert audit["job_id"] == "JOB-1" and audit["ts"].endswith("Z")


def test_manifest_schema_constants_are_the_ones_the_design_pins() -> None:
    assert (MANIFEST_SCHEMA, RESULT_SCHEMA) == (1, 1)


def test_the_privileged_origin_is_not_a_word_the_manifest_can_just_claim() -> None:
    """lane a fix pass (CONT-1), design §4 T2.

    ``origin`` is written by the research container, which is the untrusted
    half of this boundary. ``operator_list`` is what keeps a URL's query
    string and exempts it from the ``agent_daily`` cap, so a manifest that
    merely asserts the label — all a hand-written one has to do — used to get
    both controls disabled for free. The sidecar derives what it acts on
    instead: the label counts only when a delivered list backs it.
    """
    assert make_manifest(origin="agent").effective_origin == "agent"
    assert make_manifest(origin="operator_list").effective_origin == "agent"
    assert make_manifest(origin="operator_list", list_ref="").effective_origin == "agent"
    backed = make_manifest(
        origin="operator_list", list_ref="sha256:" + "0" * 64 + "/deliveries/links.md"
    )
    assert backed.effective_origin == "operator_list"
    # The label itself is preserved: what a manifest CLAIMED is provenance,
    # and the record should not quietly rewrite it.
    assert backed.origin == "operator_list"
    assert make_manifest(origin="operator_list").origin == "operator_list"


def test_the_manifest_constructor_defaults_to_the_unprivileged_origin() -> None:
    fields = {
        "job_id": "JOB-webfetch-WF-01",
        "fetch_id": "WF-01",
        "launch_id": "LNCH-01M1R3J6TZ2HFQM95AEFEW1GY1",
        "url": "https://diataxis.fr/tutorials/",
    }
    assert Manifest.build(**fields).origin == "agent"
