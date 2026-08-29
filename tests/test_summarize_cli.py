"""``trialerror summarize`` CLI group -- argv parsing, envelope shaping, and
auto-discovery, driven end to end through ``trialerror.cli.main`` (same
convention ``tests/test_verify_cli.py`` uses)."""

from __future__ import annotations

import json

from trialerror.cli import main
from trialerror.cli import summarize as cli_summarize
from trialerror.jobs.worker import run_one

from tests._summarize_fixtures import bootstrap_launch, build_small_corpus


def _call(argv, capsys):
    rc = main(argv)
    out = capsys.readouterr().out.strip()
    return rc, json.loads(out)


def test_group_name_and_help_registered():
    assert cli_summarize.GROUP_NAME == "summarize"
    assert cli_summarize.HELP


def test_no_action_is_a_structured_error(program_root, platform_root, capsys):
    rc, env = _call(["summarize", "--program-root", str(program_root), "--platform-root", str(platform_root)], capsys)
    assert rc == 1
    assert env["error"]["code"] == "no_action"


# ---------------------------------------------------------------------------
# run -- with --body (synchronous, immediate store)
# ---------------------------------------------------------------------------


def test_run_with_body_stores_immediately(store, program_root, platform_root, capsys):
    corpus = build_small_corpus(store)
    store.close()

    rc, env = _call(
        [
            "summarize", "run", "--subject-kind", "document", "--subject-id", corpus["open_doc_id"],
            "--by-launch", corpus["launch_id"], "--body", "A CLI-authored overview of the open source.",
            "--program-root", str(program_root), "--platform-root", str(platform_root),
        ],
        capsys,
    )
    assert rc == 0
    assert env["result"]["status"] == "stored"
    assert env["result"]["summary"]["body"] == "A CLI-authored overview of the open source."


def test_run_without_body_returns_pending_envelope(store, program_root, platform_root, capsys):
    corpus = build_small_corpus(store)
    store.close()

    rc, env = _call(
        [
            "summarize", "run", "--subject-kind", "document", "--subject-id", corpus["open_doc_id"],
            "--by-launch", corpus["launch_id"], "--program-root", str(program_root), "--platform-root", str(platform_root),
        ],
        capsys,
    )
    assert rc == 0
    assert env["result"]["status"] == "pending_judgment"
    assert env["result"]["envelope"]["subject_id"] == corpus["open_doc_id"]


def test_run_with_judgments_file(store, program_root, platform_root, capsys):
    corpus = build_small_corpus(store)
    store.close()

    judgments_file = program_root / "judgments.json"
    judgments_file.write_text(json.dumps({corpus["open_doc_id"]: "An overview supplied via judgments file."}), encoding="utf-8")

    rc, env = _call(
        [
            "summarize", "run", "--subject-kind", "document", "--subject-id", corpus["open_doc_id"],
            "--by-launch", corpus["launch_id"], "--judgments-file", str(judgments_file),
            "--program-root", str(program_root), "--platform-root", str(platform_root),
        ],
        capsys,
    )
    assert rc == 0
    assert env["result"]["status"] == "stored"
    assert env["result"]["summary"]["body"] == "An overview supplied via judgments file."


def test_run_missing_subject_id_without_batch_is_a_structured_error(program_root, platform_root, capsys):
    rc, env = _call(
        ["summarize", "run", "--by-launch", "LNCH-x", "--program-root", str(program_root), "--platform-root", str(platform_root)],
        capsys,
    )
    assert rc == 1
    assert env["error"]["code"] == "missing_subject_id"


def test_run_missing_by_launch_is_a_structured_error(store, program_root, platform_root, capsys):
    corpus = build_small_corpus(store)
    store.close()
    rc, env = _call(
        [
            "summarize", "run", "--subject-id", corpus["open_doc_id"], "--body", "x",
            "--program-root", str(program_root), "--platform-root", str(platform_root),
        ],
        capsys,
    )
    assert rc == 1
    assert env["error"]["code"] == "missing_by_launch"


def test_run_unnormalized_document_is_a_summarize_refused_error(store, program_root, platform_root, capsys):
    from trialerror.stores.writer import insert
    from trialerror.util.ids import new_id

    corpus = build_small_corpus(store)
    empty_doc_id = new_id("DOC")
    insert(
        store, "document",
        {
            "doc_id": empty_doc_id, "source_id": corpus["open_source_id"], "rel_path": "archive/empty.md",
            "media_type": "md", "normalizer_id": "fixture", "normalizer_version": "1",
            "sha256": "0" * 64, "status": "registered",
        },
    )
    store.close()

    rc, env = _call(
        [
            "summarize", "run", "--subject-id", empty_doc_id, "--by-launch", corpus["launch_id"],
            "--body", "x", "--program-root", str(program_root), "--platform-root", str(platform_root),
        ],
        capsys,
    )
    assert rc == 1
    assert env["error"]["code"] == "summarize_refused"


# ---------------------------------------------------------------------------
# run --batch -- enqueues a job on the M2 ledger
# ---------------------------------------------------------------------------


