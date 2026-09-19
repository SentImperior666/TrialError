"""lane-b-translator: the ``trialerror feed translate`` /
``trialerror feed translations`` CLI verbs, driven end to end through
``trialerror.cli.main`` (the convention ``tests/test_summarize_cli.py``
uses).

``translate`` ENQUEUES -- it never translates inline and never calls a
model. Every test here therefore asserts on a job, not on a translation,
except the one that runs the enqueued job through the worker to close the
loop.
"""

from __future__ import annotations

import json

from trialerror.cli import feed as cli_feed
from trialerror.cli import main
from trialerror.feed_translate.api import get_translation
from trialerror.jobs.worker import run_one
from trialerror.stores.store import open_store

from tests._feed_translate_fixtures import DENSE_POST, FAITHFUL_TRANSLATION, UNFAITHFUL_TRANSLATION, build_feed


def _call(argv, capsys):
    rc = main(argv)
    out = capsys.readouterr().out.strip()
    return rc, json.loads(out)


def _seed(program_root, platform_root):
    store = open_store(program_root, platform_root=platform_root)
    built = build_feed(store)
    store.close()
    return built


def test_translate_and_translations_are_registered_actions():
    assert cli_feed.GROUP_NAME == "feed"
    assert callable(cli_feed.run_translate)
    assert callable(cli_feed.run_translations)


def test_no_action_names_the_two_new_verbs(program_root, platform_root, capsys):
    # the group parser itself takes no --program-root (each ACTION does), so
    # this one goes through the global flag -- the FX-12 shape.
    rc, env = _call(["--program-root", str(program_root), "feed"], capsys)
    assert rc == 1
    assert "translate" in env["error"]["message"]


def test_translate_requires_exactly_one_target(program_root, platform_root, capsys):
    built = _seed(program_root, platform_root)
    rc, env = _call(["feed", "translate", "--program-root", str(program_root)], capsys)
    assert rc == 1
    assert env["error"]["code"] == "target_required"

    rc, env = _call(
        [
            "feed", "translate", "--program-root", str(program_root),
            "--post-id", built["post_ids"][0], "--pending",
        ],
        capsys,
    )
    assert rc == 1
    assert env["error"]["code"] == "target_required"


def test_translate_refuses_body_without_a_post_id(program_root, platform_root, capsys):
    built = _seed(program_root, platform_root)
    rc, env = _call(
        [
            "feed", "translate", "--program-root", str(program_root),
            "--thread-id", built["thread_id"], "--body", "plain text",
        ],
        capsys,
    )
    assert rc == 1
    assert env["error"]["code"] == "body_needs_post_id"


def test_translate_post_enqueues_a_job_and_names_the_worker_next_action(program_root, platform_root, capsys):
    built = _seed(program_root, platform_root)
    rc, env = _call(
        ["feed", "translate", "--program-root", str(program_root), "--post-id", built["post_ids"][0]],
        capsys,
    )
    assert rc == 0
    assert env["result"]["status"] == "enqueued"
    assert env["result"]["targets"] == 1
    job = env["result"]["job"]
    assert job["kind"] == "custom"
    assert job["state"] == "pending"
    assert json.loads(job["payload"])["handler"] == "feed_translate"
    assert any("start-worker" in " ".join(na["argv"]) for na in env["nextActions"])


def test_translate_pending_counts_the_backlog(program_root, platform_root, capsys):
    store = open_store(program_root, platform_root=platform_root)
    build_feed(store, bodies=[DENSE_POST, "Another post.", "A third post."])
    store.close()
    rc, env = _call(["feed", "translate", "--program-root", str(program_root), "--pending"], capsys)
    assert rc == 0
    assert env["result"]["targets"] == 3


def test_translate_body_round_trips_through_the_job_to_a_stored_translation(
    program_root, platform_root, capsys
):
    built = _seed(program_root, platform_root)
    post_id = built["post_ids"][0]
    rc, env = _call(
        [
            "feed", "translate", "--program-root", str(program_root),
            "--post-id", post_id, "--body", FAITHFUL_TRANSLATION, "--by-launch", built["launch_id"],
        ],
        capsys,
    )
    assert rc == 0
    job_id = env["result"]["job"]["job_id"]

    store = open_store(program_root, platform_root=platform_root)
    try:
        run_one(store, job_id=job_id)
        row = get_translation(store, post_id=post_id)
        assert row["body"] == FAITHFUL_TRANSLATION
        assert row["gate_status"] == "pass"
        assert row["created_by_launch"] == built["launch_id"]
    finally:
        store.close()

    rc, env = _call(
        ["feed", "translations", "--program-root", str(program_root), "--post-id", post_id], capsys
    )
    assert rc == 0
    assert env["result"]["translation"]["body"] == FAITHFUL_TRANSLATION


