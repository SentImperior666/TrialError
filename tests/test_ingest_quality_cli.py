"""Tests for ``trialerror ingest quality`` -- the read-only verb, one
document or the corpus.
"""

from __future__ import annotations

import json

import pytest

from trialerror.cli import ingest as cli_ingest
from trialerror.ingest import quality
from tests._ingest_fixtures import bootstrap_launch
from tests._quality_fixtures import CLEAN_TEXT, GLUED_TEXT, seed_document, seed_many_documents, seed_quality_corpus


class _Args:
    def __init__(self, **kw):
        self.program_root = None
        self.platform_root = None
        self.doc_id = None
        self.all_docs = False
        self.sample = None
        self.seed = None
        self.worst = None
        for k, v in kw.items():
            setattr(self, k, v)


def _quality(program_root, platform_root, **kw) -> dict:
    return cli_ingest._cmd_quality(
        _Args(program_root=str(program_root), platform_root=str(platform_root), **kw)
    )


# ---------------------------------------------------------------------------
# the parser
# ---------------------------------------------------------------------------
def test_the_verb_is_registered_with_its_flags():
    import argparse

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="group")
    cli_ingest.register(sub)
    args = parser.parse_args(["ingest", "quality", "--all", "--sample", "5", "--seed", "2", "--worst", "3"])
    assert args.all_docs is True
    assert (args.sample, args.seed, args.worst) == (5, 2, 3)
    assert args.handler is cli_ingest._cmd_quality


# ---------------------------------------------------------------------------
# usage refusals
# ---------------------------------------------------------------------------
def test_neither_doc_id_nor_all_is_refused(store, program_root, platform_root):
    store.close()
    env = _quality(program_root, platform_root)
    assert env["ok"] is False
    assert env["error"]["code"] == "usage"


def test_both_doc_id_and_all_is_refused(store, program_root, platform_root):
    store.close()
    env = _quality(program_root, platform_root, doc_id="DOC-1", all_docs=True)
    assert env["ok"] is False
    assert env["error"]["code"] == "usage"


def test_sample_without_all_is_refused(store, program_root, platform_root):
    store.close()
    env = _quality(program_root, platform_root, doc_id="DOC-1", sample=5)
    assert env["ok"] is False
    assert "--all" in env["error"]["message"]


def test_worst_without_all_is_refused(store, program_root, platform_root):
    """Fix pass V-7: --worst was silently ignored beside --doc-id while
    --sample/--seed were refused -- one flag short of the same guard."""
    store.close()
    env = _quality(program_root, platform_root, doc_id="DOC-1", worst=2)
    assert env["ok"] is False
    assert env["error"]["code"] == "usage"
    assert "--worst" in env["error"]["message"]


def test_every_corpus_flag_is_named_in_one_refusal(store, program_root, platform_root):
    store.close()
    env = _quality(program_root, platform_root, doc_id="DOC-1", sample=5, seed=1, worst=2)
    assert env["ok"] is False
    for flag in ("--sample", "--seed", "--worst"):
        assert flag in env["error"]["message"]


@pytest.mark.parametrize("sample", [0, -1])
def test_a_non_positive_sample_is_refused(store, program_root, platform_root, sample):
    """V-6's CLI half: --sample 0 measures nothing and --sample -1 reached
    the sampler as "no sample" -- both are spelled --all with no --sample."""
    store.close()
    env = _quality(program_root, platform_root, all_docs=True, sample=sample)
    assert env["ok"] is False
    assert env["error"]["code"] == "usage"


def test_an_unknown_doc_id_is_an_error_envelope(store, program_root, platform_root):
    store.close()
    env = _quality(program_root, platform_root, doc_id="DOC-nope")
    assert env["ok"] is False
    assert env["error"]["code"] == "DocumentNotFoundError"


# ---------------------------------------------------------------------------
# --doc-id
# ---------------------------------------------------------------------------
def test_doc_id_reports_the_four_numbers_and_the_verdict(store, program_root, platform_root):
    launch_id = bootstrap_launch(store)
    doc_id = seed_document(store, launch_id, GLUED_TEXT)
    store.close()
    env = _quality(program_root, platform_root, doc_id=doc_id)
    assert env["ok"] is True
    row = env["result"]["document"]
    for key in quality.MEASURE_KEYS:
        assert key in row
    assert row["suspect"] is True
    assert row["thresholds"]["glued_token_rate_max"] == quality.DEFAULT_THRESHOLDS["glued_token_rate_max"]
    assert env["nextActions"][0]["argv"] == ["trialerror", "ingest", "status", "--doc-id", doc_id]


