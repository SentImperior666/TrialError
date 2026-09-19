"""Tests for :mod:`trialerror.retrieve.checks` -- the ``retrieve`` doctor
checks (``fence_integrity``, ``retrieval_latency``, and C-0080's
``fulltext_index_stale``)."""

from __future__ import annotations

import pytest

from trialerror.retrieve import tantivysearch
from trialerror.retrieve.checks import check_fence_integrity, check_fulltext_index_stale, check_retrieval_latency
from trialerror.stores import paths
from trialerror.stores.writer import insert
from trialerror.util.doctor import DoctorContext, clear_registry, discover_and_register_checks, run_checks
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now

from tests._retrieve_fixtures import bootstrap_launch, build_small_corpus


def test_fence_integrity_skips_when_no_program_root():
    result = check_fence_integrity(DoctorContext(program_root=None))
    assert result.status == "skip"


def test_fence_integrity_skips_when_no_commercial_restricted_chunks(store):
    result = check_fence_integrity(DoctorContext(program_root=store.program_root))
    assert result.status == "skip"
    assert result.details["sampled"] == 0


def test_fence_integrity_passes_on_a_real_fenced_corpus(store):
    build_small_corpus(store)
    result = check_fence_integrity(DoctorContext(program_root=store.program_root))
    assert result.status == "pass"
    assert result.details["sampled"] >= 1
    assert result.details["offender_chunk_ids"] == []


def test_fence_integrity_fails_if_a_chunk_would_exceed_the_20_word_cap(store, monkeypatch):
    """Regression-sentinel proof: force a broken ``excerpt_words`` (no cap)
    and confirm the check actually catches it -- otherwise this doctor
    check would be a check that can never fail."""
    build_small_corpus(store)
    import trialerror.retrieve.checks as checks_mod

    monkeypatch.setattr(checks_mod, "excerpt_words", lambda text, max_words=20: text)  # no-op cap -- always "violates"
    result = check_fence_integrity(DoctorContext(program_root=store.program_root))
    assert result.status == "fail"
    assert result.details["offender_chunk_ids"]


def test_retrieval_latency_skips_when_no_chunks(store):
    result = check_retrieval_latency(DoctorContext(program_root=store.program_root))
    assert result.status == "skip"


def test_retrieval_latency_passes_and_reports_elapsed_ms(store):
    build_small_corpus(store)
    result = check_retrieval_latency(DoctorContext(program_root=store.program_root))
    assert result.status in ("pass", "warn")
    assert result.details["chunks"] >= 1
    assert "elapsed_ms" in result.details


def test_retrieval_latency_reports_fts5_when_no_tantivy_index_is_built(store):
    """Rule 4's fallback state -- the one every existing program is in
    before it runs ``reindex-fulltext`` -- must be what this probe reports,
    not silently assumed away."""
    build_small_corpus(store)
    result = check_retrieval_latency(DoctorContext(program_root=store.program_root))
    assert result.details["fulltext_backend"] == "fts5"
    assert result.message.startswith("fts5 prefilter")


@pytest.mark.skipif(not tantivysearch.tantivy_available(), reason="tantivy-py not installed")
def test_retrieval_latency_reports_tantivy_once_the_index_is_built(store):
    """Fix pass, finding LD-03: before the fix this probe called
    ``fts_search`` directly, so on a program actually served by tantivy it
    measured a tier nothing was querying -- backwards for a lane whose
    whole point is the lexical tier's latency. It now goes through the
    SAME resolution ``engine.search`` uses and names the backend that
    actually answered."""
    build_small_corpus(store)
    tantivysearch.reindex(store.knowledge, paths.fulltext_index_path(store.program_root))
    result = check_retrieval_latency(DoctorContext(program_root=store.program_root))
    assert result.details["fulltext_backend"] == "tantivy"
    assert result.message.startswith("tantivy prefilter")


def test_checks_are_auto_discovered_via_the_doctor_framework(store):
    clear_registry()
    discover_and_register_checks()
    results = run_checks(DoctorContext(program_root=store.program_root), only=["fence_integrity", "retrieval_latency"])
    names = {r.name for r in results}
    assert names == {"fence_integrity", "retrieval_latency"}


# ---------------------------------------------------------------------------
# fulltext_index_stale (C-0080)
# ---------------------------------------------------------------------------


def _pin_backend(program_root, backend: str) -> None:
    (program_root / "trialerror.toml").write_text(
        f'[program]\nid = "PROG-test"\n\n[retrieve]\nfulltext_backend = "{backend}"\n', encoding="utf-8"
    )