def test_translate_judgments_file_carries_a_batch(program_root, platform_root, tmp_path, capsys):
    store = open_store(program_root, platform_root=platform_root)
    built = build_feed(store, bodies=[DENSE_POST, "Gate G-9 is pending review."])
    store.close()
    a, b = built["post_ids"]
    judgments = tmp_path / "translations.json"
    judgments.write_text(
        json.dumps({a: FAITHFUL_TRANSLATION, b: "Gate G-9 is still pending a review."}), encoding="utf-8"
    )

    rc, env = _call(
        [
            "feed", "translate", "--program-root", str(program_root),
            "--thread-id", built["thread_id"], "--judgments-file", str(judgments),
        ],
        capsys,
    )
    assert rc == 0
    job_id = env["result"]["job"]["job_id"]

    store = open_store(program_root, platform_root=platform_root)
    try:
        run_one(store, job_id=job_id)
        assert get_translation(store, post_id=a)["gate_status"] == "pass"
        assert get_translation(store, post_id=b)["gate_status"] == "pass"
    finally:
        store.close()


def test_translate_refuses_one_claim_file_without_the_other(program_root, platform_root, tmp_path, capsys):
    built = _seed(program_root, platform_root)
    decomp = tmp_path / "decomp.json"
    decomp.write_text("{}", encoding="utf-8")
    rc, env = _call(
        [
            "feed", "translate", "--program-root", str(program_root),
            "--post-id", built["post_ids"][0], "--body", FAITHFUL_TRANSLATION,
            "--claim-decomposition-file", str(decomp),
        ],
        capsys,
    )
    assert rc == 1
    assert env["error"]["code"] == "claim_files_incomplete"


def test_translate_claim_files_round_trip_to_the_judged_tier_via_the_job(
    program_root, platform_root, tmp_path, capsys
):
    """FT-1 (fix pass), end to end through the CLI: a meaning-inverting
    negation that keeps every id/number/date/hedge is enqueued with
    --claim-decomposition-file/--claim-judgments-file, and the judged tier
    withholds it -- the same job path a live worker runs."""
    built = _seed(program_root, platform_root)
    post_id = built["post_ids"][0]
    negation = (
        "We did NOT book LNCH-01JXYZ4 against pool P-2 on 2026-09-05. "
        "The match-up is still pending. "
        "So the 3 gates after it stay deferred."
    )
    decomp = tmp_path / "decomp.json"
    decomp.write_text(
        json.dumps(
            {
                f"{post_id}::S-1": {"claims": ["the booking was not made"]},
                f"{post_id}::S-2": {"claims": ["the match-up is pending"]},
                f"{post_id}::S-3": {"claims": ["the 3 gates stay deferred"]},
            }
        ),
        encoding="utf-8",
    )
    claim_judgments = tmp_path / "claim_judgments.json"
    claim_judgments.write_text(
        json.dumps(
            {
                f"{post_id}::S-1::CLM-1": {"label": "unsupported"},
                f"{post_id}::S-2::CLM-1": {"label": "supported"},
                f"{post_id}::S-3::CLM-1": {"label": "supported"},
            }
        ),
        encoding="utf-8",
    )

    rc, env = _call(
        [
            "feed", "translate", "--program-root", str(program_root),
            "--post-id", post_id, "--body", negation,
            "--claim-decomposition-file", str(decomp), "--claim-judgments-file", str(claim_judgments),
        ],
        capsys,
    )
    assert rc == 0
    job_id = env["result"]["job"]["job_id"]

    store = open_store(program_root, platform_root=platform_root)
    try:
        run_one(store, job_id=job_id)
        row = get_translation(store, post_id=post_id)
        assert row["gate_status"] == "fail"
        assert row["faithfulness_score"] < 1.0
    finally:
        store.close()


def test_translations_can_list_only_what_the_gate_withheld(program_root, platform_root, capsys):
    built = _seed(program_root, platform_root)
    post_id = built["post_ids"][0]
    rc, env = _call(
        [
            "feed", "translate", "--program-root", str(program_root),
            "--post-id", post_id, "--body", UNFAITHFUL_TRANSLATION,
        ],
        capsys,
    )
    store = open_store(program_root, platform_root=platform_root)
    try:
        run_one(store, job_id=env["result"]["job"]["job_id"])
    finally:
        store.close()

    rc, env = _call(
        ["feed", "translations", "--program-root", str(program_root), "--gate-status", "fail"], capsys
    )
    assert rc == 0
    assert env["result"]["count"] == 1
    assert env["result"]["translations"][0]["post_id"] == post_id


def test_translations_reports_a_clean_not_found(program_root, platform_root, capsys):
    built = _seed(program_root, platform_root)
    rc, env = _call(
        ["feed", "translations", "--program-root", str(program_root), "--post-id", built["post_ids"][0]],
        capsys,
    )
    assert rc == 1
    assert env["error"]["code"] == "not_found"
