"""``trialerror.retrieve.probes`` -- the full-text and vector canaries, and
``embed_coverage`` (design Section 3.4). Uses the real ``store`` fixture and
``tests/_retrieve_fixtures.build_small_corpus``, which populates
``chunk_fts``/``emb``/``vec_chunks__<model_key>`` exactly the way real
ingestion does (that fixture's own docstring)."""

from __future__ import annotations

import pytest

from trialerror.probes.registry import ProbeContext
from trialerror.retrieve import probes as retrieve_probes
from trialerror.stores.store import open_store

from tests._retrieve_fixtures import build_small_corpus


@pytest.fixture()
def store(platform_root, program_root):
    """Overrides conftest's own ``store`` fixture: this file's probe bodies
    run on trialerror.probes.registry's per-probe timeout thread, so every
    connection must tolerate cross-thread use (see connect()'s and
    open_store()'s own docstrings)."""
    s = open_store(program_root, platform_root=platform_root, check_same_thread=False)
    yield s
    s.close()


@pytest.fixture()
def corpus(store):
    return build_small_corpus(store)


def _a_long_chunk(store) -> dict:
    row = store.knowledge.execute("SELECT chunk_id, text FROM chunk WHERE length(text) >= 200 LIMIT 1").fetchone()
    assert row is not None, "fixture corpus must have at least one chunk >= 200 chars for these tests"
    return dict(row)


def test_fulltext_canary_passes_on_a_seeded_corpus(store, corpus):
    ctx = ProbeContext(host="h", platform_store=None, store=store)
    result = retrieve_probes.probe_fulltext_canary(ctx)
    assert result.status == "pass"
    assert result.detail["found"] is True


def test_fulltext_canary_skips_on_an_empty_corpus(store):
    ctx = ProbeContext(host="h", platform_store=None, store=store)
    result = retrieve_probes.probe_fulltext_canary(ctx)
    assert result.status == "skip"


def test_fulltext_canary_fails_when_the_chunk_is_not_returned(store, corpus, monkeypatch):
    monkeypatch.setattr(retrieve_probes, "fts_search", lambda *a, **k: [])
    ctx = ProbeContext(host="h", platform_store=None, store=store)
    result = retrieve_probes.probe_fulltext_canary(ctx)
    assert result.status == "fail"