def test_fulltext_index_stale_skips_when_no_program_root():
    assert check_fulltext_index_stale(DoctorContext(program_root=None)).status == "skip"


def test_fulltext_index_stale_skips_when_the_program_pins_fts5(store):
    build_small_corpus(store)
    _pin_backend(store.program_root, "fts5")
    result = check_fulltext_index_stale(DoctorContext(program_root=store.program_root))
    assert result.status == "skip"
    assert result.details["configured_backend"] == "fts5"


def test_fulltext_index_stale_skips_when_tantivy_is_not_installed(store, monkeypatch):
    build_small_corpus(store)
    monkeypatch.setattr(tantivysearch, "tantivy_module", lambda: None)
    result = check_fulltext_index_stale(DoctorContext(program_root=store.program_root))
    assert result.status == "skip"
    assert result.details["tantivy_available"] is False


def test_fulltext_index_stale_skips_on_an_empty_corpus(store):
    result = check_fulltext_index_stale(DoctorContext(program_root=store.program_root))
    assert result.status == "skip"


@pytest.mark.skipif(not tantivysearch.tantivy_available(), reason="tantivy-py not installed")
def test_fulltext_index_stale_warns_when_no_index_has_been_built(store):
    """The state every existing program is in the moment C-0080 merges:
    a warn (search still answers, out of FTS5), never a fail."""
    build_small_corpus(store)
    result = check_fulltext_index_stale(DoctorContext(program_root=store.program_root))
    assert result.status == "warn"
    assert "reindex-fulltext" in result.message
    assert result.details["state"] == "missing"


@pytest.mark.skipif(not tantivysearch.tantivy_available(), reason="tantivy-py not installed")
def test_fulltext_index_stale_passes_on_a_freshly_built_index(store):
    build_small_corpus(store)
    tantivysearch.reindex(store.knowledge, paths.fulltext_index_path(store.program_root))
    result = check_fulltext_index_stale(DoctorContext(program_root=store.program_root))
    assert result.status == "pass"
    assert result.details["db_fingerprint"] == result.details["meta_fingerprint"]


@pytest.mark.skipif(not tantivysearch.tantivy_available(), reason="tantivy-py not installed")
def test_fulltext_index_stale_fails_when_the_corpus_moved_past_the_index(store):
    """The dangerous case, and the reason this check is a ``fail``: the
    serving path TRUSTS a ready index, so a stale one silently
    under-recalls rather than degrading visibly."""
    build_small_corpus(store)
    tantivysearch.reindex(store.knowledge, paths.fulltext_index_path(store.program_root))
    launch_id = bootstrap_launch(store)
    insert(
        store, "source",
        {"source_id": new_id("SRC"), "kind": "report", "title": "later", "license_tier": "open",
         "acquisition_route": "web", "request_state": "indexed", "registered_ts": now(),
         "registered_by_launch": launch_id},
    )
    doc_id = store.knowledge.execute("SELECT doc_id FROM document LIMIT 1").fetchone()["doc_id"]
    element_id = store.knowledge.execute("SELECT element_id FROM element LIMIT 1").fetchone()["element_id"]
    insert(
        store, "chunk",
        {"chunk_id": new_id("CHK"), "doc_id": doc_id, "seq": 9999, "text": "written after the index was built",
         "token_count": 6, "element_first": element_id, "element_last": element_id, "page_start": 1, "page_end": 1,
         "sha256": "f" * 64, "chunker_id": "t", "chunker_version": "1", "created_ts": now()},
    )
    store.knowledge.commit()

    result = check_fulltext_index_stale(DoctorContext(program_root=store.program_root))
    assert result.status == "fail"
    assert "reindex-fulltext" in result.message


@pytest.mark.skipif(not tantivysearch.tantivy_available(), reason="tantivy-py not installed")
def test_fulltext_index_stale_is_repaired_by_the_reindex_it_recommends(store):
    """The check and its documented remedy, end to end -- a doctor finding
    whose suggested fix doesn't actually clear it is worse than no check."""
    build_small_corpus(store)
    ctx = DoctorContext(program_root=store.program_root)
    assert check_fulltext_index_stale(ctx).status == "warn"
    tantivysearch.reindex(store.knowledge, paths.fulltext_index_path(store.program_root))
    assert check_fulltext_index_stale(ctx).status == "pass"


def test_fulltext_index_stale_is_auto_discovered_by_the_doctor_framework(store):
    clear_registry()
    discover_and_register_checks()
    results = run_checks(DoctorContext(program_root=store.program_root), only=["fulltext_index_stale"])
    assert [r.name for r in results] == ["fulltext_index_stale"]
    assert results[0].category == "retrieve"
