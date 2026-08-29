"""Tests for :mod:`trialerror.retrieve.checks` -- M8's doctor checks
(``fence_integrity``, ``retrieval_latency``)."""

from __future__ import annotations

from trialerror.retrieve.checks import check_fence_integrity, check_retrieval_latency
from trialerror.stores.writer import insert
from trialerror.util.doctor import DoctorContext, clear_registry, discover_and_register_checks, run_checks
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now

from tests._retrieve_fixtures import build_small_corpus


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


def test_checks_are_auto_discovered_via_the_doctor_framework(store):
    clear_registry()
    discover_and_register_checks()
    results = run_checks(DoctorContext(program_root=store.program_root), only=["fence_integrity", "retrieval_latency"])
    names = {r.name for r in results}
    assert names == {"fence_integrity", "retrieval_latency"}