def test_run_batch_enqueues_a_job_that_a_worker_can_drain(store, program_root, platform_root, capsys):
    corpus = build_small_corpus(store)
    store.close()

    judgments_file = program_root / "batch_judgments.json"
    judgments_file.write_text(
        json.dumps({corpus["open_doc_id"]: "batch overview one", corpus["restricted_doc_id"]: "batch overview two"}),
        encoding="utf-8",
    )

    rc, env = _call(
        [
            "summarize", "run", "--batch", "--by-launch", corpus["launch_id"],
            "--judgments-file", str(judgments_file), "--program-root", str(program_root), "--platform-root", str(platform_root),
        ],
        capsys,
    )
    assert rc == 0
    assert env["result"]["status"] == "enqueued"
    job_id = env["result"]["job"]["job_id"]
    assert env["result"]["job"]["kind"] == "custom"

    from trialerror.stores.store import open_store

    worker_store = open_store(program_root, platform_root=platform_root)
    try:
        result = run_one(worker_store, job_id=job_id, kind="custom")
        assert result["status"] == "complete"
        from trialerror.summarize.api import get_summary

        assert get_summary(worker_store, subject_kind="document", subject_id=corpus["open_doc_id"])["body"] == "batch overview one"
        assert get_summary(worker_store, subject_kind="document", subject_id=corpus["restricted_doc_id"])["body"] == "batch overview two"
    finally:
        worker_store.close()


def test_run_batch_collection_is_a_structured_error(program_root, platform_root, capsys):
    rc, env = _call(
        [
            "summarize", "run", "--batch", "--subject-kind", "collection", "--by-launch", "LNCH-x",
            "--program-root", str(program_root), "--platform-root", str(platform_root),
        ],
        capsys,
    )
    assert rc == 1
    assert env["error"]["code"] == "batch_collection_unsupported"


def test_run_batch_missing_by_launch_is_a_structured_error(program_root, platform_root, capsys):
    rc, env = _call(
        ["summarize", "run", "--batch", "--program-root", str(program_root), "--platform-root", str(platform_root)],
        capsys,
    )
    assert rc == 1
    assert env["error"]["code"] == "missing_by_launch"


# ---------------------------------------------------------------------------
# show / list
# ---------------------------------------------------------------------------


def test_show_by_id_and_not_found(store, program_root, platform_root, capsys):
    corpus = build_small_corpus(store)
    store.close()

    rc, env = _call(
        [
            "summarize", "run", "--subject-id", corpus["open_doc_id"], "--by-launch", corpus["launch_id"],
            "--body", "shown overview", "--program-root", str(program_root), "--platform-root", str(platform_root),
        ],
        capsys,
    )
    summary_id = env["result"]["summary"]["summary_id"]

    rc, env = _call(
        ["summarize", "show", "--id", summary_id, "--program-root", str(program_root), "--platform-root", str(platform_root)],
        capsys,
    )
    assert rc == 0
    assert env["result"]["summary"]["body"] == "shown overview"

    rc, env = _call(
        ["summarize", "show", "--id", "SUM-does-not-exist", "--program-root", str(program_root), "--platform-root", str(platform_root)],
        capsys,
    )
    assert rc == 1
    assert env["error"]["code"] == "not_found"


def test_show_by_subject_kind_and_id(store, program_root, platform_root, capsys):
    corpus = build_small_corpus(store)
    store.close()

    _call(
        [
            "summarize", "run", "--subject-id", corpus["open_doc_id"], "--by-launch", corpus["launch_id"],
            "--body", "subject-lookup overview", "--program-root", str(program_root), "--platform-root", str(platform_root),
        ],
        capsys,
    )
    rc, env = _call(
        [
            "summarize", "show", "--subject-kind", "document", "--subject-id", corpus["open_doc_id"],
            "--program-root", str(program_root), "--platform-root", str(platform_root),
        ],
        capsys,
    )
    assert rc == 0
    assert env["result"]["summary"]["body"] == "subject-lookup overview"


def test_show_missing_lookup_key_is_a_structured_error(program_root, platform_root, capsys):
    rc, env = _call(
        ["summarize", "show", "--program-root", str(program_root), "--platform-root", str(platform_root)],
        capsys,
    )
    assert rc == 1
    assert env["error"]["code"] == "missing_lookup_key"


def test_list_round_trip(store, program_root, platform_root, capsys):
    corpus = build_small_corpus(store)
    store.close()

    for doc_key in ("open_doc_id", "restricted_doc_id"):
        _call(
            [
                "summarize", "run", "--subject-id", corpus[doc_key], "--by-launch", corpus["launch_id"],
                "--body", f"overview for {corpus[doc_key]}", "--program-root", str(program_root), "--platform-root", str(platform_root),
            ],
            capsys,
        )

    rc, env = _call(["summarize", "list", "--program-root", str(program_root), "--platform-root", str(platform_root)], capsys)
    assert rc == 0
    assert env["result"]["count"] == 2

    rc, env = _call(
        [
            "summarize", "list", "--subject-id", corpus["open_doc_id"],
            "--program-root", str(program_root), "--platform-root", str(platform_root),
        ],
        capsys,
    )
    assert env["result"]["count"] == 1