def test_fulltext_canary_fails_when_search_raises(store, corpus, monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("index is broken")

    monkeypatch.setattr(retrieve_probes, "fts_search", _boom)
    ctx = ProbeContext(host="h", platform_store=None, store=store)
    result = retrieve_probes.probe_fulltext_canary(ctx)
    assert result.status == "fail"
    assert "index is broken" in result.detail["reason"]


def test_degraded_line_is_none_on_pass(store, corpus):
    ctx = ProbeContext(host="h", platform_store=None, store=store)
    result = retrieve_probes.probe_fulltext_canary(ctx)
    assert retrieve_probes.fulltext_canary_degraded_line(result.status, result.detail) is None


def test_degraded_line_on_failure_matches_the_design_wording(store, corpus, monkeypatch):
    monkeypatch.setattr(retrieve_probes, "fts_search", lambda *a, **k: [])
    ctx = ProbeContext(host="h", platform_store=None, store=store)
    result = retrieve_probes.probe_fulltext_canary(ctx)
    line = retrieve_probes.fulltext_canary_degraded_line(result.status, result.detail)
    assert line.startswith("SEARCH DEGRADED: full-text search did not find a known passage")
    assert "An empty search result is not evidence that something is absent." in line


def test_degraded_line_pure_formatter_on_a_bare_status_and_detail():
    """The B-1 fix's whole point: this is a pure formatter over an
    ALREADY-RUN result's status/detail, no ProbeContext or execution
    needed."""
    assert retrieve_probes.fulltext_canary_degraded_line("pass", {}) is None
    assert retrieve_probes.fulltext_canary_degraded_line("skip", {"reason": "empty corpus"}) is None
    line = retrieve_probes.fulltext_canary_degraded_line("fail", {"reason": "boom"})
    assert "boom" in line
    line_no_detail = retrieve_probes.fulltext_canary_degraded_line("fail", None)
    assert "the canary chunk was not returned" in line_no_detail


def test_vector_canary_passes_on_a_seeded_corpus(store, corpus):
    ctx = ProbeContext(host="h", platform_store=None, store=store)
    result = retrieve_probes.probe_vector_canary(ctx)
    assert result.status == "pass"


def test_vector_canary_skips_on_an_empty_corpus(store):
    ctx = ProbeContext(host="h", platform_store=None, store=store)
    result = retrieve_probes.probe_vector_canary(ctx)
    assert result.status == "skip"


def test_embed_coverage_passes_when_fully_embedded(store, corpus):
    ctx = ProbeContext(host="h", platform_store=None, store=store)
    result = retrieve_probes.probe_embed_coverage(ctx)
    assert result.status == "pass"
    assert result.detail["pct_embedded"] == 100.0


def test_embed_coverage_warns_below_95_percent(store, corpus):
    # add chunks with no embedding at all, dragging coverage down
    for i in range(30):
        store.knowledge.execute(
            "INSERT INTO chunk (chunk_id, doc_id, seq, text, token_count, element_first, element_last, "
            "sha256, chunker_id, chunker_version, created_ts) SELECT ?, doc_id, 999 + ?, 'unembedded text', "
            "3, element_first, element_last, ?, chunker_id, chunker_version, created_ts FROM chunk LIMIT 1",
            (f"CHK-unembedded-{i}", i, f"deadbeef{i:04d}"),
        )
    ctx = ProbeContext(host="h", platform_store=None, store=store)
    result = retrieve_probes.probe_embed_coverage(ctx)
    assert result.status == "warn"
    assert result.detail["pct_embedded"] < 95


def test_embed_coverage_skips_on_empty_corpus(store):
    ctx = ProbeContext(host="h", platform_store=None, store=store)
    assert retrieve_probes.probe_embed_coverage(ctx).status == "skip"


# ---------------------------------------------------------------------------
# the vector_canary job handler + hourly enqueue
# ---------------------------------------------------------------------------


def test_job_handler_runs_the_probe_and_records_a_row(store, corpus):
    from trialerror.retrieve.handlers import run_probe_vector_canary

    class _FakeJobContext:
        def __init__(self, store, payload):
            self.store = store
            self._payload = payload

        @property
        def payload(self):
            return self._payload

    run_probe_vector_canary(_FakeJobContext(store, {"host": "dev"}))
    row = store.platform.execute("SELECT * FROM probe_run WHERE name = 'vector_canary'").fetchone()
    assert row is not None
    assert row["status"] == "pass"


def test_default_host_label_is_a_non_empty_string():
    """N-4 fix round: the shared fallback both the ingest index job's own
    enqueue and (indirectly, via its own hostname resolution) SessionStart
    now agree on, instead of the ingest side always reporting the bare
    literal "default"."""
    from trialerror.retrieve.handlers import default_host_label

    label = default_host_label()
    assert isinstance(label, str) and label


def test_enqueue_vector_canary_if_due_is_idempotent_within_the_same_hour(store):
    from trialerror.retrieve.handlers import enqueue_vector_canary_if_due

    first = enqueue_vector_canary_if_due(store, host="dev")
    second = enqueue_vector_canary_if_due(store, host="dev")
    assert first is True
    assert second is False
    jobs = store.jobs.execute("SELECT COUNT(*) AS n FROM job WHERE kind = 'custom'").fetchone()["n"]
    assert jobs == 1


def test_enqueue_vector_canary_if_due_does_not_preempt_real_work(store):
    """Second fix round: the canary used to land as an ordinary, immediately-
    claimable job -- so ``claim_next``'s ``created_ts ASC`` ordering let it
    jump ahead of a real pipeline job enqueued moments later, changing the
    order real work ran in (the exact regression
    ``tests/test_ingest_reindex_vectors.py::test_adopting_an_offload_embed_
    result_fills_the_vector_index`` and ``tests/test_offload_stage.py::
    test_full_round_trip_pending_to_indexed`` hit). It must now come out
    deferred (see ``trialerror.jobs.ledger.enqueue``'s ``defer_s``), so a real
    job created right after it is still claimed first."""
    from trialerror.jobs import ledger
    from trialerror.retrieve.handlers import enqueue_vector_canary_if_due

    assert enqueue_vector_canary_if_due(store, host="dev") is True
    real = ledger.enqueue(store, kind="ocr", payload={"doc_id": "DOC-1"})

    claimed = ledger.claim_next(store, worker_id="w1")
    assert claimed["job_id"] == real["job_id"], "the canary must not preempt real pipeline work"