def test_doc_id_on_a_clean_document_suggests_nothing(store, program_root, platform_root):
    launch_id = bootstrap_launch(store)
    doc_id = seed_document(store, launch_id, [CLEAN_TEXT, CLEAN_TEXT], page_count=2)
    store.close()
    env = _quality(program_root, platform_root, doc_id=doc_id)
    assert env["result"]["document"]["suspect"] is False
    assert env["nextActions"] == []


# ---------------------------------------------------------------------------
# --all
# ---------------------------------------------------------------------------
def test_all_measures_every_document_and_ranks_them(store, program_root, platform_root):
    launch_id = bootstrap_launch(store)
    docs = seed_quality_corpus(store, launch_id)
    store.close()
    env = _quality(program_root, platform_root, all_docs=True)
    result = env["result"]
    assert result["corpus_documents"] == 4
    assert result["measured"] == 4
    assert result["suspect_count"] == 3
    assert result["worst"][0]["doc_id"] != docs["clean"]
    assert result["sample"] is None and result["seed"] is None


def test_all_reports_which_measure_each_suspect_tripped(store, program_root, platform_root):
    launch_id = bootstrap_launch(store)
    seed_quality_corpus(store, launch_id)
    store.close()
    by_measure = _quality(program_root, platform_root, all_docs=True)["result"]["suspect_by_measure"]
    assert by_measure["glued_token_rate"] == 1
    assert by_measure["unusable_char_count"] == 1
    assert by_measure["terminator_density"] == 3


def test_all_with_a_sample_measures_only_that_many_and_records_the_seed(store, program_root, platform_root):
    launch_id = bootstrap_launch(store)
    seed_many_documents(store, launch_id, 9)
    store.close()
    env = _quality(program_root, platform_root, all_docs=True, sample=3, seed=5)
    result = env["result"]
    assert (result["corpus_documents"], result["measured"]) == (9, 3)
    assert (result["sample"], result["seed"]) == (3, 5)


def test_worst_bounds_the_reported_rows(store, program_root, platform_root):
    launch_id = bootstrap_launch(store)
    for _ in range(6):
        seed_document(store, launch_id, GLUED_TEXT)
    store.close()
    env = _quality(program_root, platform_root, all_docs=True, worst=2)
    assert len(env["result"]["worst"]) == 2
    assert env["result"]["suspect_count"] == 6


def test_all_on_an_empty_corpus_answers_rather_than_raising(store, program_root, platform_root):
    store.close()
    env = _quality(program_root, platform_root, all_docs=True)
    assert env["ok"] is True
    assert env["result"] == {
        **env["result"],
        "corpus_documents": 0,
        "measured": 0,
        "suspect_count": 0,
        "worst": [],
    }


def test_the_envelope_is_json_serializable(store, program_root, platform_root):
    launch_id = bootstrap_launch(store)
    seed_quality_corpus(store, launch_id)
    store.close()
    env = _quality(program_root, platform_root, all_docs=True)
    assert json.loads(json.dumps(env))["command"] == "ingest.quality"


def test_the_verb_writes_nothing(store, program_root, platform_root):
    """Read-only is the contract the orchestrator runs this under on a live
    program."""
    launch_id = bootstrap_launch(store)
    seed_quality_corpus(store, launch_id)

    def _snapshot():
        return {
            table: store.knowledge.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("document", "element", "chunk", "record")
        }

    before = _snapshot()
    store.close()
    _quality(program_root, platform_root, all_docs=True)

    from trialerror.stores.store import open_store

    reopened = open_store(program_root, platform_root=platform_root)
    try:
        after = {
            table: reopened.knowledge.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("document", "element", "chunk", "record")
        }
    finally:
        reopened.close()
    assert before == after


# ---------------------------------------------------------------------------
# FB-1b item 4: the size floor, from the verb's side
# ---------------------------------------------------------------------------
TINY_NOTE = "sync the offload queue then re-run the worker"  # 8 tokens, no terminator


def test_all_counts_the_documents_below_the_size_floor(store, program_root, platform_root):
    launch_id = bootstrap_launch(store)
    for _ in range(3):
        seed_document(store, launch_id, TINY_NOTE)
    seed_document(store, launch_id, GLUED_TEXT)
    store.close()
    env = _quality(program_root, platform_root, all_docs=True)
    result = env["result"]
    assert result["suspect_count"] == 1
    assert result["below_min_tokens"] == 3
    assert result["thresholds"]["min_tokens"] == quality.DEFAULT_MIN_TOKENS


def test_doc_id_reports_the_flag_beside_the_numbers(store, program_root, platform_root):
    launch_id = bootstrap_launch(store)
    doc_id = seed_document(store, launch_id, TINY_NOTE)
    store.close()
    row = _quality(program_root, platform_root, doc_id=doc_id)["result"]["document"]
    assert row["below_min_tokens"] is True
    assert row["suspect"] is False
    assert row["reasons"] == []
    assert row["terminator_density"] == 0.0  # measured, just not judged
