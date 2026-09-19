"""Lane FB-1 item F4: acquisition says it is not searchable yet.

"acquired" reads as done. It is not: a source is registered, the raw file is
written, and the FIRST pipeline stage is enqueued -- nothing is normalized,
chunked, embedded or indexed until a worker runs that job. These tests pin
the two keys that say so and the two next actions that say what to do.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from trialerror.cli import ingest as cli_ingest
from trialerror.ingest import pipeline, pipeline_status
from trialerror.ingest.acquire import AcquireResult

from tests._ingest_fixtures import bootstrap_launch, write_html_fixture


class _Args:
    def __init__(self, **kw):
        self.program_root = None
        self.platform_root = None
        self.media_type = None
        self.yes = False
        for k, v in kw.items():
            setattr(self, k, v)


# ---------------------------------------------------------------------------
# AcquireResult
# ---------------------------------------------------------------------------


def test_a_job_enqueued_means_not_searchable_and_names_the_pending_stage():
    result = AcquireResult(
        outcome="acquired",
        source={"source_id": "SRC-1"},
        document={"doc_id": "DOC-1"},
        job={"job_id": "JOB-1", "kind": "ocr", "payload": "{}"},
    )
    assert result.searchable is False
    assert result.pending_stage == "ocr"
    assert result.to_dict()["searchable"] is False
    assert result.to_dict()["pending_stage"] == "ocr"


def test_the_pending_stage_is_read_off_the_job_not_guessed():
    """A custom-handler stage (the DjVu route) rides ``kind='custom'`` with
    the handler in the payload; the stage a caller is waiting on is the
    handler's own name, which only the job row knows."""
    result = AcquireResult(
        outcome="acquired",
        source={},
        document={"doc_id": "DOC-1"},
        job={"job_id": "JOB-1", "kind": "custom", "payload": '{"handler": "djvu"}'},
    )
    assert result.pending_stage == "djvu"


def test_an_already_ingested_dedup_is_searchable_with_nothing_pending():
    """The one acquired outcome with no job: the source deduped onto a
    document this program has already run the pipeline for."""
    result = AcquireResult(outcome="acquired", source={"source_id": "SRC-1"}, document={"doc_id": "DOC-1"})
    assert result.searchable is True
    assert result.pending_stage is None


def test_a_request_queued_acquisition_is_not_searchable_either():
    """Nothing was acquired at all -- there is no document to find, and
    reporting `searchable: true` because no job is pending would be the
    wrong answer for the opposite reason."""
    result = AcquireResult(outcome="queued", source={"source_id": "SRC-1", "request_state": "wanted"})
    assert result.searchable is False
    assert result.pending_stage is None


# ---------------------------------------------------------------------------
# the two next actions
# ---------------------------------------------------------------------------


def test_the_two_next_actions_name_the_job_and_the_inline_way_to_run_it():
    actions = pipeline_status.not_yet_searchable_next_actions({"job_id": "JOB-7"})
    argvs = [a.argv for a in actions]
    assert argvs == [
        ["trialerror", "jobs", "start-worker", "--job-id", "JOB-7"],
        ["trialerror", "jobs", "start-worker", "--foreground", "--job-id", "JOB-7"],
    ]
    assert "not searchable until it completes" in actions[0].description


def test_no_actions_without_a_job():
    assert pipeline_status.not_yet_searchable_next_actions(None) == []
    assert pipeline_status.not_yet_searchable_next_actions({}) == []


def test_both_next_actions_parse_under_the_top_level_parser():
    from trialerror.cli import build_parser

    parser = build_parser()
    for action in pipeline_status.not_yet_searchable_next_actions({"job_id": "JOB-7"}):
        parsed = parser.parse_args(list(action.argv)[1:])
        assert parsed.job_id == "JOB-7"
    # the second one is the foreground form -- pinned, because `--drain` was
    # the alternative this item deliberately did NOT add.
    second = pipeline_status.not_yet_searchable_next_actions({"job_id": "JOB-7"})[1]
    assert parser.parse_args(list(second.argv)[1:]).foreground is True


def test_no_drain_flag_was_added_to_ingest_add():
    from trialerror.cli import build_parser

    with pytest.raises(SystemExit):
        build_parser().parse_args(
            ["ingest", "add", "--source-id", "S", "--path", "p", "--launch-id", "L", "--drain"]
        )


# ---------------------------------------------------------------------------
# `ingest add` says the same two things
# ---------------------------------------------------------------------------


def test_ingest_add_reports_not_searchable_and_both_actions(store, program_root, platform_root):
    launch_id = bootstrap_launch(store)
    source = pipeline.register_source(
        store, kind="web", title="T", license_tier="open", acquisition_route="web",
        registered_by_launch=launch_id,
    )
    raw_dir = program_root / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    raw = write_html_fixture(raw_dir / "fixture.html")
    store.close()

    env = cli_ingest._cmd_add(
        _Args(
            program_root=str(program_root), platform_root=str(platform_root),
            source_id=source["source_id"], path=str(raw), launch_id=launch_id,
        )
    )
    assert env["ok"] is True
    assert env["result"]["searchable"] is False
    assert env["result"]["pending_stage"] == "normalize"
    descriptions = [a["description"] for a in env["nextActions"]]
    assert "not searchable until it completes" in descriptions[0]
    assert env["nextActions"][1]["argv"][:5] == [
        "trialerror", "jobs", "start-worker", "--foreground", "--job-id",
    ]


def test_the_sentence_lives_in_one_place(store, program_root, platform_root):
    """`ingest add` and `lit acquire` must not drift into two wordings for
    the same fact, so both read the same constant."""
    assert pipeline_status.NOT_SEARCHABLE_UNTIL_RUN in (
        pipeline_status.not_yet_searchable_next_actions({"job_id": "J"})[0].description
    )
    lit_source = (Path(__file__).resolve().parents[1] / "trialerror" / "cli" / "lit.py").read_text(
        encoding="utf-8"
    )
    ingest_source = (Path(__file__).resolve().parents[1] / "trialerror" / "cli" / "ingest.py").read_text(
        encoding="utf-8"
    )
    assert "not_yet_searchable_next_actions" in lit_source
    assert "not_yet_searchable_next_actions" in ingest_source
    assert "run the enqueued pipeline job" not in lit_source
    assert "run the enqueued pipeline job" not in ingest_source
